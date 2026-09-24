from __future__ import annotations

from typing import Any, Callable

import pytest

from deeplock_content.cache import BuildCache
from deeplock_content.generation import ModelResponse
from deeplock_content.generation.builder import _cached_call
from deeplock_content.settings import Settings


class _StaticAdapter:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls = 0
        self.inputs: list[str] = []
        self.output_verbosities: list[str] = []

    def structured(self, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        self.inputs.append(kwargs["input_text"])
        self.output_verbosities.append(kwargs["output_verbosity"])
        return ModelResponse(
            data=self.data,
            response_id="resp_structured",
            model_snapshot="cx/gpt-5.5-snapshot",
        )


class _ExplodingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def structured(self, **_kwargs: Any) -> ModelResponse:
        self.calls += 1
        raise RuntimeError("transport unavailable")


class _NoCallAdapter:
    def structured(self, **_kwargs: Any) -> ModelResponse:
        raise AssertionError("a reusable cached response must not call the adapter")


def _settings() -> Settings:
    return Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="medium",
        review_reasoning_effort="high",
        generator_prompt_version="course-v2",
        review_prompt_version="reviewer-v2",
        max_repair_rounds=0,
        request_timeout_seconds=1,
        max_api_attempts=1,
    )


def _call(
    *,
    cache: BuildCache,
    adapter: Any,
    validator: Callable[[dict[str, Any]], None],
    resume: bool = True,
    include_invalid_output_in_correction: bool = False,
    correction_output_verbosity: str | None = None,
    correction_context: dict | None = None,
) -> ModelResponse:
    return _cached_call(
        cache=cache,
        adapter=adapter,
        settings=_settings(),
        step_type="TEST_DURABLE_STRUCTURED_OUTPUT",
        input_hash="sha256:test-input",
        prompt_version="test-v1",
        logical_model="gpt-5.5",
        instructions="Return the requested object.",
        input_text="Test input",
        schema_name="durable_output",
        schema={"type": "object", "additionalProperties": True},
        resume=resume,
        output_validator=validator,
        include_invalid_output_in_correction=include_invalid_output_in_correction,
        correction_output_verbosity=correction_output_verbosity,
        correction_context=correction_context,
    )


def _only_generation_key(cache: BuildCache) -> str:
    row = cache.connection.execute(
        "SELECT cache_key FROM generation_steps"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_validator_rejected_payload_is_preserved_and_promoted_after_validator_fix(tmp_path):
    cache = BuildCache(tmp_path / "cache.sqlite3")
    payload = {"value": "preserve this structured response"}
    adapter = _StaticAdapter(payload)

    def old_validator(_data: dict[str, Any]) -> None:
        raise ValueError("old validator bug")

    with pytest.raises(ValueError, match="old validator bug"):
        _call(cache=cache, adapter=adapter, validator=old_validator)

    key = _only_generation_key(cache)
    failed = cache.get_failed("generation_steps", key)
    assert failed is not None
    assert failed["output"] == payload
    assert failed["error_code"] == "OUTPUT_VALIDATION_FAILED"
    assert failed["response_id"] == "resp_structured"
    assert failed["model_snapshot"] == "cx/gpt-5.5-snapshot"
    assert cache.get("generation_steps", key) is None

    def fixed_validator(data: dict[str, Any]) -> None:
        if data != payload:
            raise ValueError("unexpected output")

    response = _call(
        cache=cache,
        adapter=_NoCallAdapter(),
        validator=fixed_validator,
    )

    assert response.data == payload
    assert response.response_id == "resp_structured"
    assert response.model_snapshot == "cx/gpt-5.5-snapshot"
    assert cache.get_failed("generation_steps", key) is None
    assert cache.get("generation_steps", key)["output"] == payload
    cache.close()


def test_failed_payload_is_revalidated_and_regenerated_when_still_invalid(tmp_path):
    cache = BuildCache(tmp_path / "cache.sqlite3")

    def validator(data: dict[str, Any]) -> None:
        if data.get("valid") is not True:
            raise ValueError("valid must be true")

    with pytest.raises(ValueError, match="valid must be true"):
        _call(
            cache=cache,
            adapter=_StaticAdapter({"valid": False, "value": "old"}),
            validator=validator,
        )

    replacement_adapter = _StaticAdapter({"valid": True, "value": "new"})
    response = _call(
        cache=cache,
        adapter=replacement_adapter,
        validator=validator,
        include_invalid_output_in_correction=True,
        correction_output_verbosity="low",
        correction_context={"module_id": "canonical-module", "ordered_atom_ids": ["canonical-atom"]},
    )

    assert replacement_adapter.calls == 1
    assert "CORRECTION REQUIRED" in replacement_adapter.inputs[0]
    assert "PREVIOUS_INVALID_STRUCTURED_RESPONSE" in replacement_adapter.inputs[0]
    assert '"valid": false' in replacement_adapter.inputs[0]
    assert '"value": "old"' in replacement_adapter.inputs[0]
    assert "Test input" not in replacement_adapter.inputs[0]
    assert '"module_id": "canonical-module"' in replacement_adapter.inputs[0]
    assert '"ordered_atom_ids": ["canonical-atom"]' in replacement_adapter.inputs[0]
    assert replacement_adapter.output_verbosities == ["low"]
    assert response.data == {"valid": True, "value": "new"}
    cache.close()


def test_transport_failure_stays_a_message_and_is_never_promoted_as_model_output(tmp_path):
    cache = BuildCache(tmp_path / "cache.sqlite3")
    transport_adapter = _ExplodingAdapter()

    with pytest.raises(RuntimeError, match="transport unavailable"):
        _call(cache=cache, adapter=transport_adapter, validator=lambda _data: None)

    key = _only_generation_key(cache)
    failed = cache.get_failed("generation_steps", key)
    assert failed is not None
    assert failed["output"] == {"message": "transport unavailable"}
    assert failed["error_code"] == "OPENAI_REQUEST_FAILED"
    assert failed["response_id"] is None

    replacement_adapter = _StaticAdapter({"valid": True})
    response = _call(
        cache=cache,
        adapter=replacement_adapter,
        # Intentionally permissive: the transport message must still be
        # ineligible for promotion because it was not a model response.
        validator=lambda _data: None,
    )

    assert replacement_adapter.calls == 1
    assert response.data == {"valid": True}
    cache.close()
