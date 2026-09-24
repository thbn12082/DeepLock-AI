"""Pick the model transport for a build.

`gpt-5.5` keeps going through the local 9router exactly as before; a `claude-*`
logical model is routed to the Anthropic Messages API instead. Everything else
in the builder is transport-agnostic, so this is the only place that has to
know the difference.

The Claude path is metered. `CLAUDE_TOKEN_BUDGET` sets a hard ceiling in tokens
for the whole run and `CLAUDE_BUDGET_LEDGER` names the JSON file that carries
the running total between processes, so an interrupted backlog run resumes with
its spend intact instead of starting over at zero.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Protocol

from ..settings import Settings
from .api import ResponsesAdapter

DEFAULT_BUDGET_LEDGER = Path(".build") / "claude" / "token-budget.json"

_budget_lock = threading.Lock()
_budget: Any | None = None


class ModelAdapter(Protocol):
    def structured(self, **kwargs: Any) -> Any: ...


def _int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.replace("_", "").strip())
    except ValueError as error:
        raise ValueError(f"{name} must be an integer number of tokens") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def claude_token_budget() -> Any | None:
    """Process-wide token budget for Claude calls, or None when unmetered."""

    global _budget
    limit = _int_env("CLAUDE_TOKEN_BUDGET")
    if limit is None:
        return None
    with _budget_lock:
        if _budget is None:
            from .claude_api import TokenBudget

            ledger = Path(os.getenv("CLAUDE_BUDGET_LEDGER") or DEFAULT_BUDGET_LEDGER)
            _budget = TokenBudget(ledger, limit_tokens=limit)
        return _budget


def build_adapter(settings: Settings) -> ModelAdapter:
    """Return the adapter that serves this run's logical model."""

    if settings.generator_model.startswith("claude-"):
        # Imported lazily so a gpt-5.5-only checkout never needs the Anthropic SDK.
        from .claude_api import ClaudeAdapter

        return ClaudeAdapter(settings, budget=claude_token_budget())
    return ResponsesAdapter(settings)
