from __future__ import annotations

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar

from .generation import ResponsesAdapter
from .generation.api import DEFAULT_OUTPUT_VERBOSITY
from .generation.adapters import build_adapter
from .models import ExtractedCorpus
from .settings import Settings
from .util import (
    PROMPT_ROOT,
    normalize_text,
    read_json,
    sha256_bytes,
    sha256_json,
    write_json,
)


_VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pages"],
    "properties": {
        "pages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "page_number",
                    "content_kind",
                    "transcription",
                    "visual_explanation",
                    "useful_illustration",
                    "caption",
                ],
                "properties": {
                    "page_number": {"type": "integer", "minimum": 1},
                    "content_kind": {
                        "type": "string",
                        "enum": ["TECHNICAL", "NONTECHNICAL"],
                    },
                    "transcription": {"type": "string"},
                    "visual_explanation": {"type": "string"},
                    "useful_illustration": {"type": "boolean"},
                    "caption": {"type": "string"},
                },
            },
        }
    },
}
_VISION_PROMPT_PATH = PROMPT_ROOT / "vision-v1" / "system.txt"
_VISION_OUTPUT_VERBOSITY = DEFAULT_OUTPUT_VERBOSITY
_VISION_NONTECHNICAL_REASON_PREFIX = "Vision audit classified this page as NONTECHNICAL"
_VISION_NONTECHNICAL_REASON = (
    f"{_VISION_NONTECHNICAL_REASON_PREFIX}; it contains only cover, agenda, closing, "
    "administrative, or decorative material and no learner-facing technical content."
)


_OOXML_VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["images"],
    "properties": {
        "images": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "image_number",
                    "transcription",
                    "visual_explanation",
                    "useful_illustration",
                    "caption",
                ],
                "properties": {
                    "image_number": {"type": "integer", "minimum": 1, "maximum": 4},
                    "transcription": {"type": "string"},
                    "visual_explanation": {"type": "string"},
                    "useful_illustration": {"type": "boolean"},
                    "caption": {"type": "string"},
                },
            },
        }
    },
}
_OOXML_VISION_PROMPT_PATH = PROMPT_ROOT / "vision-ooxml-v1" / "system.txt"
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


_VisionBatchItem = TypeVar("_VisionBatchItem")
_VisionBatchResult = TypeVar("_VisionBatchResult")


class _SplitVisionBatchError(RuntimeError):
    """Collect terminal leaf failures after preserving successful sibling batches."""

    def __init__(self, failures: list[tuple[tuple[str, ...], Exception]]):
        self.failures = failures
        details = "; ".join(
            f"{','.join(identities)}: {type(error).__name__}: {error}"
            for identities, error in failures
        )
        super().__init__(f"{len(failures)} terminal vision sub-batch failure(s): {details}")


def _run_vision_batch_with_split(
    items: list[_VisionBatchItem],
    *,
    request: Callable[[list[_VisionBatchItem]], _VisionBatchResult],
    persist: Callable[[_VisionBatchResult], None],
    identity: Callable[[_VisionBatchItem], str],
) -> None:
    """Run one batch, recursively bisecting failed requests without nested concurrency.

    ``request`` already includes the transport adapter's own retry policy. Only
    after those retries are exhausted do we bisect the batch. Every successful
    leaf/sub-batch is persisted immediately, so a terminal one-item failure
    cannot discard completed sibling work and resume never needs to repeat it.
    """

    if not items:
        return
    try:
        result = request(items)
    except Exception as initial_error:
        if len(items) == 1:
            raise
        midpoint = len(items) // 2
        failures: list[tuple[tuple[str, ...], Exception]] = []
        for subset in (items[:midpoint], items[midpoint:]):
            try:
                _run_vision_batch_with_split(
                    subset,
                    request=request,
                    persist=persist,
                    identity=identity,
                )
            except _SplitVisionBatchError as error:
                failures.extend(error.failures)
            except Exception as error:
                failures.append((tuple(identity(item) for item in subset), error))
        if failures:
            raise _SplitVisionBatchError(failures) from initial_error
        return
    # Cache/integrity failures are deliberately outside the request exception
    # handler: retrying or splitting a successful API response cannot repair a
    # local persistence failure and could duplicate expensive vision calls.
    persist(result)


@dataclass(frozen=True)
class _OoxmlOccurrence:
    workspace: Path
    module_index: int
    occurrence_id: str
    asset_member: str
    page_number: int
    source_media_sha256: str
    png_sha256: str
    png_path: Path


@dataclass(frozen=True)
class _OoxmlImage:
    png_sha256: str
    png_path: Path


def _page_data_url(pdf_payload: bytes, page_number: int, target_width: int = 1400) -> str:
    import pymupdf

    # Render the immutable byte payload whose digest was checked against the
    # source corpus. Opening the path again here would leave a TOCTOU window in
    # which different PDF bytes could be sent under the cached source hash.
    with pymupdf.open(stream=pdf_payload, filetype="pdf") as document:
        page = document[page_number - 1]
        scale = min(2.5, max(0.75, target_width / max(1.0, float(page.rect.width))))
        payload = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            alpha=False,
        ).tobytes("png")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _validated_pages(data: dict[str, Any], expected_pages: list[int]) -> list[dict[str, Any]]:
    raw_pages = data.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("Vision response has no pages array")
    if any(not isinstance(item, dict) for item in raw_pages):
        raise ValueError("Vision response page item is not an object")
    actual: list[int] = []
    for item in raw_pages:
        page_number = item.get("page_number")
        if type(page_number) is not int or page_number < 1:
            raise ValueError("Vision response has invalid page_number")
        actual.append(page_number)
    if actual != expected_pages:
        raise ValueError(f"Vision response page order mismatch: expected={expected_pages}, actual={actual}")
    output: list[dict[str, Any]] = []
    for item in raw_pages:
        if not isinstance(item, dict):
            raise ValueError("Vision response page item is not an object")
        if item.get("content_kind") not in {"TECHNICAL", "NONTECHNICAL"}:
            raise ValueError(f"Vision response has invalid content_kind on page {item.get('page_number')}")
        if type(item.get("useful_illustration")) is not bool:
            raise ValueError(
                f"Vision response has invalid useful_illustration on page {item.get('page_number')}"
            )
        transcription = normalize_text(str(item.get("transcription") or ""))
        explanation = normalize_text(str(item.get("visual_explanation") or ""))
        if len(normalize_text(f"{transcription}\n{explanation}")) < 10:
            raise ValueError(f"Vision audit is blank for page {item['page_number']}")
        output.append({
            "page_number": int(item["page_number"]),
            "content_kind": str(item["content_kind"]),
            "transcription": transcription,
            "visual_explanation": explanation,
            "useful_illustration": bool(item["useful_illustration"]),
            "caption": normalize_text(str(item.get("caption") or "")),
        })
    return output


def _bounded_text(value: str, *, limit: int = 500) -> str:
    cleaned = normalize_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _validated_ooxml_images(
    data: dict[str, Any],
    expected_png_sha256: list[str],
) -> list[dict[str, Any]]:
    raw_images = data.get("images")
    if not isinstance(raw_images, list):
        raise ValueError("OOXML vision response has no images array")
    expected_numbers = list(range(1, len(expected_png_sha256) + 1))
    actual_numbers: list[int] = []
    for raw in raw_images:
        if not isinstance(raw, dict):
            raise ValueError("OOXML vision response image item is not an object")
        image_number = raw.get("image_number")
        if type(image_number) is not int:
            raise ValueError("OOXML vision response has invalid image_number")
        actual_numbers.append(image_number)
    if actual_numbers != expected_numbers:
        raise ValueError(
            "OOXML vision response image order mismatch: "
            f"expected={expected_numbers}, actual={actual_numbers}"
        )
    output: list[dict[str, Any]] = []
    for png_sha256, raw in zip(expected_png_sha256, raw_images, strict=True):
        useful = raw.get("useful_illustration")
        if type(useful) is not bool:
            raise ValueError(
                f"OOXML vision response has invalid useful_illustration for {png_sha256}"
            )
        for field in ("transcription", "visual_explanation", "caption"):
            if not isinstance(raw.get(field), str):
                raise ValueError(f"OOXML vision response has invalid {field} for {png_sha256}")
        transcription = normalize_text(raw["transcription"])
        explanation = normalize_text(raw["visual_explanation"])
        caption = normalize_text(raw["caption"])
        if useful and not normalize_text(f"{caption}\n{transcription}\n{explanation}"):
            raise ValueError(f"Useful OOXML illustration has no description: {png_sha256}")
        output.append({
            "payload_sha256": png_sha256,
            "transcription": transcription,
            "visual_explanation": explanation,
            "useful_illustration": useful,
            "caption": caption,
        })
    return output


def _ooxml_audit_contract(settings: Settings) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "prompt_sha256": sha256_bytes(_OOXML_VISION_PROMPT_PATH.read_bytes()),
        "response_schema_sha256": sha256_json(_OOXML_VISION_SCHEMA),
        "logical_model": settings.generator_model,
        "transport_model": settings.transport_model(settings.generator_model),
        "reasoning_effort": settings.vision_reasoning_effort,
        "response_verbosity": _VISION_OUTPUT_VERBOSITY,
        "image_detail": "high",
        "batch_size": 4,
    }


def ooxml_audit_contract_sha256(settings: Settings) -> str:
    """Public build-gate fingerprint for the exact OOXML vision contract."""

    return sha256_json(_ooxml_audit_contract(settings))


def _ooxml_result_core(raw: dict[str, Any]) -> dict[str, Any]:
    png_sha256 = raw.get("payload_sha256")
    if not isinstance(png_sha256, str) or not _SHA256_PATTERN.fullmatch(png_sha256):
        raise ValueError("Cached OOXML result has invalid payload_sha256")
    useful = raw.get("useful_illustration")
    if type(useful) is not bool:
        raise ValueError(f"Cached OOXML result has invalid usefulness: {png_sha256}")
    for field in ("transcription", "visual_explanation", "caption", "model_snapshot"):
        if not isinstance(raw.get(field), str):
            raise ValueError(f"Cached OOXML result has invalid {field}: {png_sha256}")
    core: dict[str, Any] = {
        "payload_sha256": png_sha256,
        "transcription": normalize_text(raw["transcription"]),
        "visual_explanation": normalize_text(raw["visual_explanation"]),
        "useful_illustration": useful,
        "caption": normalize_text(raw["caption"]),
        "model_snapshot": normalize_text(raw["model_snapshot"]),
    }
    if not core["model_snapshot"]:
        raise ValueError(f"Cached OOXML result has no model snapshot: {png_sha256}")
    if useful and not normalize_text(
        f"{core['caption']}\n{core['transcription']}\n{core['visual_explanation']}"
    ):
        raise ValueError(f"Cached useful OOXML result has no description: {png_sha256}")
    return core


def _ooxml_cache_body(
    *,
    contract: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "audit_contract": contract,
        "audit_contract_sha256": sha256_json(contract),
        "results": {key: results[key] for key in sorted(results)},
    }


def _write_ooxml_cache(
    cache_path: Path,
    *,
    contract: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> None:
    body = _ooxml_cache_body(contract=contract, results=results)
    write_json(cache_path, {
        **body,
        "cache_integrity_sha256": sha256_json(body),
    })


def _load_ooxml_cache(
    cache_path: Path,
    *,
    contract: dict[str, Any],
    resume: bool,
) -> dict[str, dict[str, Any]]:
    if not resume or not cache_path.is_file():
        return {}
    try:
        cached = read_json(cache_path)
        if not isinstance(cached, dict):
            return {}
        body = {
            key: cached.get(key)
            for key in (
                "schema_version",
                "audit_contract",
                "audit_contract_sha256",
                "results",
            )
        }
        if (
            body["schema_version"] != "1.0"
            or body["audit_contract"] != contract
            or body["audit_contract_sha256"] != sha256_json(contract)
            or cached.get("cache_integrity_sha256") != sha256_json(body)
            or not isinstance(body["results"], dict)
        ):
            return {}
        results: dict[str, dict[str, Any]] = {}
        for key, raw in body["results"].items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                return {}
            core = _ooxml_result_core(raw)
            if key != core["payload_sha256"]:
                return {}
            result = {
                **core,
                "result_sha256": str(raw.get("result_sha256") or ""),
            }
            if result["result_sha256"] != sha256_json(core):
                return {}
            results[key] = result
        return results
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _resolve_ooxml_png(workspace: Path, asset_member: str) -> tuple[Path, str]:
    if not asset_member or "\\" in asset_member:
        raise ValueError(f"Invalid OOXML asset_member: {asset_member!r}")
    media_root = (workspace / "media").resolve()
    unresolved = media_root / Path(asset_member)
    if unresolved.is_symlink():
        raise ValueError(f"OOXML audit asset may not be a symlink: {unresolved}")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(media_root)
    except ValueError as error:
        raise ValueError(f"OOXML audit asset escapes workspace media: {asset_member}") from error
    if not resolved.is_file():
        raise ValueError(f"OOXML audit asset is unavailable: {resolved}")
    payload = resolved.read_bytes()
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"OOXML audit asset is not an exact PNG payload: {resolved}")
    return resolved, sha256_bytes(payload)


def _occurrence_identity(raw: dict[str, Any]) -> tuple[str, str, int, str]:
    occurrence_id = str(raw.get("occurrence_id") or "").strip()
    asset_member = str(raw.get("asset_member") or "").strip()
    page = raw.get("page")
    source_sha256 = str(raw.get("ooxml_media_sha256") or "").strip()
    if not occurrence_id or type(page) is not int or page < 1:
        raise ValueError(f"Invalid OOXML audit occurrence: {occurrence_id!r}")
    if not _SHA256_PATTERN.fullmatch(source_sha256):
        raise ValueError(f"Invalid OOXML media hash for occurrence {occurrence_id}")
    return occurrence_id, asset_member, page, source_sha256


def _collect_ooxml_occurrences(
    *,
    plan_path: Path,
    plan: dict[str, Any],
) -> list[_OoxmlOccurrence]:
    occurrences: list[_OoxmlOccurrence] = []
    seen_ids: set[str] = set()
    for lecture in plan.get("lectures") or []:
        workspace = Path(str(lecture.get("workspace") or ""))
        if not workspace.is_absolute():
            workspace = (plan_path.parent / workspace).resolve()
        curriculum = read_json(workspace / "curriculum.json")
        module_identities: list[tuple[str, str, int, str]] = []
        for module_index, module in enumerate(curriculum.get("modules") or []):
            for raw in module.get("ooxml_visual_audit_occurrences") or []:
                if not isinstance(raw, dict):
                    raise ValueError(f"Invalid OOXML audit occurrence in {workspace}")
                identity = _occurrence_identity(raw)
                occurrence_id, asset_member, page, source_sha256 = identity
                if occurrence_id in seen_ids:
                    raise ValueError(f"Duplicate global OOXML occurrence_id: {occurrence_id}")
                seen_ids.add(occurrence_id)
                png_path, png_sha256 = _resolve_ooxml_png(workspace, asset_member)
                occurrences.append(_OoxmlOccurrence(
                    workspace=workspace,
                    module_index=module_index,
                    occurrence_id=occurrence_id,
                    asset_member=asset_member,
                    page_number=page,
                    source_media_sha256=source_sha256,
                    png_sha256=png_sha256,
                    png_path=png_path,
                ))
                module_identities.append(identity)
        if "ooxml_visual_audit_occurrences" in lecture:
            planned = lecture.get("ooxml_visual_audit_occurrences") or []
            if any(not isinstance(raw, dict) for raw in planned):
                raise ValueError(f"Invalid plan-level OOXML occurrences for {workspace}")
            plan_identities = [_occurrence_identity(raw) for raw in planned]
            if sorted(plan_identities) != sorted(module_identities):
                raise ValueError(
                    f"Plan/curriculum OOXML audit occurrence mismatch: {workspace}"
                )
    return sorted(
        occurrences,
        key=lambda item: (
            str(item.workspace),
            item.module_index,
            item.page_number,
            item.occurrence_id,
        ),
    )


def _png_data_url(path: Path, expected_sha256: str) -> str:
    payload = path.read_bytes()
    if sha256_bytes(payload) != expected_sha256:
        raise ValueError(f"OOXML PNG changed after audit planning: {path}")
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"OOXML audit payload is no longer PNG: {path}")
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def _audit_ooxml_batch(
    *,
    images: list[_OoxmlImage],
    settings: Settings,
    adapter: ResponsesAdapter,
) -> list[dict[str, Any]]:
    expected_hashes = [item.png_sha256 for item in images]
    response = adapter.structured(
        logical_model=settings.generator_model,
        reviewer=False,
        reasoning_effort=settings.vision_reasoning_effort,
        instructions=_OOXML_VISION_PROMPT_PATH.read_text(encoding="utf-8"),
        input_text=(
            "Kiểm toán từng ảnh PNG đính kèm theo đúng thứ tự. "
            "Mỗi ảnh là một payload độc lập; không dùng nội dung ảnh khác để suy đoán.\n"
            f"REQUIRED_PNG_SHA256_IN_ORDER: {json.dumps(expected_hashes)}\n"
            "Trả đủ một phần tử cho mỗi ảnh với image_number bắt đầu từ 1."
        ),
        input_image_data_urls=[
            _png_data_url(item.png_path, item.png_sha256) for item in images
        ],
        schema_name=f"ooxml_lecture_images_{len(images)}",
        schema={
            **_OOXML_VISION_SCHEMA,
            "properties": {
                **_OOXML_VISION_SCHEMA["properties"],
                "images": {
                    **_OOXML_VISION_SCHEMA["properties"]["images"],
                    "minItems": len(images),
                    "maxItems": len(images),
                },
            },
        },
        output_verbosity=_VISION_OUTPUT_VERBOSITY,
    )
    results: list[dict[str, Any]] = []
    for validated in _validated_ooxml_images(response.data, expected_hashes):
        core = {
            **validated,
            "model_snapshot": response.model_snapshot,
        }
        results.append({
            **core,
            "result_sha256": sha256_json(core),
        })
    return results


def _remove_previous_supplement(current: str, previous: str) -> str:
    value = normalize_text(current)
    old = normalize_text(previous)
    if not old:
        return value
    if value == old:
        return ""
    if value.endswith("\n\n" + old):
        return normalize_text(value[: -len(old) - 2])
    if value.startswith(old + "\n\n"):
        return normalize_text(value[len(old) + 2 :])
    position = value.find(old)
    if position >= 0:
        return normalize_text(value[:position] + value[position + len(old) :])
    return value


def _ooxml_page_supplement(
    items: list[tuple[dict[str, Any], dict[str, Any]]],
) -> str:
    sections: list[str] = []
    for index, (_occurrence, result) in enumerate(items, start=1):
        transcription = normalize_text(result["transcription"])
        explanation = normalize_text(result["visual_explanation"])
        caption = normalize_text(result["caption"])
        lines: list[str] = []
        if result["useful_illustration"]:
            if caption:
                lines.append(f"Hình {index}: {caption}")
            if transcription:
                lines.append(f"Văn bản trong hình:\n{transcription}")
            if explanation:
                lines.append(f"Giải thích hình:\n{explanation}")
        elif transcription:
            # Decorative media is not shipped, but any source text visible in
            # it remains part of the full lecture corpus.
            lines.append(f"Văn bản trong hình {index}:\n{transcription}")
        if lines:
            sections.append(normalize_text("\n".join(lines)))
    return normalize_text("\n\n".join(sections))


def _audited_alt_text(result: dict[str, Any], fallback: str) -> tuple[str, str]:
    caption = _bounded_text(
        result.get("caption")
        or result.get("visual_explanation")
        or result.get("transcription")
        or fallback
    )
    parts = [caption]
    transcription = normalize_text(str(result.get("transcription") or ""))
    explanation = normalize_text(str(result.get("visual_explanation") or ""))
    if transcription and transcription not in parts:
        parts.append(f"Văn bản: {transcription}")
    if explanation and explanation not in parts:
        parts.append(explanation)
    return caption, _bounded_text(" ".join(parts))


def _apply_ooxml_results(
    *,
    occurrences: list[_OoxmlOccurrence],
    results: dict[str, dict[str, Any]],
    audit_contract_sha256: str,
) -> tuple[int, int]:
    by_workspace: dict[Path, list[_OoxmlOccurrence]] = {}
    for occurrence in occurrences:
        by_workspace.setdefault(occurrence.workspace, []).append(occurrence)
    completed_workspaces = 0
    completed_occurrences = 0
    for workspace in sorted(by_workspace, key=str):
        curriculum_path = workspace / "curriculum.json"
        curriculum = read_json(curriculum_path)
        grouped: dict[int, list[_OoxmlOccurrence]] = {}
        for occurrence in by_workspace[workspace]:
            grouped.setdefault(occurrence.module_index, []).append(occurrence)
        if any(
            occurrence.png_sha256 not in results
            for occurrence in by_workspace[workspace]
        ):
            continue
        for module_index, collected in sorted(grouped.items()):
            module = curriculum["modules"][module_index]
            raw_occurrences = list(module.get("ooxml_visual_audit_occurrences") or [])
            raw_by_id = {
                str(raw.get("occurrence_id")): raw
                for raw in raw_occurrences
                if isinstance(raw, dict)
            }
            if set(raw_by_id) != {item.occurrence_id for item in collected}:
                raise ValueError(f"OOXML occurrences changed before apply: {workspace}")
            collected_by_id = {item.occurrence_id: item for item in collected}

            previous_derived = dict(
                module.get("ooxml_visual_audit_page_supplements") or {}
            )
            supplements = {
                str(page): normalize_text(str(text))
                for page, text in (module.get("page_text_supplements") or {}).items()
            }
            for page, old in previous_derived.items():
                cleaned = _remove_previous_supplement(
                    supplements.get(str(page), ""),
                    str(old),
                )
                if cleaned:
                    supplements[str(page)] = cleaned
                else:
                    supplements.pop(str(page), None)

            page_items: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
            useful_specs: list[dict[str, Any]] = []
            completion_records: list[dict[str, Any]] = []
            for occurrence_id in sorted(raw_by_id):
                raw = raw_by_id[occurrence_id]
                collected_item = collected_by_id[occurrence_id]
                png_path, current_png_sha256 = _resolve_ooxml_png(
                    workspace,
                    collected_item.asset_member,
                )
                if (
                    png_path != collected_item.png_path
                    or current_png_sha256 != collected_item.png_sha256
                ):
                    raise ValueError(f"OOXML PNG changed before apply: {occurrence_id}")
                result = results[collected_item.png_sha256]
                page_items.setdefault(collected_item.page_number, []).append((raw, result))
                if result["useful_illustration"]:
                    spec = {
                        key: value
                        for key, value in raw.items()
                        if key not in {"reasons", "page_text_length"}
                    }
                    caption, alt_text = _audited_alt_text(
                        result,
                        str(spec.get("alt_text") or spec.get("caption") or "Minh họa bài giảng"),
                    )
                    spec["caption"] = caption
                    spec["alt_text"] = alt_text
                    useful_specs.append(spec)
                completion_records.append({
                    "occurrence_id": occurrence_id,
                    "source_media_sha256": collected_item.source_media_sha256,
                    "png_sha256": collected_item.png_sha256,
                    "result_sha256": result["result_sha256"],
                    "useful_illustration": result["useful_illustration"],
                })

            derived: dict[str, str] = {}
            for page, items in sorted(page_items.items()):
                items.sort(key=lambda item: str(item[0].get("occurrence_id") or ""))
                contribution = _ooxml_page_supplement(items)
                if not contribution:
                    continue
                key = str(page)
                derived[key] = contribution
                supplements[key] = normalize_text(
                    "\n\n".join(
                        item for item in (supplements.get(key, ""), contribution) if item
                    )
                )

            required_ids = set(raw_by_id)
            untouched = [
                dict(raw)
                for raw in (module.get("illustrations") or [])
                if str(raw.get("occurrence_id") or "") not in required_ids
            ]
            module["illustrations"] = sorted(
                [*untouched, *useful_specs],
                key=lambda item: (
                    int(item.get("page") or 0),
                    str(item.get("occurrence_id") or item.get("asset_member") or ""),
                ),
            )
            module["page_text_supplements"] = supplements
            module["ooxml_visual_audit_page_supplements"] = derived
            completion_records.sort(key=lambda item: item["occurrence_id"])
            module["ooxml_visual_audit_completed_occurrence_ids"] = [
                item["occurrence_id"] for item in completion_records
            ]
            module["ooxml_visual_audit_completed"] = completion_records
            module["ooxml_visual_audit_completed_sha256"] = sha256_json(completion_records)
            module["ooxml_visual_audit_contract_sha256"] = audit_contract_sha256
            completed_occurrences += len(completion_records)
        write_json(curriculum_path, curriculum)
        completed_workspaces += 1
    return completed_workspaces, completed_occurrences


def _audit_catalog_ooxml_images(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    settings: Settings,
    adapter: ResponsesAdapter,
    resume: bool,
) -> dict[str, Any]:
    occurrences = _collect_ooxml_occurrences(plan_path=plan_path, plan=plan)
    images_by_sha: dict[str, _OoxmlImage] = {}
    for occurrence in occurrences:
        images_by_sha.setdefault(
            occurrence.png_sha256,
            _OoxmlImage(occurrence.png_sha256, occurrence.png_path),
        )
    contract = _ooxml_audit_contract(settings)
    cache_path = plan_path.parent / "ooxml-vision-audit.json"
    results = _load_ooxml_cache(cache_path, contract=contract, resume=resume)
    pending = [
        images_by_sha[key]
        for key in sorted(images_by_sha)
        if key not in results
    ]
    batches = [pending[offset : offset + 4] for offset in range(0, len(pending), 4)]
    failures: list[dict[str, Any]] = []
    api_batches = 0
    cache_lock = Lock()

    def request_batch(batch: list[_OoxmlImage]) -> list[dict[str, Any]]:
        nonlocal api_batches
        with cache_lock:
            api_batches += 1
        return _audit_ooxml_batch(
            images=batch,
            settings=settings,
            adapter=adapter,
        )

    def persist_batch(audited: list[dict[str, Any]]) -> None:
        # Initial batches are disjoint, but all workers share one global,
        # content-addressed cache. Serialize merge + atomic replacement so
        # successful split siblings can never overwrite one another.
        with cache_lock:
            for result in audited:
                key = result["payload_sha256"]
                existing = results.get(key)
                if existing is not None and existing != result:
                    raise ValueError(f"Conflicting OOXML vision result for {key}")
                results[key] = result
            _write_ooxml_cache(cache_path, contract=contract, results=results)

    def audit_resilient_batch(batch: list[_OoxmlImage]) -> None:
        _run_vision_batch_with_split(
            batch,
            request=request_batch,
            persist=persist_batch,
            identity=lambda item: item.png_sha256,
        )

    with ThreadPoolExecutor(
        max_workers=max(1, min(5, settings.concurrency)),
        thread_name_prefix="9router-ooxml-vision-audit",
    ) as pool:
        futures = {
            pool.submit(audit_resilient_batch, batch): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                future.result()
            except Exception as error:
                if isinstance(error, _SplitVisionBatchError):
                    terminal = error.failures
                else:
                    with cache_lock:
                        unresolved = tuple(
                            item.png_sha256
                            for item in batch
                            if item.png_sha256 not in results
                        )
                    terminal = [(unresolved, error)]
                for identities, terminal_error in terminal:
                    failures.append({
                        "png_sha256": list(identities),
                        "error_type": type(terminal_error).__name__,
                        "error": str(terminal_error),
                    })
    _write_ooxml_cache(cache_path, contract=contract, results=results)
    completed_workspaces, completed_occurrences = _apply_ooxml_results(
        occurrences=occurrences,
        results=results,
        audit_contract_sha256=sha256_json(contract),
    )
    required_hashes = set(images_by_sha)
    completed_unique = len(required_hashes.intersection(results))
    return {
        "expected_workspaces": len({item.workspace for item in occurrences}),
        "workspaces": completed_workspaces,
        "expected_occurrences": len(occurrences),
        "occurrences": completed_occurrences,
        "unique_images": len(images_by_sha),
        "completed_unique_images": completed_unique,
        "api_batches": api_batches,
        "model_snapshots": sorted({
            results[key]["model_snapshot"]
            for key in required_hashes.intersection(results)
        }),
        "cache_path": str(cache_path.resolve()),
        "failures": failures,
    }


def _pdf_audit_contract(
    document_sha256: str,
    pdf_payload_sha256: str,
    settings: Settings,
) -> dict[str, Any]:
    return {
        "document_sha256": document_sha256,
        "pdf_payload_sha256": pdf_payload_sha256,
        "prompt_sha256": sha256_bytes(_VISION_PROMPT_PATH.read_bytes()),
        "schema_sha256": sha256_json(_VISION_SCHEMA),
        "transport_model": settings.transport_model(settings.generator_model),
        "reasoning_effort": settings.vision_reasoning_effort,
        "response_verbosity": _VISION_OUTPUT_VERBOSITY,
        "render_target_width": 1400,
        "batch_size": 4,
    }


def _previous_vision_nontechnical_pages(
    curriculum: dict[str, Any],
    *,
    filename: str,
) -> set[int]:
    """Recover audited exclusions so rematerialization remains idempotent.

    Once a page is explicitly excluded it is removed from the module's list of
    technical pages that must be materialized. The top-level exclusion remains
    the fail-closed source-coverage declaration, and this helper lets a resumed
    audit still validate/cache the original page classification without making
    another API request.
    """

    pages: set[int] = set()
    for field in ("excluded_nontechnical_pages", "excluded_image_only_pages"):
        for raw in curriculum.get(field) or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("file") or "") != filename:
                continue
            if not str(raw.get("reason") or "").startswith(
                _VISION_NONTECHNICAL_REASON_PREFIX
            ):
                continue
            try:
                page = int(raw.get("page") or 0)
            except (TypeError, ValueError):
                continue
            if page > 0:
                pages.add(page)
    return pages


def _replace_vision_nontechnical_exclusions(
    curriculum: dict[str, Any],
    *,
    filename: str,
    audited_pages: set[int],
    extracted_pages: set[int],
    nontechnical_pages: set[int],
) -> None:
    """Materialize explicit exclusions without weakening page coverage.

    Extracted title/agenda/closing pages use ``excluded_nontechnical_pages`` so
    their OCR chunks are removed. Raster-only equivalents use the existing
    ``excluded_image_only_pages`` contract. Both forms are understood by the
    source builder and therefore keep the every-page coverage check fail closed.
    """

    fields = ("excluded_nontechnical_pages", "excluded_image_only_pages")
    retained: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        raw_entries = curriculum.get(field) or []
        if not isinstance(raw_entries, list) or any(
            not isinstance(item, dict) for item in raw_entries
        ):
            raise ValueError(f"{field} must be an array of objects")
        retained[field] = [
            dict(item)
            for item in raw_entries
            if not (
                str(item.get("file") or "") == filename
                and int(item.get("page") or 0) in audited_pages
                and str(item.get("reason") or "").startswith(
                    _VISION_NONTECHNICAL_REASON_PREFIX
                )
            )
        ]

    for page in sorted(nontechnical_pages):
        field = (
            "excluded_nontechnical_pages"
            if page in extracted_pages
            else "excluded_image_only_pages"
        )
        retained[field].append({
            "file": filename,
            "page": page,
            "reason": _VISION_NONTECHNICAL_REASON,
        })

    for field in fields:
        curriculum[field] = retained[field]


def _audit_workspace(
    *,
    workspace: Path,
    settings: Settings,
    adapter: ResponsesAdapter,
    resume: bool,
) -> tuple[Path, int, list[str]]:
    curriculum_path = workspace / "curriculum.json"
    source_path = workspace / "source.json"
    curriculum = read_json(curriculum_path)
    corpus = ExtractedCorpus.model_validate(read_json(source_path))
    if len(corpus.documents) != 1 or len(curriculum.get("modules") or []) != 1:
        raise ValueError(f"Vision audit workspace must contain one lecture: {workspace}")
    document = corpus.documents[0]
    pdf_path = Path(str(document.source_path or ""))
    module = curriculum["modules"][0]
    filename = str(module.get("output") or document.filename)
    required_pages = sorted({
        int(item) for item in module.get("visual_audit_required_pages") or []
    } | _previous_vision_nontechnical_pages(curriculum, filename=filename))
    if not required_pages:
        return workspace, 0, []
    if not pdf_path.is_file() or pdf_path.suffix.casefold() != ".pdf":
        raise ValueError(f"Vision audit requires the extracted PDF original: {pdf_path}")
    pdf_payload = pdf_path.read_bytes()
    pdf_payload_sha256 = sha256_bytes(pdf_payload)
    if pdf_payload_sha256 != document.sha256:
        raise ValueError(
            "Vision audit PDF hash does not match the extracted source corpus: "
            f"expected={document.sha256}, actual={pdf_payload_sha256}, path={pdf_path}"
        )
    cache_path = workspace / "vision-audit.json"
    audit_contract_payload = _pdf_audit_contract(
        document.sha256,
        pdf_payload_sha256,
        settings,
    )
    audit_contract = sha256_json(audit_contract_payload)
    cached: dict[str, Any] = {}
    if resume and cache_path.is_file():
        try:
            candidate_cache = read_json(cache_path)
            if isinstance(candidate_cache, dict):
                cached = candidate_cache
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cached = {}
    cache_matches = (
        cached.get("document_sha256") == document.sha256
        and cached.get("pdf_payload_sha256") == pdf_payload_sha256
        and cached.get("audit_contract") == audit_contract_payload
        and cached.get("audit_contract_sha256") == audit_contract
    )
    cached_pages: dict[int, dict[str, Any]] = {}
    if cache_matches:
        for item in cached.get("pages", []):
            try:
                page_number = item.get("page_number")
                validated = _validated_pages({"pages": [item]}, [page_number])
            except (AttributeError, TypeError, ValueError):
                continue
            cached_pages[page_number] = validated[0]
    snapshots = set(cached.get("model_snapshots") or []) if cache_matches else set()
    pending = [page for page in required_pages if page not in cached_pages]

    def request_batch(page_numbers: list[int]) -> tuple[list[dict[str, Any]], str]:
        input_text = (
            "Kiểm toán các ảnh trang theo đúng thứ tự đính kèm. "
            f"DOCUMENT: {getattr(document, 'display_name', None) or document.filename}\n"
            f"REQUIRED_PAGE_NUMBERS_IN_ORDER: {json.dumps(page_numbers)}\n"
            "Chép và giải thích đầy đủ nội dung nhìn thấy. Không tóm tắt và không bỏ trang."
        )
        response = adapter.structured(
            logical_model=settings.generator_model,
            reviewer=False,
            reasoning_effort=settings.vision_reasoning_effort,
            instructions=_VISION_PROMPT_PATH.read_text(encoding="utf-8"),
            input_text=input_text,
            input_image_data_urls=[
                _page_data_url(pdf_payload, page) for page in page_numbers
            ],
            schema_name=f"lecture_visual_pages_{len(page_numbers)}",
            schema={
                **_VISION_SCHEMA,
                "properties": {
                    **_VISION_SCHEMA["properties"],
                    "pages": {
                        **_VISION_SCHEMA["properties"]["pages"],
                        "minItems": len(page_numbers),
                        "maxItems": len(page_numbers),
                    },
                },
            },
            output_verbosity=_VISION_OUTPUT_VERBOSITY,
        )
        return _validated_pages(response.data, page_numbers), response.model_snapshot

    def persist_batch(result: tuple[list[dict[str, Any]], str]) -> None:
        audited_pages, model_snapshot = result
        for item in audited_pages:
            cached_pages[item["page_number"]] = item
        snapshots.add(model_snapshot)
        write_json(cache_path, {
            "schema_version": "1.0",
            "document_sha256": document.sha256,
            "pdf_payload_sha256": pdf_payload_sha256,
            "audit_contract": audit_contract_payload,
            "audit_contract_sha256": audit_contract,
            "pages": [cached_pages[key] for key in sorted(cached_pages)],
            "model_snapshots": sorted(snapshots),
        })

    for offset in range(0, len(pending), 4):
        page_numbers = pending[offset : offset + 4]
        _run_vision_batch_with_split(
            page_numbers,
            request=request_batch,
            persist=persist_batch,
            identity=str,
        )
    chunks_by_page: dict[int, int] = {}
    for chunk in corpus.chunks:
        chunks_by_page[chunk.page_start] = chunks_by_page.get(chunk.page_start, 0) + len(
            chunk.normalized_text
        )
    overrides = dict(module.get("page_text_overrides") or {})
    supplements = dict(module.get("page_text_supplements") or {})
    technical_pages: list[int] = []
    nontechnical_pages: set[int] = set()
    for page in required_pages:
        audit = cached_pages[page]
        overrides.pop(str(page), None)
        supplements.pop(str(page), None)
        if audit["content_kind"] == "NONTECHNICAL":
            nontechnical_pages.add(page)
            continue
        technical_pages.append(page)
        text = normalize_text(
            "\n\n".join(
                item
                for item in (audit["transcription"], audit["visual_explanation"])
                if item
            )
        )
        if chunks_by_page.get(page, 0) == 0:
            overrides[str(page)] = text
        else:
            supplements[str(page)] = text
    _replace_vision_nontechnical_exclusions(
        curriculum,
        filename=filename,
        audited_pages=set(required_pages),
        extracted_pages={page for page, length in chunks_by_page.items() if length > 0},
        nontechnical_pages=nontechnical_pages,
    )
    # The deterministic visual picker deliberately over-selects candidates;
    # vision makes the final semantic decision. Keep useful diagrams and give
    # them a meaningful source-grounded caption, while dropping decorative
    # covers/closing slides from the offline media payload.
    audited_by_page = {int(item["page_number"]): item for item in cached_pages.values()}
    kept_illustrations: list[dict[str, Any]] = []
    for raw in module.get("illustrations") or []:
        illustration = dict(raw)
        audited = audited_by_page.get(int(illustration.get("page") or 0))
        if audited is not None and (
            audited["content_kind"] == "NONTECHNICAL"
            or not audited["useful_illustration"]
        ):
            continue
        if audited is not None and audited.get("caption"):
            illustration["caption"] = audited["caption"]
        kept_illustrations.append(illustration)
    module["page_text_overrides"] = overrides
    module["page_text_supplements"] = supplements
    # The original page remains accounted for by an explicit top-level
    # exclusion. Only TECHNICAL pages belong in the materialization-completion
    # set consumed by the packaging gate.
    module["visual_audit_required_pages"] = technical_pages
    module["illustrations"] = kept_illustrations
    module["visual_audit_completed_sha256"] = sha256_bytes(
        json.dumps(
            [cached_pages[page] for page in required_pages],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    )
    write_json(curriculum_path, curriculum)
    return workspace, len(required_pages), sorted(snapshots)


def audit_catalog_visual_pages(
    *,
    plan_path: Path,
    settings: Settings,
    resume: bool = True,
    adapter: ResponsesAdapter | None = None,
) -> dict[str, Any]:
    """Vision-audit low-text PDF pages and embedded OOXML PNGs through 9router."""

    plan = read_json(plan_path)
    adapter = adapter or build_adapter(settings)
    try:
        ooxml_report = _audit_catalog_ooxml_images(
            plan_path=plan_path,
            plan=plan,
            settings=settings,
            adapter=adapter,
            resume=resume,
        )
    except Exception as error:
        ooxml_report = {
            "expected_workspaces": 0,
            "workspaces": 0,
            "expected_occurrences": 0,
            "occurrences": 0,
            "unique_images": 0,
            "completed_unique_images": 0,
            "api_batches": 0,
            "model_snapshots": [],
            "cache_path": str((plan_path.parent / "ooxml-vision-audit.json").resolve()),
            "failures": [{
                "error_type": type(error).__name__,
                "error": str(error),
            }],
        }
    workspaces = [
        Path(item["workspace"])
        for item in plan["lectures"]
        if item.get("visual_audit_required_pages")
    ]
    completed: list[tuple[Path, int, list[str]]] = []
    failures: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "status": (
            "PARTIAL"
            if ooxml_report["failures"] and not workspaces
            else ("READY" if not workspaces else "BUILDING")
        ),
        "expected_workspaces": len(workspaces),
        "workspaces": 0,
        "pages": 0,
        "model_snapshots": [],
        "failures": [],
        "ooxml": ooxml_report,
    }
    with ThreadPoolExecutor(
        max_workers=max(1, min(5, settings.concurrency)),
        thread_name_prefix="9router-vision-audit",
    ) as pool:
        futures = {
            pool.submit(
                _audit_workspace,
                workspace=workspace,
                settings=settings,
                adapter=adapter,
                resume=resume,
            ): workspace
            for workspace in workspaces
        }
        for future in as_completed(futures):
            workspace = futures[future]
            try:
                completed.append(future.result())
            except Exception as error:
                failures.append({
                    "workspace": str(workspace),
                    "error_type": type(error).__name__,
                    "error": str(error),
                })
            report = {
                "status": (
                    "BUILDING"
                    if len(completed) + len(failures) < len(workspaces)
                    else (
                        "READY"
                        if not failures and not ooxml_report["failures"]
                        else "PARTIAL"
                    )
                ),
                "expected_workspaces": len(workspaces),
                "workspaces": len(completed),
                "pages": sum(item[1] for item in completed),
                "model_snapshots": sorted({
                    snapshot for item in completed for snapshot in item[2]
                }),
                "failures": sorted(failures, key=lambda item: item["workspace"]),
                "ooxml": ooxml_report,
            }
            write_json(plan_path.parent / "vision-audit-report.json", report)
    if not workspaces:
        write_json(plan_path.parent / "vision-audit-report.json", report)
    if failures or ooxml_report["failures"]:
        raise RuntimeError(
            f"Vision audit incomplete: {len(completed)}/{len(workspaces)} workspaces ready; "
            f"PDF failures={len(failures)}, OOXML failures={len(ooxml_report['failures'])}; "
            "rerun with resume after inspecting vision-audit-report.json"
        )
    return report
