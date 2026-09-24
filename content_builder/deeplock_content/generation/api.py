from __future__ import annotations

import json
import random
import time
from contextlib import contextmanager, nullcontext
from threading import BoundedSemaphore
from dataclasses import dataclass
from typing import Any, Literal

from openai import OpenAI

from ..settings import Settings


DEFAULT_OUTPUT_VERBOSITY: Literal["low", "medium", "high"] = "high"

_shared_request_slots: BoundedSemaphore | None = None


@contextmanager
def shared_request_budget(limit: int | None):
    """Limit live HTTP calls across every lecture, including vision and retries."""
    global _shared_request_slots
    previous = _shared_request_slots
    if limit is not None:
        if limit < 1 or previous is not None:
            raise ValueError("Request budget must be positive and cannot be nested")
        _shared_request_slots = BoundedSemaphore(limit)
    try:
        yield
    finally:
        _shared_request_slots = previous


@dataclass(frozen=True)
class ModelResponse:
    data: dict[str, Any]
    response_id: str
    model_snapshot: str


class ResponsesAdapter:
    """Responses API adapter pinned to 9router local for DeepLock prebuild calls."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.api_key or "9router-local-no-auth",
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )

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
        if output_verbosity not in {"low", "medium", "high"}:
            raise ValueError(f"Unsupported Responses output verbosity: {output_verbosity}")
        transport_model = self.settings.transport_model(logical_model, reviewer=reviewer)
        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_api_attempts + 1):
            try:
                content: list[dict[str, Any]] = [{"type": "input_text", "text": input_text}]
                content.extend(
                    {"type": "input_image", "image_url": image_url, "detail": "high"}
                    for image_url in (input_image_data_urls or [])
                )
                request: dict[str, Any] = dict(
                    model=transport_model,
                    reasoning={"effort": reasoning_effort},
                    instructions=instructions,
                    input=[
                        {
                            "role": "user",
                            "content": content,
                        }
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        },
                        "verbosity": output_verbosity,
                    },
                    store=False,
                )
                if self.settings.service_tier:
                    request["service_tier"] = self.settings.service_tier
                with _shared_request_slots if _shared_request_slots is not None else nullcontext():
                    response = self.client.responses.create(**request)
                status = getattr(response, "status", None)
                if status not in {None, "completed"}:
                    details = getattr(response, "incomplete_details", None)
                    raise RuntimeError(f"Responses API returned status={status}: {details}")
                output_text = response.output_text
                if not output_text:
                    raise RuntimeError("Responses API returned empty/refused structured output")
                return ModelResponse(
                    data=json.loads(output_text),
                    response_id=response.id,
                    model_snapshot=getattr(response, "model", None) or transport_model,
                )
            except Exception as error:  # SDK exposes multiple transient subclasses.
                last_error = error
                if attempt >= self.settings.max_api_attempts:
                    break
                delay = min(8.0, 0.75 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.35)
                time.sleep(delay)
        raise RuntimeError(f"9router Responses request failed after retries: {last_error}") from last_error
