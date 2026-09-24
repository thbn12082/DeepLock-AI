from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPO_ROOT / "schemas"
PROMPT_ROOT = REPO_ROOT / "prompts"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def stable_id(prefix: str, *parts: str, length: int = 16) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def curriculum_illustration_id(
    *,
    content_id_seed: str,
    lecture_id: str,
    page_number: int,
    occurrence_seed: str | None = None,
) -> str:
    """Keep legacy page IDs while disambiguating multiple media occurrences on one page."""

    parts = [content_id_seed, lecture_id, str(page_number)]
    if occurrence_seed:
        parts.append(occurrence_seed)
    return stable_id("illustration", *parts)


def resolve_content_id_seed(source_hash: str, curriculum: dict[str, Any] | None = None) -> str:
    """Return the immutable seed used for user-facing content identities.

    Source provenance must use the current corpus hash, but adding an audited
    page supplement should not renumber every unrelated lesson and invalidate
    otherwise reusable generation cache. Curricula may therefore pin the
    identity seed that was used when their logical content graph was created.
    """

    raw_seed = curriculum.get("stable_content_id_seed") if curriculum else None
    seed = str(raw_seed or source_hash).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", seed):
        raise ValueError("stable_content_id_seed must be a lowercase sha256 digest")
    return seed


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def safe_filename(value: str) -> str:
    name = Path(value).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_finite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Non-finite number at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_finite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            ensure_finite(item, f"{path}[{index}]")


def load_schema(name: str) -> dict[str, Any]:
    return read_json(SCHEMA_ROOT / name)


def inline_schema_refs(schema: dict[str, Any], base: Path | None = None) -> dict[str, Any]:
    """Inline local schema refs because Responses strict schemas must be self-contained."""
    base = base or SCHEMA_ROOT

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$ref"} and not value["$ref"].startswith("#"):
                target = read_json(base / value["$ref"])
                target.pop("$schema", None)
                target.pop("$id", None)
                target.pop("title", None)
                return visit(target)
            return {key: visit(item) for key, item in value.items() if key not in {"$schema", "$id"}}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(schema)


def responses_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a local contract onto the Responses Structured Outputs subset.

    The full local JSON Schema and Pydantic model still validate every generated
    value. OpenAI strict schemas currently reject ``uniqueItems``, so it belongs
    in the deterministic gate rather than the wire schema.
    """

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: visit(item)
                for key, item in value.items()
                if key not in {"$schema", "$id", "uniqueItems"}
            }
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(inline_schema_refs(schema))
