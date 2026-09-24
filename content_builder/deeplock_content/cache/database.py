from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1


class BuildCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def migrate(self) -> None:
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"Build cache schema {current} is newer than supported {SCHEMA_VERSION}")
        if current < 1:
            self.connection.executescript(
                """
                CREATE TABLE generation_steps (
                    cache_key TEXT PRIMARY KEY,
                    step_type TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    logical_model TEXT NOT NULL,
                    model_snapshot TEXT,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    response_id TEXT,
                    output_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE ai_reviews (
                    cache_key TEXT PRIMARY KEY,
                    candidate_hash TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    logical_model TEXT NOT NULL,
                    model_snapshot TEXT,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    response_id TEXT,
                    output_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_steps_status ON generation_steps(status, step_type);
                CREATE INDEX idx_reviews_hash ON ai_reviews(candidate_hash, source_hash);
                PRAGMA user_version=1;
                """
            )
            self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def get(self, table: str, cache_key: str) -> dict[str, Any] | None:
        if table not in {"generation_steps", "ai_reviews"}:
            raise ValueError("invalid cache table")
        with self._lock:
            row = self.connection.execute(
                f"SELECT * FROM {table} WHERE cache_key=? AND status='READY'", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["output"] = json.loads(result.pop("output_json"))
        return result

    def get_failed(self, table: str, cache_key: str) -> dict[str, Any] | None:
        """Return one explicit FAILED row without weakening normal cache hits.

        Callers must still validate ``output`` before reusing it.  Keeping this
        separate from :meth:`get` ensures FAILED rows can never become ordinary
        cache hits accidentally.
        """

        if table not in {"generation_steps", "ai_reviews"}:
            raise ValueError("invalid cache table")
        with self._lock:
            row = self.connection.execute(
                f"SELECT * FROM {table} WHERE cache_key=? AND status='FAILED'", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["output"] = json.loads(result.pop("output_json"))
        return result

    def record_attempt(self, table: str, values: dict[str, Any]) -> None:
        if table not in {"generation_steps", "ai_reviews"}:
            raise ValueError("invalid cache table")
        keys = list(values)
        assignments = ",".join(f"{key}=excluded.{key}" for key in keys if key != "cache_key")
        placeholders = ",".join("?" for _ in keys)
        encoded = [json.dumps(value, ensure_ascii=False) if key == "output_json" and value is not None else value for key, value in values.items()]
        with self._lock:
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO {table} ({','.join(keys)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(cache_key) DO UPDATE SET {assignments}, updated_at=CURRENT_TIMESTAMP",
                    encoded,
                )
