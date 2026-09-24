from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "tmp" / "run_course_v2_pilot.py"
SPEC = importlib.util.spec_from_file_location("run_course_v2_pilot", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


TARGET_PRIORITY = next(iter(pilot.PILOT_CODEX_PRIORITIES))


def _connections(*, active: set[int], locks: dict[int, dict] | None = None) -> list[dict]:
    locks = locks or {}
    return [
        {
            "id": f"codex-{priority}",
            "provider": "codex",
            "priority": priority,
            "isActive": priority in active,
            **locks.get(priority, {}),
        }
        for priority in sorted(pilot.PILOT_CODEX_PRIORITIES)
    ]


def _usage(*, remaining: int = 100, review_limit_reached: bool = False) -> dict:
    return {
        "limitReached": remaining <= 0,
        "reviewLimitReached": review_limit_reached,
        "quotas": {
            "session": {
                "remaining": remaining,
                "resetAt": "2026-08-31T16:41:35.000Z",
                "unlimited": False,
            }
        },
    }


def _opener(
    payload: dict,
    calls: list[tuple[str, int]],
    *,
    usages: dict[int, dict] | None = None,
):
    usages = usages or {
        priority: _usage()
        for priority in pilot.PILOT_CODEX_PRIORITIES
    }

    def open_request(request, *, timeout: int):
        assert request.get_header("X-9r-cli-token") == "test-cli-token"
        calls.append((request.full_url, timeout))
        if request.full_url.endswith("/api/providers"):
            return FakeResponse(payload)
        for priority, usage in usages.items():
            if f"/api/usage/codex-{priority}?force=1" in request.full_url:
                return FakeResponse(usage)
        raise AssertionError(f"Unexpected preflight URL: {request.full_url}")

    return open_request


def test_preflight_checks_selected_account_but_starts_no_workers_when_it_is_inactive():
    calls: list[tuple[str, int]] = []
    payload = {"connections": _connections(active=set())}

    with pytest.raises(RuntimeError, match=r"active=0/1.*no generation workers"):
        pilot._router_preflight(
            SimpleNamespace(base_url="http://127.0.0.1:20128/v1"),
            opener=_opener(payload, calls),
            cli_token="test-cli-token",
        )

    assert calls[0] == ("http://127.0.0.1:20128/api/providers", 5)
    assert len(calls) == 2
    assert {
        url.rsplit("/", 1)[-1].split("?", 1)[0]
        for url, _timeout in calls[1:]
    } == {f"codex-{TARGET_PRIORITY}"}


def test_preflight_rejects_active_routes_when_live_session_quota_is_exhausted():
    calls: list[tuple[str, int]] = []
    payload = {"connections": _connections(active={TARGET_PRIORITY})}
    usages = {TARGET_PRIORITY: _usage(remaining=0)}

    with pytest.raises(
        RuntimeError,
        match=r"active=1/1, generator_ready=0/1, reviewer_ready=0/1",
    ):
        pilot._router_preflight(
            SimpleNamespace(base_url="http://localhost:20128/v1"),
            opener=_opener(payload, calls, usages=usages),
            cli_token="test-cli-token",
        )

    assert len(calls) == 2


def test_preflight_accepts_selected_active_account_with_live_quota_for_both_models():
    calls: list[tuple[str, int]] = []
    payload = {"connections": _connections(active={TARGET_PRIORITY})}
    usages = {TARGET_PRIORITY: _usage(remaining=100)}

    availability = pilot._router_preflight(
        SimpleNamespace(base_url="http://127.0.0.1:20128/v1"),
        opener=_opener(payload, calls, usages=usages),
        cli_token="test-cli-token",
    )

    assert availability == {
        "routes": 1,
        "active": 1,
        "quota_checked": 1,
        "generator_ready": 1,
        "reviewer_ready": 1,
    }
    assert len(calls) == 2


def test_router_origin_rejects_a_localhost_lookalike_url():
    with pytest.raises(RuntimeError, match="local 9router"):
        pilot._router_origin("https://example.invalid/v1?next=localhost:20128")


def test_router_cli_token_matches_official_machine_secret_derivation(tmp_path: Path):
    data_dir = tmp_path / "9router"
    (data_dir / "auth").mkdir(parents=True)
    (data_dir / "machine-id").write_text("machine-123\n", encoding="utf-8")
    (data_dir / "auth" / "cli-secret").write_text("secret-456\n", encoding="utf-8")
    expected = hashlib.sha256(b"machine-1239r-cli-authsecret-456").hexdigest()[:16]

    assert pilot._router_cli_token(data_dir=data_dir) == expected


def test_canary_stage_forces_one_worker_while_full_stage_allows_twenty():
    assert pilot._parse_pilot_stage(None) == "canary"
    assert pilot._pilot_concurrency("canary", None) == 1
    assert pilot._pilot_concurrency("full", None) == 20
    assert pilot._pilot_concurrency("full", "7") == 7

    with pytest.raises(RuntimeError, match="Canary stage requires"):
        pilot._pilot_concurrency("canary", "20")
    with pytest.raises(RuntimeError, match="must be 'canary' or 'full'"):
        pilot._parse_pilot_stage("turbo")


def test_pilot_atom_request_width_is_strictly_bounded_from_one_to_five():
    assert pilot._pilot_max_atoms_per_generation_call(None) == 2
    assert pilot._pilot_max_atoms_per_generation_call("1") == 1
    assert pilot._pilot_max_atoms_per_generation_call("5") == 5

    for invalid in ("0", "6", "not-an-integer"):
        with pytest.raises(
            RuntimeError,
            match="COURSE_V2_PILOT_MAX_ATOMS_PER_GENERATION_CALL",
        ):
            pilot._pilot_max_atoms_per_generation_call(invalid)


def test_course_v2_settings_uses_pilot_atom_request_width(monkeypatch):
    base = pilot.Settings(
        api_key="local-test-key",
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="low",
        review_reasoning_effort="high",
        generator_prompt_version="course-v2",
        review_prompt_version="reviewer-v2",
        max_repair_rounds=0,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )
    monkeypatch.setattr(pilot.Settings, "from_env", lambda: base)
    monkeypatch.setenv("COURSE_V2_PILOT_MAX_ATOMS_PER_GENERATION_CALL", "1")

    settings = pilot._course_v2_settings()

    assert settings.max_atoms_per_generation_call == 1


def test_pilot_pins_granular_v3_curriculum_without_legacy_outline_keys():
    curriculum = {
        "outline_mode": "DETERMINISTIC",
        "deterministic_outline": True,
        "atoms_per_module": 5,
        "pages_per_part": 24,
        "target_pages_per_atom": 6,
        "max_atoms_per_part": 5,
        "content_identity_namespace": "v2",
        "batch_atoms_by_module": False,
        "modules": [{"module_id": "lecture_python"}],
    }

    pilot._apply_granular_course_v2_curriculum(curriculum)

    assert curriculum == {
        "outline_mode": "SEMANTIC_V2",
        "pages_per_part": 20,
        "target_pages_per_atom": 2,
        "max_atoms_per_part": 10,
        "content_identity_namespace": "v3",
        "batch_atoms_by_module": True,
        "modules": [{"module_id": "lecture_python"}],
    }


def test_main_preflight_failure_happens_before_writes_vision_or_build(monkeypatch):
    forbidden_calls: list[str] = []
    settings = SimpleNamespace(
        base_url="http://127.0.0.1:20128/v1",
        concurrency=4,
        max_atoms_per_generation_call=2,
    )
    monkeypatch.setattr(pilot, "read_json", lambda _path: {})
    monkeypatch.setattr(pilot, "PILOT_STAGE", "full")
    monkeypatch.setattr(pilot, "_pilot_lectures", lambda _plan: [object()] * 4)
    monkeypatch.setattr(pilot, "_load_curricula", lambda _lectures: [])
    monkeypatch.setattr(pilot, "_course_v2_settings", lambda: settings)
    monkeypatch.setattr(
        pilot,
        "_router_preflight",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("quota unavailable")),
    )
    monkeypatch.setattr(
        pilot,
        "write_json",
        lambda *_args, **_kwargs: forbidden_calls.append("write"),
    )
    monkeypatch.setattr(
        pilot,
        "audit_catalog_visual_pages",
        lambda **_kwargs: forbidden_calls.append("vision"),
    )
    monkeypatch.setattr(
        pilot,
        "build_catalog_packs",
        lambda **_kwargs: forbidden_calls.append("build"),
    )

    with pytest.raises(RuntimeError, match="quota unavailable"):
        pilot.main()

    assert forbidden_calls == []
