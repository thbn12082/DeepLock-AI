from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from deeplock_content.generation.api import ResponsesAdapter
from deeplock_content.generation.api import shared_request_budget


def test_shared_budget_limits_multiple_adapters_and_releases_failed_calls():
    lock = threading.Lock()
    active = peak = count = 0

    def create(**kwargs):
        nonlocal active, peak, count
        with lock:
            active += 1
            peak = max(peak, active)
            count += 1
            number = count
        try:
            time.sleep(0.02)
            if number == 1:
                raise RuntimeError("fixture failure")
            return SimpleNamespace(status="completed", output_text='{"ok":true}', id=str(number), model="fixture")
        finally:
            with lock:
                active -= 1

    adapters = [ResponsesAdapter(_settings()) for _ in range(2)]
    for adapter in adapters:
        adapter.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with shared_request_budget(3), ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(_call, adapters[i % 2]) for i in range(12)]
        errors = sum(f.exception() is not None for f in futures)
    assert errors == 1
    assert count == 12
    assert peak == 3
    assert active == 0
from deeplock_content.settings import MAX_ROUTER_CONCURRENCY, Settings


class _FakeResponses:
    def __init__(self):
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output_text='{"ok":true}',
            id=f"response_{len(self.requests)}",
            model=str(kwargs["model"]),
        )


def _settings() -> Settings:
    return Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=5,
        generator_reasoning_effort="medium",
        review_reasoning_effort="high",
        generator_prompt_version="course-v1",
        review_prompt_version="reviewer-v1",
        max_repair_rounds=2,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )


def _call(adapter: ResponsesAdapter, **kwargs):
    return adapter.structured(
        logical_model="gpt-5.5",
        reviewer=bool(kwargs.pop("reviewer", False)),
        reasoning_effort="medium",
        instructions="Return JSON.",
        input_text="Input",
        schema_name="fixture",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        **kwargs,
    )


def test_structured_generation_defaults_to_high_output_verbosity():
    adapter = ResponsesAdapter(_settings())
    fake = _FakeResponses()
    adapter.client = SimpleNamespace(responses=fake)
    response = _call(adapter)
    assert response.data == {"ok": True}
    assert fake.requests[0]["text"]["verbosity"] == "high"


def test_structured_reviewer_can_explicitly_request_low_output_verbosity():
    adapter = ResponsesAdapter(_settings())
    fake = _FakeResponses()
    adapter.client = SimpleNamespace(responses=fake)
    _call(adapter, reviewer=True, output_verbosity="low")
    assert fake.requests[0]["text"]["verbosity"] == "low"


def test_structured_request_forwards_configured_service_tier():
    adapter = ResponsesAdapter(replace(_settings(), service_tier="priority"))
    fake = _FakeResponses()
    adapter.client = SimpleNamespace(responses=fake)

    _call(adapter)

    assert fake.requests[0]["service_tier"] == "priority"


def test_structured_request_omits_unconfigured_service_tier():
    adapter = ResponsesAdapter(_settings())
    fake = _FakeResponses()
    adapter.client = SimpleNamespace(responses=fake)

    _call(adapter)

    assert "service_tier" not in fake.requests[0]


def test_generation_group_limit_defaults_to_two_and_clamps_env(tmp_path, monkeypatch):
    assert _settings().max_atoms_per_generation_call == 2
    monkeypatch.setenv("OPENAI_GENERATOR_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_MAX_ATOMS_PER_GENERATION_CALL", "99")

    settings = Settings.from_env(tmp_path / "missing.env")

    assert settings.max_atoms_per_generation_call == 5


def test_editorial_group_limit_defaults_to_two_and_clamps_env(tmp_path, monkeypatch):
    assert _settings().max_atoms_per_editorial_call == 2
    assert replace(
        _settings(),
        max_atoms_per_editorial_call=0,
    ).max_atoms_per_editorial_call == 1
    monkeypatch.setenv("OPENAI_GENERATOR_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_EDITORIAL_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_MAX_ATOMS_PER_EDITORIAL_CALL", "99")

    settings = Settings.from_env(tmp_path / "missing.env")

    assert settings.max_atoms_per_editorial_call == 5


def test_router_defaults_allow_verified_two_requests_per_active_account(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_GENERATOR_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "gpt-5.5")
    monkeypatch.delenv("NINEROUTER_CONCURRENCY", raising=False)
    monkeypatch.delenv("OPENAI_REQUEST_TIMEOUT_SECONDS", raising=False)

    settings = Settings.from_env(tmp_path / "missing.env")

    assert settings.concurrency == 10
    assert settings.request_timeout_seconds == 1200


def test_service_tier_is_read_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_GENERATOR_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_EDITORIAL_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_SERVICE_TIER", "priority")

    settings = Settings.from_env(tmp_path / "missing.env")

    assert settings.service_tier == "priority"


def test_router_concurrency_is_hard_clamped_to_the_dial(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_GENERATOR_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "gpt-5.5")
    monkeypatch.setenv("NINEROUTER_CONCURRENCY", str(MAX_ROUTER_CONCURRENCY + 50))

    settings = Settings.from_env(tmp_path / "missing.env")

    # Raised from 10, which was measured for gpt-5.5 across five Codex routes
    # and left each lecture running editorial groups one at a time. The
    # default when the variable is unset is still 10.
    assert settings.concurrency == MAX_ROUTER_CONCURRENCY
