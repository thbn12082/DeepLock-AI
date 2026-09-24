from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .util import REPO_ROOT

# Codex renames models under the same account, so match the family rather than
# pinning one version - a rename upstream should not be a hard outage here.
_GPT_FAMILY = re.compile(r"^gpt-[0-9][0-9A-Za-z._-]*$")


# Upper bound on in-flight 9router requests. This is a tuning dial, not a
# safety invariant: the real limits are the Codex accounts' own rate limits,
# which answer 429 with a "reset after" the builder already honours. Measured
# on two accounts: 40 in flight produced no 429s and about 90 completed
# steps per 15 minutes, and 112 produced no 429s and 166 steps, so the
# accounts were never the constraint at those levels - this ceiling was.
# 192 then went backwards: still no 429s, but the router queues rather than
# rejects, so throughput collapsed from 743 completed steps per ten minutes to
# about 40. Past roughly 112 the requests just wait behind each other.
MAX_ROUTER_CONCURRENCY = 256


def _local_9router_key(base_url: str) -> str | None:
    explicit = os.getenv("OPENAI_API_KEY") or os.getenv("NINEROUTER_API_KEY")
    if explicit:
        return explicit
    if "127.0.0.1:20128" not in base_url and "localhost:20128" not in base_url:
        return None
    roaming = os.getenv("APPDATA")
    if not roaming:
        return None
    database = Path(roaming) / "9router" / "db" / "data.sqlite"
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT key FROM apiKeys WHERE isActive = 1 ORDER BY createdAt DESC LIMIT 1"
            ).fetchone()
            return str(row[0]).strip() if row and row[0] else None
        finally:
            connection.close()
    except sqlite3.Error:
        return None


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    generator_model: str
    review_model: str
    model_prefix: str
    concurrency: int
    generator_reasoning_effort: str
    review_reasoning_effort: str
    generator_prompt_version: str
    review_prompt_version: str
    max_repair_rounds: int
    request_timeout_seconds: float
    max_api_attempts: int
    review_batch_size: int = 1
    # 9router's Codex adapter forwards the priority tier to the Responses API.
    # Keep it optional so direct Settings constructions and non-9router
    # adapters retain their existing behaviour.
    service_tier: str | None = None
    vision_reasoning_effort: str = "low"
    max_atoms_per_generation_call: int = 2
    # This is an explicit, opt-in second pass over the already generated
    # candidate.  Legacy/catalog-v1 builds retain their factual reviewer unless
    # the caller deliberately enables learner rewrite mode.
    editorial_rewrite_enabled: bool = False
    editorial_model: str = "gpt-5.5"
    editorial_reasoning_effort: str = "high"
    editorial_prompt_version: str = "learner-rewrite-v1"
    max_atoms_per_editorial_call: int = 2

    def __post_init__(self) -> None:
        # Directly constructed Settings instances are used by smoke tests and
        # catalog workers, so enforce the same safe bound as environment input.
        object.__setattr__(
            self,
            "max_atoms_per_generation_call",
            max(1, min(5, int(self.max_atoms_per_generation_call))),
        )
        object.__setattr__(
            self,
            "max_atoms_per_editorial_call",
            max(1, min(5, int(self.max_atoms_per_editorial_call))),
        )

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or REPO_ROOT / ".env", override=False)
        generator = os.getenv("OPENAI_GENERATOR_MODEL", "gpt-5.5")
        reviewer = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.5")
        editor = os.getenv("OPENAI_EDITORIAL_MODEL", "gpt-5.5")
        # gpt-5.5 through the local 9router remains the default and is what the
        # course-v3 repair backlog keeps using, because the build cache is keyed
        # by logical model and switching would discard it. A Claude model is
        # allowed only as a deliberate, uniform opt-in for new-lecture builds.
        if not (generator == reviewer == editor):
            raise ValueError(
                "DeepLock requires the same logical model for generator, reviewer, and editor; "
                f"got generator={generator!r}, reviewer={reviewer!r}, editor={editor!r}"
            )
        if not _GPT_FAMILY.match(generator) and not generator.startswith("claude-"):
            raise ValueError(
                "DeepLock supports a gpt-* logical model (via 9router) or a claude-* model; "
                f"got {generator!r}"
            )
        # Five active Codex routes are configured on the target 9router. A
        # production headroom probe verified two simultaneous requests per
        # route without queuing or 429/502 responses, so keep up to ten calls
        # in flight while retaining an environment override for smaller runs.
        #
        # That ceiling is a property of the 9router account pool, not of the
        # builder, so a claude-* run gets its own dial: the relay is a separate
        # provider with separate limits and the whole point of using it is
        # throughput. Tune with CLAUDE_CONCURRENCY and back off if the endpoint
        # starts returning 429.
        if generator.startswith("claude-"):
            concurrency = max(1, min(160, int(os.getenv("CLAUDE_CONCURRENCY", "24"))))
        else:
            concurrency = max(
                1,
                min(MAX_ROUTER_CONCURRENCY, int(os.getenv("NINEROUTER_CONCURRENCY", "10"))),
            )
        base_url = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
        return cls(
            api_key=_local_9router_key(base_url),
            base_url=base_url,
            generator_model=generator,
            review_model=reviewer,
            model_prefix=os.getenv("NINEROUTER_MODEL_PREFIX", "cx/"),
            concurrency=concurrency,
            generator_reasoning_effort=os.getenv("OPENAI_GENERATOR_REASONING_EFFORT", "medium"),
            review_reasoning_effort=os.getenv("OPENAI_REVIEW_REASONING_EFFORT", "high"),
            generator_prompt_version=os.getenv("GENERATOR_PROMPT_VERSION", "course-v2"),
            review_prompt_version=os.getenv("REVIEW_PROMPT_VERSION", "reviewer-v2"),
            max_repair_rounds=max(0, min(2, int(os.getenv("MAX_AI_REPAIR_ROUNDS", "2")))),
            # Large strict-schema lecture responses regularly take longer than
            # five minutes. 9router continues those upstream calls after a
            # short client timeout, which wastes completed responses and then
            # retries them. Allow the successful response to reach the cache.
            request_timeout_seconds=float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "1200")),
            max_api_attempts=max(1, min(3, int(os.getenv("OPENAI_MAX_ATTEMPTS", "3")))),
            review_batch_size=max(1, min(5, int(os.getenv("NINEROUTER_REVIEW_BATCH_SIZE", "5")))),
            service_tier=(os.getenv("OPENAI_SERVICE_TIER") or "").strip() or None,
            vision_reasoning_effort=os.getenv("OPENAI_VISION_REASONING_EFFORT", "low"),
            max_atoms_per_generation_call=max(
                1,
                min(5, int(os.getenv("OPENAI_MAX_ATOMS_PER_GENERATION_CALL", "2"))),
            ),
            editorial_rewrite_enabled=(
                os.getenv("LEARNER_REWRITE_ENABLED", "false").strip().casefold()
                in {"1", "true", "yes", "on"}
            ),
            editorial_model=editor,
            editorial_reasoning_effort=os.getenv(
                "OPENAI_EDITORIAL_REASONING_EFFORT",
                "high",
            ),
            editorial_prompt_version=os.getenv(
                "EDITORIAL_PROMPT_VERSION",
                "learner-rewrite-v1",
            ),
            max_atoms_per_editorial_call=int(
                os.getenv("OPENAI_MAX_ATOMS_PER_EDITORIAL_CALL", "2")
            ),
        )

    def transport_model(self, logical_model: str, *, reviewer: bool = False) -> str:
        # Claude models never go through the 9router route prefix; they are
        # called directly by ClaudeAdapter. Returning them unchanged also keeps
        # their build-cache keys distinct from the cx/gpt-5.5 entries.
        if logical_model.startswith("claude-"):
            return logical_model
        if "127.0.0.1:20128" not in self.base_url and "localhost:20128" not in self.base_url:
            return logical_model
        suffix = "-review" if reviewer else ""
        return f"{self.model_prefix}{logical_model}{suffix}"
