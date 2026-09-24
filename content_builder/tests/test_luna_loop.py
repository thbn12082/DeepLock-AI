import importlib
import json
from pathlib import Path


def _loop(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tmp"))
    return importlib.import_module("luna_backlog_loop")


def test_retained_queue_excludes_pruned_and_existing_packs(tmp_path, monkeypatch):
    loop = _loop(monkeypatch)
    monkeypatch.setattr(loop, "ROOT", tmp_path)
    backlog = tmp_path / "backlog.txt"
    backlog.write_text("old\ndropped\nold\n", encoding="utf-8")
    monkeypatch.setattr(loop, "BACKLOG", backlog)
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "packed.dlpack").touch()
    monkeypatch.setattr(loop, "PACKS", packs)
    docs = {
        ".build/course-v3/pruned-selection/pruned-selection.json": {"drop_missing": [{"id": "dropped"}]},
        ".build/course-v3/pruned-selection/packed-low-value.json": [{"id": "low"}],
        ".build/all-lectures/catalog/catalog-plan.json": {"lectures": [{"lecture_id": i} for i in ["new", "old", "packed", "dropped", "low"]]},
    }
    for name, data in docs.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    assert loop._remaining(True) == ["old", "new"]


def test_deterministic_failures_deferred_but_transient_failures_retried(monkeypatch):
    loop = _loop(monkeypatch)
    state = {}
    failures = [
        {"lecture_id": "identity", "error_type": "ValueError", "error": "Editorial rewrite changed module ID x"},
        {"lecture_id": "quota", "error_type": "RuntimeError", "error": "503 quota"},
        {"lecture_id": "frozen", "error_type": "ValueError", "error": "Frozen editorial input has stale illustration identity"},
    ]
    loop._record_failures(state, failures)
    assert not state["identity"]["blocked"]
    assert state["frozen"]["blocked"]
    loop._record_failures(state, failures)
    assert state["identity"]["blocked"]
    assert not state["quota"]["blocked"]
