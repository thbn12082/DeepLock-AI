"""Anthropic Claude adapter for the DeepLock content builder.

This sits beside `api.py` (the 9router/Responses adapter) and deliberately does
not replace it: course-v3 repair work keeps running on `gpt-5.5` through
9router, while new-lecture builds can be pointed at Claude. Both expose the same
`structured()` signature so the builder does not care which one it holds.

Two things that matter for this project:

*   Every call is metered against a hard token budget held in a JSON ledger on
    disk, so a long unattended backlog run cannot quietly overspend.
*   Cache keys in `BuildCache` include the logical model, so anything built here
    is cached separately from the gpt-5.5 work. That is intentional - it is what
    keeps the two pipelines from invalidating each other.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anthropic

from ..settings import Settings
from .api import DEFAULT_OUTPUT_VERBOSITY, ModelResponse

# Anthropic's own default and the one this project standardises on. Overridable
# through CLAUDE_MODEL for a deliberate experiment, never silently downgraded.
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

# Salvage happens deep inside a worker thread. Without a log line, "the run had
# no stream-cut failures" cannot be told apart from "cuts happened and were
# stitched", which are very different facts about the content.
_LOG = logging.getLogger(__name__)

# Streaming is required for large max_tokens, and every call here asks for a
# whole rewritten lecture module, so stream unconditionally. Thinking tokens
# count against this ceiling too, so an editorial group of five atoms at medium
# effort overran 32k and came back truncated; 64k leaves headroom without
# reaching the 128k model cap. Override with CLAUDE_MAX_OUTPUT_TOKENS.
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("CLAUDE_MAX_OUTPUT_TOKENS", "64000"))

# Effort replaces the Responses API's reasoning.effort. Same vocabulary for the
# three levels this builder uses.
_ALLOWED_EFFORT = {"low", "medium", "high", "xhigh", "max"}

_DATA_URL = re.compile(r"^data:(?P<media_type>[\w.+/-]+);base64,(?P<data>.+)$", re.DOTALL)


class TokenBudgetExceeded(RuntimeError):
    """Raised before a request that would take the run past its token budget."""


@dataclass
class BudgetState:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        # Weighted the way Anthropic prices these: a cache write costs about a
        # quarter more than fresh input, a cache read about a tenth as much.
        # The relay's own billing is not visible from here, so this is an
        # estimate used for the run's safety ceiling - reconcile the raw
        # counters below against the provider dashboard.
        return (
            self.input_tokens
            + self.output_tokens
            + int(self.cache_write_tokens * 1.25)
            + int(self.cache_read_tokens * 0.1)
        )


class TokenBudget:
    """A durable, process-safe-enough token ledger with a hard ceiling.

    The ledger is written after every call so an interrupted run resumes with
    its spend intact rather than starting from zero.
    """

    def __init__(self, path: Path, *, limit_tokens: int) -> None:
        if limit_tokens <= 0:
            raise ValueError("Token budget limit must be positive")
        self.path = path
        self.limit_tokens = limit_tokens
        self._lock = threading.Lock()
        self.state = self._load()

    def _load(self) -> BudgetState:
        if not self.path.is_file():
            return BudgetState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return BudgetState()
        if not isinstance(raw, dict):
            return BudgetState()
        return BudgetState(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            calls=int(raw.get("calls") or 0),
            cache_write_tokens=int(raw.get("cache_write_tokens") or 0),
            cache_read_tokens=int(raw.get("cache_read_tokens") or 0),
        )

    def _flush(self) -> None:
        payload = {
            "input_tokens": self.state.input_tokens,
            "output_tokens": self.state.output_tokens,
            "cache_write_tokens": self.state.cache_write_tokens,
            "cache_read_tokens": self.state.cache_read_tokens,
            "total_tokens": self.state.total,
            "calls": self.state.calls,
            "limit_tokens": self.limit_tokens,
            "remaining_tokens": max(0, self.limit_tokens - self.state.total),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def check(self, *, headroom: int = 0) -> None:
        with self._lock:
            if self.state.total + headroom >= self.limit_tokens:
                raise TokenBudgetExceeded(
                    f"Claude token budget exhausted: used {self.state.total} of "
                    f"{self.limit_tokens} tokens across {self.state.calls} calls"
                )

    def record(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        with self._lock:
            self.state.input_tokens += max(0, int(input_tokens))
            self.state.output_tokens += max(0, int(output_tokens))
            self.state.cache_write_tokens += max(0, int(cache_write_tokens))
            self.state.cache_read_tokens += max(0, int(cache_read_tokens))
            self.state.calls += 1
            self._flush()

    @property
    def remaining(self) -> int:
        return max(0, self.limit_tokens - self.state.total)


def _tool_name(schema_name: str) -> str:
    """Coerce a builder schema name into a legal Anthropic tool name."""

    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", schema_name).strip("_") or "result"
    return cleaned[:64]


def _json_payload(text: str) -> str:
    """Pull the JSON object out of a reply that may be fenced or prefaced.

    Only used by the last-resort prompt strategy, where the endpoint offers no
    schema enforcement of its own.
    """

    body = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    if body.startswith("{") and body.endswith("}"):
        return body
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("Claude reply contained no JSON object")
    return body[start : end + 1]


def _image_block(data_url: str) -> dict[str, Any]:
    match = _DATA_URL.match(data_url.strip())
    if not match:
        raise ValueError("Claude adapter only accepts base64 data: image URLs")
    data = match.group("data")
    # Reject anything that is not valid base64 before it reaches the wire.
    base64.b64decode(data, validate=True)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": match.group("media_type"),
            "data": data,
        },
    }




_TRUNCATED_STREAM_MARKERS = (
    "incomplete chunked read",
    "peer closed connection",
    "response ended prematurely",
    "connection reset",
)


def _is_truncated_stream(error: BaseException) -> bool:
    """Whether an exception looks like the relay cutting a stream short."""

    text = f"{error}".lower()
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        text += f" {cause}".lower()
    return any(marker in text for marker in _TRUNCATED_STREAM_MARKERS)



def _snapshot(stream: Any) -> Any:
    """The partial message built so far, or None if the stream never started."""

    try:
        return stream.current_message_snapshot
    except Exception:
        return None


def _streamed_text(stream: Any) -> str:
    """Text accumulated before a stream was cut, or "" if none survived."""

    # current_message_snapshot asserts internally until the first event has
    # arrived, so a relay that drops before message_start raises AssertionError
    # here rather than returning None. Swallow it: "nothing survived" is the
    # correct answer, and it is what lets the non-streamed retry run.
    try:
        snapshot = stream.current_message_snapshot
    except Exception:
        return ""
    if snapshot is None:
        return ""
    parts: list[str] = []
    for block in getattr(snapshot, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _schema_violations(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Shallow structural check of a payload against the parts models rely on.

    Not a full JSON Schema validator - the builder's Pydantic models are the
    real gate. This exists to turn a malformed reply into a precise, quotable
    complaint the model can act on, so it only inspects required keys, object
    shape and array bounds.
    """

    problems: list[str] = []
    declared = schema.get("type")
    if declared == "object":
        if not isinstance(data, dict):
            return [f"{path} must be an object, got {type(data).__name__}"]
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in data:
                problems.append(f"{path}.{key} is required but missing")
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in properties:
                    problems.append(f"{path}.{key} is not an allowed field")
        for key, sub in properties.items():
            if key in data:
                problems.extend(_schema_violations(data[key], sub, f"{path}.{key}"))
    elif declared == "array":
        if not isinstance(data, list):
            return [f"{path} must be an array, got {type(data).__name__}"]
        minimum, maximum = schema.get("minItems"), schema.get("maxItems")
        if isinstance(minimum, int) and len(data) < minimum:
            problems.append(f"{path} needs at least {minimum} items, got {len(data)}")
        if isinstance(maximum, int) and len(data) > maximum:
            problems.append(f"{path} allows at most {maximum} items, got {len(data)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data):
                problems.extend(_schema_violations(item, item_schema, f"{path}[{index}]"))
    return problems[:40]



@dataclass
class _StitchedMessage:
    """A message rebuilt from a cut stream plus its continuation."""

    text: str
    model: str
    message_id: str
    reason: str = "end_turn"

    @property
    def id(self) -> str:
        return self.message_id

    @property
    def content(self) -> list[Any]:
        return [_TextBlock(self.text)]

    @property
    def stop_reason(self) -> str:
        return self.reason

    @property
    def usage(self) -> None:
        return None


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


class ClaudeAdapter:
    """Messages API adapter exposing the same surface as `ResponsesAdapter`."""

    def __init__(
        self,
        settings: Settings,
        *,
        budget: TokenBudget | None = None,
        model: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        self.settings = settings
        self.budget = budget
        self.model = model or os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
        self.prompt_cache = (
            os.getenv("CLAUDE_PROMPT_CACHE", "1").strip().casefold()
            not in {"0", "false", "no", "off"}
        )
        self.max_output_tokens = max_output_tokens
        # This project's key belongs to a third-party Anthropic-compatible
        # relay, so the endpoint is taken from CLAUDE_PROXY_BASE_URL and passed
        # explicitly. ANTHROPIC_BASE_URL is deliberately ignored: some shells
        # (including Claude Code sessions) export it pointing at
        # api.anthropic.com, which would silently send a relay key to
        # Anthropic. The SDK still sends the headers a relay expects,
        # `x-api-key` and `anthropic-version`.
        base_url = (os.getenv("CLAUDE_PROXY_BASE_URL") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError(
                "CLAUDE_PROXY_BASE_URL is not set. Point it at the Anthropic-compatible "
                "endpoint that issued this key; the adapter will not fall back to "
                "api.anthropic.com."
            )
        self.base_url = base_url
        # Relays differ in how they want the credential. Some take an API key
        # in `x-api-key`, others a bearer token in `Authorization`; the SDK
        # sends whichever of the two constructor arguments is set, so honour
        # ANTHROPIC_AUTH_TOKEN when present and fall back to ANTHROPIC_API_KEY.
        auth_token = (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip()
        credentials: dict[str, str] = (
            {"auth_token": auth_token}
            if auth_token
            else {"api_key": (os.getenv("ANTHROPIC_API_KEY") or "").strip()}
        )
        if not next(iter(credentials.values()), ""):
            raise RuntimeError(
                "No relay credential found; set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY."
            )
        self.client = anthropic.Anthropic(
            base_url=base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
            **credentials,
        )
        # Which structured-output mechanism this endpoint actually accepts.
        # A proxy pinned to anthropic-version 2023-06-01 predates
        # `output_config`, so probe once and then stay on what worked.
        self._strategy: str | None = os.getenv("CLAUDE_STRUCTURED_STRATEGY") or None

    def structured(
        self,
        *,
        logical_model: str,
        reviewer: bool,
        reasoning_effort: str,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
        input_image_data_urls: list[str] | None = None,
        output_verbosity: Literal["low", "medium", "high"] = DEFAULT_OUTPUT_VERBOSITY,
    ) -> ModelResponse:
        # `logical_model`/`reviewer` select a 9router route and mean nothing
        # here; verbosity has no Messages API equivalent. `schema_name` is kept:
        # it names the tool in the strict-tool fallback.
        del logical_model, reviewer, output_verbosity
        effort = reasoning_effort if reasoning_effort in _ALLOWED_EFFORT else "high"

        content: list[dict[str, Any]] = [
            _image_block(url) for url in (input_image_data_urls or [])
        ]
        content.append({"type": "text", "text": input_text})

        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_api_attempts + 1):
            if self.budget is not None:
                # Refuse before spending rather than after.
                self.budget.check(headroom=self.max_output_tokens)
            try:
                response = self._request(
                    effort=effort,
                    instructions=instructions,
                    content=content,
                    schema_name=_tool_name(schema_name),
                    schema=schema,
                )
                # A relay without server-side schema enforcement will
                # occasionally answer with a shape the builder cannot validate.
                # Showing the model exactly which constraint it broke fixes far
                # more of those than a blind retry, and costs one short call
                # instead of regenerating the whole module.
                violations = _schema_violations(response.data, schema)
                if violations:
                    response = self._repair(
                        response=response,
                        violations=violations,
                        effort=effort,
                        instructions=instructions,
                        content=content,
                        schema_name=_tool_name(schema_name),
                        schema=schema,
                    )
                return response
            except TokenBudgetExceeded:
                raise
            except anthropic.RateLimitError as error:
                # The relay publishes 200 requests/min. Honour its own
                # Retry-After when it sends one instead of hammering back.
                last_error = error
                retry_after = 0.0
                header = getattr(getattr(error, "response", None), "headers", None)
                if header is not None:
                    try:
                        retry_after = float(header.get("retry-after") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                time.sleep(max(retry_after, 5.0) + random.uniform(0.0, 1.0))
            except Exception as error:  # transient SDK/network/schema failures
                last_error = error
                if attempt >= self.settings.max_api_attempts:
                    break
                delay = min(8.0, 0.75 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.35)
                time.sleep(delay)
        raise RuntimeError(
            f"Claude Messages request failed after retries: {last_error}"
        ) from last_error

    def _repair(
        self,
        *,
        response: ModelResponse,
        violations: list[str],
        effort: str,
        instructions: str,
        content: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
    ) -> ModelResponse:
        """Ask once for a corrected payload, quoting the exact violations."""

        attempts = max(1, int(os.getenv("CLAUDE_SCHEMA_REPAIR_ROUNDS", "2")))
        current = response
        for _ in range(attempts):
            if self.budget is not None:
                self.budget.check(headroom=self.max_output_tokens)
            complaint = "\n".join(f"- {item}" for item in violations)
            previous = json.dumps(current.data, ensure_ascii=False)[:6000]
            repair_content = [
                *content,
                {
                    "type": "text",
                    "text": (
                        "Your previous reply did not match the required shape.\n"
                        f"It was:\n{previous}\n\n"
                        f"Problems:\n{complaint}\n\n"
                        "Return the corrected JSON object only. Keep the content you "
                        "already wrote; change only what is needed to satisfy the "
                        "shape, and use the exact field names from the schema."
                    ),
                },
            ]
            try:
                current = self._to_response(
                    self._send(
                        strategy=self._strategy or "prompt",
                        effort=effort,
                        instructions=instructions,
                        content=repair_content,
                        schema_name=schema_name,
                        schema=schema,
                    ),
                    strategy=self._strategy or "prompt",
                    schema_name=schema_name,
                )
            except TokenBudgetExceeded:
                raise
            except Exception:
                break
            violations = _schema_violations(current.data, schema)
            if not violations:
                return current
        # Hand back the best attempt; the builder's own models decide from here.
        return current


    # -- structured output -------------------------------------------------
    #
    # Three ways to get schema-valid JSON back, most faithful first. A relay on
    # a 2023-06-01 anthropic-version will reject the first (and possibly the
    # second) with a 400; whichever works first is remembered for the process
    # so later calls do not keep paying for a probe.

    def _request(
        self,
        *,
        effort: str,
        instructions: str,
        content: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
    ) -> ModelResponse:
        order = [self._strategy] if self._strategy else ["output_config", "tool", "prompt"]
        errors: list[str] = []
        for strategy in order:
            try:
                message = self._send(
                    strategy=strategy,
                    effort=effort,
                    instructions=instructions,
                    content=content,
                    schema_name=schema_name,
                    schema=schema,
                )
            except TokenBudgetExceeded:
                raise
            except anthropic.APIStatusError as error:
                # Only an unsupported-parameter rejection justifies stepping
                # down; anything else is a real failure worth surfacing.
                if error.status_code in {400, 404, 422} and self._strategy is None:
                    errors.append(f"{strategy}: HTTP {error.status_code} {str(error)[:160]}")
                    continue
                raise
            except (ValueError, json.JSONDecodeError) as error:
                # A relay can accept `output_config` and still answer in plain
                # key=value text instead of JSON. That is the same kind of "not
                # supported here" as a 400, so step down to the next strategy
                # rather than burn the retry budget on a body that will never
                # parse.
                if self._strategy is None:
                    errors.append(f"{strategy}: unparseable body ({str(error)[:100]})")
                    continue
                raise
            response = self._to_response(message, strategy=strategy, schema_name=schema_name)
            self._strategy = strategy
            return response
        raise RuntimeError(
            "No structured-output mechanism was accepted by this endpoint: " + "; ".join(errors)
        )

    def _system(self, text: str) -> Any:
        """The system prompt, marked cacheable.

        Every call of a given step type repeats the same preamble: the role
        instructions plus, under the prompt strategy, the whole JSON schema
        inlined. One lecture makes dozens of such calls, so that repeat is the
        largest single slice of input spend. Measured against the relay, three
        identical calls cost 11,004 input tokens uncached and 2,556 cached.

        A block below the model's minimum cacheable length is simply not
        cached and the request still succeeds, so there is nothing to guard.
        """

        if not self.prompt_cache:
            return text
        return [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _send(
        self,
        *,
        strategy: str,
        effort: str,
        instructions: str,
        content: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if strategy == "output_config":
            kwargs["system"] = self._system(instructions)
            kwargs["output_config"] = {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            }
        elif strategy == "tool":
            kwargs["system"] = self._system(instructions)
            kwargs["tools"] = [
                {
                    "name": schema_name,
                    "description": "Return the result. Every field is required.",
                    "strict": True,
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": schema_name}
            kwargs["output_config"] = {"effort": effort}
        elif strategy == "prompt":
            kwargs["system"] = self._system(
                f"{instructions}\n\n"
                "Reply with a single JSON object and nothing else. No prose, no "
                "explanation, no markdown fence. It must validate against this "
                f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
            )
            # Effort is the single largest cost dial and the prompt branch used to
            # drop it: without output_config the relay answers at its own default,
            # which measured 3,399 output tokens against 482 at medium for the same
            # task. The schema stays out of output_config on purpose - this relay
            # accepts "format" and then ignores it, which is why the prompt strategy
            # exists at all - but it does honour "effort".
            kwargs["output_config"] = {"effort": effort}
        else:
            raise ValueError(f"Unknown structured-output strategy: {strategy}")

        message = None
        try:
            with self.client.messages.stream(**kwargs) as stream:
                try:
                    message = stream.get_final_message()
                except Exception as error:
                    # Some relays cut a long chunked response part-way through
                    # ("peer closed connection without sending complete message
                    # body"). Everything streamed before the cut is already in
                    # the snapshot, so hand that back to the model and ask it
                    # to finish, instead of paying for the whole answer again.
                    if not _is_truncated_stream(error):
                        raise
                    partial = _streamed_text(stream)
                    self._record(_snapshot(stream))
                    if partial:
                        try:
                            message = self._continue_after_cut(
                                partial=partial, kwargs=kwargs, schema=schema
                            )
                        except Exception as salvage_error:
                            # A gateway may refuse the continuation request
                            # itself - this relay documents "assistant-prefill
                            # not supported" - and a salvage that cannot run is
                            # not a reason to fail the whole call. Drop back to
                            # the non-streamed retry below.
                            _LOG.info(
                                "continuation unavailable (%s); retrying unstreamed",
                                salvage_error,
                            )
                            message = None
                    if message is None:
                        raise
        except Exception as error:
            if not _is_truncated_stream(error):
                raise
            # Nothing usable arrived before the cut. A single response body
            # sometimes survives where a stream does not, so try once more
            # without streaming, at a ceiling that fits the HTTP timeout.
            message = self.client.messages.create(
                **{**kwargs, "max_tokens": min(kwargs.get("max_tokens", 16000), 16000)}
            )
        usage = getattr(message, "usage", None)
        if self.budget is not None and usage is not None:
            self.budget.record(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        return message

    def _record(self, usage_source: Any) -> None:
        """Meter a call even when it failed, so the ledger is not optimistic."""

        usage = getattr(usage_source, "usage", None)
        if self.budget is None or usage is None:
            return
        self.budget.record(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    def _continue_after_cut(
        self, *, partial: str, kwargs: dict[str, Any], schema: dict[str, Any]
    ) -> Any:
        """Ask the model to finish an answer whose stream was cut.

        The relay charged for what it already sent, so resending the whole
        request throws that away. Handing the fragment back and asking only for
        the remainder costs the tail instead of the whole body.
        """

        tail_kwargs = {
            **kwargs,
            "max_tokens": min(kwargs.get("max_tokens", 16000), 16000),
            "messages": [
                *kwargs["messages"],
                {"role": "assistant", "content": partial},
                {
                    "role": "user",
                    "content": (
                        "The connection dropped and your reply was cut off mid-way. "
                        "Continue from exactly where it stopped and finish the JSON "
                        "object. Output only the remaining characters - do not repeat "
                        "anything you already sent, and do not restart."
                    ),
                },
            ],
        }
        tail = self.client.messages.create(**tail_kwargs)
        self._record(tail)
        continuation = "".join(
            getattr(block, "text", "") or ""
            for block in tail.content
            if getattr(block, "type", None) == "text"
        )
        # The continuation inherits a system prompt that says "reply with a
        # single JSON object", so the model quite often restarts instead of
        # continuing. That restart is a complete answer we have already paid
        # for, so try it on its own as well as stitched. Longest valid wins:
        # a restart carries the whole payload, a true continuation only closes
        # the object it was handed.
        candidates = [
            ("stitched", partial + continuation),
            ("restart", continuation),
            ("partial", partial),
        ]
        best_name, best_text, best_data = None, None, None
        for name, candidate in candidates:
            try:
                data = json.loads(_json_payload(candidate))
            except ValueError:
                continue
            if best_data is None or len(candidate) > len(best_text or ""):
                best_name, best_text, best_data = name, candidate, data
        if best_text is None:
            raise RuntimeError(
                "Stream was cut and neither the continuation nor the fragment "
                "closed the JSON object"
            )
        _LOG.info(
            "stream cut salvaged via %s (partial %d chars + continuation %d chars)",
            best_name,
            len(partial),
            len(continuation),
        )
        return _StitchedMessage(
            text=best_text,
            model=self.model,
            message_id=tail.id,
            # Carry the tail's own stop_reason so a continuation that itself
            # ran out of room is still caught by the max_tokens guard.
            reason=getattr(tail, "stop_reason", None) or "end_turn",
        )


    def _to_response(self, message: Any, *, strategy: str, schema_name: str) -> ModelResponse:
        stop_reason = getattr(message, "stop_reason", None)
        # A policy decline arrives as HTTP 200; check before reading content.
        if stop_reason == "refusal":
            raise RuntimeError(
                f"Claude declined the request: {getattr(message, 'stop_details', None)}"
            )
        if stop_reason == "max_tokens":
            raise RuntimeError(
                "Claude response hit max_tokens before completing the JSON payload"
            )
        if strategy == "tool":
            for block in message.content:
                if getattr(block, "type", None) == "tool_use" and block.name == schema_name:
                    # Tool input arrives as a parsed object, not a JSON string.
                    return ModelResponse(
                        data=dict(block.input),
                        response_id=message.id,
                        model_snapshot=getattr(message, "model", None) or self.model,
                    )
            raise RuntimeError("Claude returned no tool_use block for the requested schema")
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise RuntimeError("Claude returned no text block")
        return ModelResponse(
            data=json.loads(_json_payload(text)),
            response_id=message.id,
            model_snapshot=getattr(message, "model", None) or self.model,
        )
