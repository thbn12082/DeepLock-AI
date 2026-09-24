from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "tmp" / "run_course_v3_batch.py"
SPEC = importlib.util.spec_from_file_location("run_course_v3_batch", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


def _plan(count: int = 5) -> dict:
    return {
        "source_hash": "sha256:" + "a" * 64,
        "lectures": [
            {"lecture_id": f"lec_{index}", "title": f"Lecture {index}"}
            for index in range(1, count + 1)
        ],
    }


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
    *,
    active_priorities: set[int],
    usage: dict | None = None,
    usages: dict[int, dict] | None = None,
    calls: list[tuple[str, str | None]] | None = None,
):
    calls = calls if calls is not None else []
    connections = [
        {
            "id": f"codex-{priority}",
            "provider": "codex",
            "priority": priority,
            "isActive": priority in active_priorities,
        }
        for priority in (1, 7, 9)
    ]
    usage_by_priority = usages or {
        priority: usage or _usage()
        for priority in (1, 7, 9)
    }

    def open_request(request, *, timeout: int):
        assert timeout == 5
        calls.append((request.full_url, request.get_header("X-9r-cli-token")))
        if request.full_url.endswith("/api/providers"):
            return FakeResponse({"connections": connections})
        for priority, payload in usage_by_priority.items():
            if f"/api/usage/codex-{priority}?force=1" in request.full_url:
                return FakeResponse(payload)
        raise AssertionError(f"Unexpected URL: {request.full_url}")

    return open_request


def test_range_selection_is_one_based_inclusive_and_explicit_ids_keep_requested_order():
    plan = _plan()

    selected = runner._select_lectures(
        plan,
        start=2,
        end=4,
        lecture_ids=[],
    )
    explicit = runner._select_lectures(
        plan,
        start=None,
        end=None,
        lecture_ids=["lec_5", "lec_1"],
    )

    assert selected.lecture_ids == ("lec_2", "lec_3", "lec_4")
    assert selected.catalog_indices == (2, 3, 4)
    assert explicit.lecture_ids == ("lec_5", "lec_1")
    assert explicit.catalog_indices == (5, 1)


@pytest.mark.parametrize(
    ("start", "end", "lecture_ids", "message"),
    [
        (None, None, [], "exactly one mode"),
        (1, 2, ["lec_1"], "exactly one mode"),
        (1, None, [], "requires both"),
        (4, 2, [], "cannot be greater"),
        (1, 6, [], "exceeds the catalog size"),
        (None, None, ["lec_1", "lec_1"], "must be unique"),
        (None, None, ["lec_missing"], "Unknown lecture IDs"),
    ],
)
def test_selection_fails_closed_for_ambiguous_or_invalid_batches(
    start: int | None,
    end: int | None,
    lecture_ids: list[str],
    message: str,
):
    with pytest.raises(ValueError, match=message):
        runner._select_lectures(
            _plan(),
            start=start,
            end=end,
            lecture_ids=lecture_ids,
        )


def test_cli_defaults_to_twenty_requests_and_caps_at_the_dial():
    args = runner._parse_args(["--start", "1", "--end", "2"])

    assert args.concurrency == 20
    assert args.codex_priorities == [7]
    assert args.max_atoms_per_request == 1

    round_robin = runner._parse_args([
        "--start", "1", "--end", "2",
        "--codex-priority", "2",
        "--codex-priority", "3",
    ])
    assert round_robin.codex_priorities == [2, 3]

    with pytest.raises(SystemExit):
        runner._parse_args(
            [
                "--start", "1", "--end", "2",
                "--concurrency", str(runner.MAX_CONCURRENCY + 1),
            ]
        )
    with pytest.raises(SystemExit):
        runner._parse_args([
            "--start", "1", "--end", "2",
            "--codex-priority", "7",
            "--codex-priority", "7",
        ])


def test_preflight_accepts_only_the_selected_active_route_and_exposes_no_secret():
    calls: list[tuple[str, str | None]] = []

    result = runner._router_preflight(
        SimpleNamespace(base_url="http://127.0.0.1:20128/v1"),
        selected_priorities=[7],
        opener=_opener(active_priorities={7}, calls=calls),
        cli_token="top-secret-test-token",
    )

    assert result == {
        "selected_priorities": [7],
        "configured_codex_routes": 3,
        "active_codex_routes": 1,
        "quota_checked": 1,
        "generator_ready": 1,
        "learner_rewrite_ready": 1,
        "source_review_ready": 1,
        "session_unlimited_routes": 0,
        "minimum_session_remaining": 100.0,
    }
    assert len(calls) == 2
    assert all(token == "top-secret-test-token" for _url, token in calls)
    assert "top-secret-test-token" not in json.dumps(result)
    assert "codex-7" not in json.dumps(result)


def test_preflight_rejects_round_robin_with_an_unselected_active_route_before_quota_call():
    calls: list[tuple[str, str | None]] = []

    with pytest.raises(RuntimeError, match="unselected active Codex routes"):
        runner._router_preflight(
            SimpleNamespace(base_url="http://localhost:20128/v1"),
            selected_priorities=[7],
            opener=_opener(active_priorities={7, 9}, calls=calls),
            cli_token="test-token",
        )

    assert len(calls) == 1


def test_preflight_rejects_exhausted_selected_route():
    with pytest.raises(RuntimeError, match="live quota is unavailable"):
        runner._router_preflight(
            SimpleNamespace(base_url="http://127.0.0.1:20128/v1"),
            selected_priorities=[7],
            opener=_opener(active_priorities={7}, usage=_usage(remaining=0)),
            cli_token="test-token",
        )


def test_preflight_accepts_every_selected_round_robin_route_and_checks_each_quota():
    calls: list[tuple[str, str | None]] = []

    result = runner._router_preflight(
        SimpleNamespace(base_url="http://127.0.0.1:20128/v1"),
        selected_priorities=[7, 9],
        opener=_opener(active_priorities={7, 9}, calls=calls),
        cli_token="test-token",
    )

    assert result["selected_priorities"] == [7, 9]
    assert result["active_codex_routes"] == 2
    assert result["quota_checked"] == 2
    assert result["learner_rewrite_ready"] == 2
    assert len(calls) == 3


def test_preflight_does_not_block_learner_rewrite_on_unused_source_review_quota():
    result = runner._router_preflight(
        SimpleNamespace(base_url="http://127.0.0.1:20128/v1"),
        selected_priorities=[7],
        opener=_opener(
            active_priorities={7},
            usage=_usage(review_limit_reached=True),
        ),
        cli_token="test-token",
    )

    assert result["learner_rewrite_ready"] == 1
    assert result["source_review_ready"] == 0


def test_course_v3_curriculum_uses_granular_semantic_identity():
    curriculum = {
        "outline_mode": "DETERMINISTIC",
        "deterministic_outline": True,
        "atoms_per_module": 5,
        "pages_per_part": 99,
        "target_pages_per_atom": 9,
        "max_atoms_per_part": 5,
        "content_identity_namespace": "v2",
        "batch_atoms_by_module": False,
        "modules": [],
    }

    runner._apply_course_v3_curriculum(curriculum)

    assert curriculum == {
        "outline_mode": "SEMANTIC_V2",
        "pages_per_part": 20,
        "target_pages_per_atom": 2,
        "max_atoms_per_part": 10,
        "content_identity_namespace": "v3",
        "batch_atoms_by_module": True,
        "modules": [],
    }


def test_effort_accepts_luna_none_mode(monkeypatch):
    monkeypatch.setenv("OPENAI_GENERATOR_REASONING_EFFORT", "none")

    assert runner._effort("OPENAI_GENERATOR_REASONING_EFFORT", "medium") == "none"


def test_course_v3_settings_explicitly_enable_single_learner_rewrite_pass(monkeypatch):
    base = runner.Settings(
        api_key="fixture-key",
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="medium",
        review_reasoning_effort="high",
        generator_prompt_version="course-v2",
        review_prompt_version="reviewer-v2",
        max_repair_rounds=2,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )
    monkeypatch.setattr(runner.Settings, "from_env", classmethod(lambda _cls: base))

    settings = runner._course_v3_settings(concurrency=20, max_atoms_per_request=1)

    assert settings.editorial_rewrite_enabled is True
    assert settings.editorial_model == "gpt-5.5"
    assert settings.editorial_reasoning_effort == "medium"
    assert settings.editorial_prompt_version == "learner-rewrite-v1"
    # The editorial group size is now a request-budget dial, defaulting to the
    # Settings maximum so a lecture costs about 60% fewer editorial requests.
    assert settings.max_atoms_per_editorial_call == runner.DEFAULT_EDITORIAL_GROUP_SIZE
    assert runner.DEFAULT_EDITORIAL_GROUP_SIZE == 5

    narrow = runner._course_v3_settings(
        concurrency=20, max_atoms_per_request=1, editorial_group_size=2
    )
    assert narrow.max_atoms_per_editorial_call == 2
    assert settings.concurrency == 20


def test_isolated_workspace_seeds_cache_once_and_preserves_resume_state(tmp_path: Path):
    source_root = tmp_path / "legacy" / "work"
    destination_root = tmp_path / "course-v3" / "work"
    lecture_id = "lec_safe"
    source_workspace = source_root / lecture_id
    source_workspace.mkdir(parents=True)
    source_payload = {"source_hash": "sha256:" + "b" * 64, "documents": [], "chunks": []}
    source_curriculum = {
        "outline_mode": "DETERMINISTIC",
        "deterministic_outline": True,
        "atoms_per_module": 5,
        "modules": [{"module_id": "module_1", "illustrations": []}],
    }
    (source_workspace / "source.json").write_text(
        json.dumps(source_payload), encoding="utf-8"
    )
    (source_workspace / "curriculum.json").write_text(
        json.dumps(source_curriculum), encoding="utf-8"
    )
    media = source_workspace / "media"
    media.mkdir()
    (media / "diagram.png").write_bytes(b"safe-image")
    (source_workspace / "vision-audit.json").write_text(
        json.dumps({"cached": True}), encoding="utf-8"
    )
    with sqlite3.connect(source_workspace / "cache.sqlite3") as connection:
        connection.execute("CREATE TABLE seeded (value TEXT NOT NULL)")
        connection.execute("INSERT INTO seeded VALUES ('legacy-cache')")

    lecture = {
        "lecture_id": lecture_id,
        "title": "Safe lecture",
        "workspace": str(source_workspace),
    }
    materialized, created = runner._materialize_workspace(
        lecture,
        source_work_root=source_root,
        destination_work_root=destination_root,
    )
    destination = Path(materialized["workspace"])

    assert created is True
    assert destination.is_relative_to(destination_root)
    assert json.loads((source_workspace / "curriculum.json").read_text()) == source_curriculum
    isolated_curriculum = json.loads((destination / "curriculum.json").read_text())
    assert isolated_curriculum["content_identity_namespace"] == "v3"
    assert isolated_curriculum["target_pages_per_atom"] == 2
    assert (destination / "vision-audit.json").is_file()
    assert (destination / "media" / "diagram.png").read_bytes() == b"safe-image"
    with sqlite3.connect(destination / "cache.sqlite3") as connection:
        assert connection.execute("SELECT value FROM seeded").fetchone()[0] == "legacy-cache"
        connection.execute("INSERT INTO seeded VALUES ('course-v3-progress')")
    isolated_curriculum["modules"][0]["page_text_overrides"] = {"3": "audited"}
    (destination / "curriculum.json").write_text(
        json.dumps(isolated_curriculum), encoding="utf-8"
    )

    resumed, created_again = runner._materialize_workspace(
        lecture,
        source_work_root=source_root,
        destination_work_root=destination_root,
    )

    assert created_again is False
    assert resumed["workspace"] == materialized["workspace"]
    with sqlite3.connect(destination / "cache.sqlite3") as connection:
        values = [row[0] for row in connection.execute("SELECT value FROM seeded ORDER BY rowid")]
    assert values == ["legacy-cache", "course-v3-progress"]
    resumed_curriculum = json.loads((destination / "curriculum.json").read_text())
    assert resumed_curriculum["modules"][0]["page_text_overrides"] == {"3": "audited"}


def test_quota_failure_classification_reads_partial_report(tmp_path: Path):
    report = tmp_path / "catalog-build-report.json"
    report.write_text(
        json.dumps({
            "failures": [{"error": "9router request failed: HTTP 429 rate_limit"}]
        }),
        encoding="utf-8",
    )

    assert runner._is_quota_failure(report)
    assert not runner._is_quota_failure(error=RuntimeError("schema validation failed"))


def test_question_id_containing_429_is_not_misclassified_as_quota(tmp_path: Path):
    report = tmp_path / "catalog-build-report.json"
    report.write_text(
        json.dumps({
            "failures": [{
                "error": (
                    "Question q_83615a4290f66e43 has an implausible/off-concept "
                    "distractor D"
                )
            }]
        }),
        encoding="utf-8",
    )

    assert not runner._is_quota_failure(report)


def test_normal_incomplete_build_return_is_paused_when_failure_report_says_quota(
    tmp_path: Path,
):
    report = tmp_path / "catalog-build-report.json"
    report.write_text(
        json.dumps({
            "ready": 1,
            "failed": 1,
            "failures": [{"error": "insufficient_quota"}],
        }),
        encoding="utf-8",
    )

    assert runner._incomplete_build_status({"ready": 1, "failed": 1}, report) == (
        "PAUSED_QUOTA",
        75,
    )
    assert runner._incomplete_build_status({"ready": 1, "failed": 0, "staged": 1}, report) == (
        "FAILED",
        1,
    )
