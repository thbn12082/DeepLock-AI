from __future__ import annotations

from deeplock_content.cache import BuildCache


def test_cache_migration_and_round_trip(tmp_path):
    cache = BuildCache(tmp_path / "cache.sqlite3")
    values = {
        "cache_key": "key", "step_type": "GENERATE_OUTLINE", "input_hash": "hash",
        "prompt_version": "course-v1", "logical_model": "gpt-5.5", "model_snapshot": "cx/gpt-5.5",
        "schema_version": "1", "status": "READY", "attempt_count": 1, "error_code": None,
        "response_id": "resp", "output_json": {"ok": True},
    }
    cache.record_attempt("generation_steps", values)
    assert cache.get("generation_steps", "key")["output"] == {"ok": True}
    assert cache.connection.execute("PRAGMA user_version").fetchone()[0] == 1
    cache.close()
