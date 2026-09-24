from __future__ import annotations

import json
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..models import StrictModel
from ..util import sha256_bytes, sha256_json, stable_id
from .archives import (
    ArchiveDescriptor,
    ArchiveInventory,
    InventoryOccurrence,
    _normalized_member_path,
    _sha256_file,
    _validate_archive_info,
    inventory_sha256,
)


_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MAX_ANCILLARY_PAYLOAD_BYTES = 64 * 1024 * 1024
AncillaryClassification = Literal[
    "ASSESSMENT_SHELL",
    "EXTERNAL_LEARNING_LINK",
    "NONLEARNING_LINK",
    "COURSE_METADATA",
    "COURSE_COVER",
]
AssessmentActivityKind = Literal[
    "LAB_ASSIGNMENT",
    "SUBMISSION",
    "REFLECTION",
    "EXAM_POINTER",
]
AncillarySourceKind = Literal[
    "assessment",
    "external-link",
    "course-metadata",
    "course-cover",
]


class AncillaryMetadata(StrictModel):
    source_kind: AncillarySourceKind
    course_id: str | None = None
    course_title: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    unit_id: str | None = None
    unit_title: str | None = None
    source_path_id: str | None = None
    activity_kind: AssessmentActivityKind | None = None
    is_graded: bool | None = None
    declared_question_count: int | None = Field(default=None, ge=0)
    assessment_permission: str | None = None
    external_object_type: str | None = None
    external_url: str | None = None
    referenced_cover_name: str | None = None


class AncillaryOccurrence(StrictModel):
    occurrence_id: str
    ancillary_id: str
    archive_id: str
    archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_name: str
    archive_index: int = Field(ge=0)
    entry_index: int = Field(ge=0)
    member_path: str
    logical_path: str


class AncillaryAsset(StrictModel):
    ancillary_id: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    suffix: str
    media_type: str
    byte_size: int = Field(ge=1)
    extracted_path: str
    classification: AncillaryClassification
    occurrence_ids: list[str] = Field(min_length=1)
    logical_paths: list[str] = Field(min_length=1)
    metadata: AncillaryMetadata
    schedulable: Literal[False] = False
    quiz_payload_available: Literal[False] = False


class AncillaryCounts(StrictModel):
    ancillary_occurrences: int = Field(ge=0)
    unique_assets: int = Field(ge=0)
    exact_duplicate_occurrences: int = Field(ge=0)
    occurrences_by_classification: dict[str, int]
    assets_by_classification: dict[str, int]
    assessment_declared_question_count: int = Field(ge=0)
    schedulable_assets: int = Field(ge=0)
    quiz_payload_assets: int = Field(ge=0)


class AncillaryManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    assets: list[AncillaryAsset]
    occurrences: list[AncillaryOccurrence]
    counts: AncillaryCounts

    @model_validator(mode="after")
    def internally_consistent(self) -> "AncillaryManifest":
        asset_ids = [item.ancillary_id for item in self.assets]
        occurrence_ids = [item.occurrence_id for item in self.occurrences]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("Ancillary asset IDs must be unique")
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ValueError("Ancillary occurrence IDs must be unique")

        known_assets = set(asset_ids)
        known_occurrences = set(occurrence_ids)
        if any(item.ancillary_id not in known_assets for item in self.occurrences):
            raise ValueError("Ancillary occurrence references an unknown asset")
        occurrence_by_id = {item.occurrence_id: item for item in self.occurrences}
        asset_by_id = {item.ancillary_id: item for item in self.assets}
        for asset in self.assets:
            if any(item not in known_occurrences for item in asset.occurrence_ids):
                raise ValueError("Ancillary asset references an unknown occurrence")
            expected_occurrences = [
                item.occurrence_id
                for item in self.occurrences
                if item.ancillary_id == asset.ancillary_id
            ]
            if asset.occurrence_ids != expected_occurrences:
                raise ValueError("Ancillary asset occurrence provenance mismatch")
            expected_paths = [occurrence_by_id[item].logical_path for item in asset.occurrence_ids]
            if asset.logical_paths != expected_paths:
                raise ValueError("Ancillary asset alias paths do not match its occurrences")

            expected_source_kind = {
                "ASSESSMENT_SHELL": "assessment",
                "EXTERNAL_LEARNING_LINK": "external-link",
                "NONLEARNING_LINK": "external-link",
                "COURSE_METADATA": "course-metadata",
                "COURSE_COVER": "course-cover",
            }[asset.classification]
            if asset.metadata.source_kind != expected_source_kind:
                raise ValueError("Ancillary classification and metadata kind disagree")
            if asset.classification == "ASSESSMENT_SHELL":
                if asset.metadata.declared_question_count is None:
                    raise ValueError("Assessment shell is missing its declared question count")
                if asset.metadata.activity_kind is None:
                    raise ValueError("Assessment shell is missing its activity kind")
            elif (
                asset.metadata.declared_question_count is not None
                or asset.metadata.is_graded is not None
                or asset.metadata.activity_kind is not None
            ):
                raise ValueError("Non-assessment ancillary asset has assessment metadata")
            if asset.classification in {"EXTERNAL_LEARNING_LINK", "NONLEARNING_LINK"}:
                if not asset.metadata.external_url or not asset.metadata.external_object_type:
                    raise ValueError("External link is missing normalized link metadata")
            if asset.classification == "COURSE_METADATA" and not asset.metadata.referenced_cover_name:
                raise ValueError("Course metadata does not reference a cover")
            if asset.classification == "COURSE_COVER" and not asset.metadata.referenced_cover_name:
                raise ValueError("Course cover is missing its source reference")

        expected_occurrence_classes = Counter(
            asset_by_id[item.ancillary_id].classification for item in self.occurrences
        )
        expected_asset_classes = Counter(item.classification for item in self.assets)
        counts = self.counts
        if counts.ancillary_occurrences != len(self.occurrences):
            raise ValueError("Ancillary occurrence count mismatch")
        if counts.unique_assets != len(self.assets):
            raise ValueError("Ancillary unique asset count mismatch")
        if counts.exact_duplicate_occurrences != len(self.occurrences) - len(self.assets):
            raise ValueError("Ancillary exact duplicate count mismatch")
        if counts.occurrences_by_classification != dict(sorted(expected_occurrence_classes.items())):
            raise ValueError("Ancillary occurrence classification counts mismatch")
        if counts.assets_by_classification != dict(sorted(expected_asset_classes.items())):
            raise ValueError("Ancillary asset classification counts mismatch")
        declared_questions = sum(
            asset_by_id[item.ancillary_id].metadata.declared_question_count or 0
            for item in self.occurrences
        )
        if counts.assessment_declared_question_count != declared_questions:
            raise ValueError("Ancillary declared assessment question count mismatch")
        if counts.schedulable_assets != sum(item.schedulable for item in self.assets):
            raise ValueError("Ancillary schedulable asset count mismatch")
        if counts.quiz_payload_assets != sum(item.quiz_payload_available for item in self.assets):
            raise ValueError("Ancillary quiz payload asset count mismatch")
        return self


def _manifest_hash_payload(manifest: AncillaryManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json", exclude={"manifest_sha256"})


def ancillary_manifest_sha256(manifest: AncillaryManifest) -> str:
    return sha256_json(_manifest_hash_payload(manifest))


@dataclass(frozen=True)
class _RawAncillary:
    occurrence: InventoryOccurrence
    archive: ArchiveDescriptor
    raw: bytes
    digest: str
    parsed_json: dict[str, Any] | None


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > _MAX_ANCILLARY_PAYLOAD_BYTES:
        raise ValueError(f"Ancillary payload is too large: {info.filename}")
    result = bytearray()
    with archive.open(info, "r") as source:
        while block := source.read(1024 * 1024):
            result.extend(block)
            if len(result) > _MAX_ANCILLARY_PAYLOAD_BYTES:
                raise ValueError(f"Ancillary payload expanded past its limit: {info.filename}")
    if len(result) != info.file_size:
        raise ValueError(f"Ancillary payload size/CRC verification failed: {info.filename}")
    return bytes(result)


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Ancillary JSON has a duplicate key at {label}: {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"Ancillary JSON contains a non-finite number at {label}: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Ancillary JSON is invalid at {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Ancillary JSON must contain an object: {label}")
    return value


def _optional_string(payload: dict[str, Any], key: str, label: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Ancillary JSON field {label}.{key} must be a string or null")
    return value


def _mapping(payload: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Ancillary JSON field {label}.{key} must be an object")
    return value


def _common_metadata(payload: dict[str, Any], *, source_kind: AncillarySourceKind) -> dict[str, Any]:
    return {
        "source_kind": source_kind,
        "course_id": _optional_string(payload, "courseId", source_kind),
        "course_title": _optional_string(payload, "courseTitle", source_kind),
        "section_id": _optional_string(payload, "sectionId", source_kind),
        "section_title": _optional_string(payload, "sectionTitle", source_kind),
        "unit_id": _optional_string(payload, "unitId", source_kind),
        "unit_title": _optional_string(payload, "unitTitle", source_kind),
        "source_path_id": _optional_string(payload, "sourcePath", source_kind),
    }


def _assessment_payload_markers(value: Any, path: str = "$") -> list[str]:
    markers: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            is_question_key = normalized.startswith("question") and normalized != "questionscount"
            is_answer_key = normalized in {
                "answer",
                "answers",
                "choice",
                "choices",
                "correct",
                "correctanswer",
                "correctanswers",
                "explanation",
                "feedback",
                "option",
                "options",
                "prompt",
                "solution",
                "solutions",
            }
            if is_question_key or is_answer_key:
                markers.append(f"{path}.{key}")
            markers.extend(_assessment_payload_markers(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            markers.extend(_assessment_payload_markers(item, f"{path}[{index}]"))
    return markers


def _assessment_activity_kind(title: str) -> AssessmentActivityKind:
    normalized = title.casefold()
    if "exam" in normalized or "final test" in normalized:
        return "EXAM_POINTER"
    if "reflection" in normalized:
        return "REFLECTION"
    if "submit" in normalized or "submission" in normalized or "nộp" in normalized:
        return "SUBMISSION"
    return "LAB_ASSIGNMENT"


def _assessment_metadata(payload: dict[str, Any], label: str) -> AncillaryMetadata:
    markers = _assessment_payload_markers(payload)
    if markers:
        preview = ", ".join(markers[:3])
        raise ValueError(
            f"Assessment contains embedded question/answer payload and is not a shell: {label} ({preview})"
        )
    metadata = _mapping(payload, "metadata", label)
    object_metadata = _mapping(metadata, "object", f"{label}.metadata")
    assessment = _mapping(metadata, "assessment", f"{label}.metadata")
    question_count = object_metadata.get("questionsCount")
    if isinstance(question_count, bool) or not isinstance(question_count, int) or question_count < 0:
        raise ValueError(f"Assessment questionsCount must be a non-negative integer: {label}")
    is_graded = assessment.get("isGraded")
    if not isinstance(is_graded, bool):
        raise ValueError(f"Assessment isGraded must be boolean: {label}")
    title = _optional_string(payload, "unitTitle", label) or _optional_string(
        object_metadata, "title", f"{label}.metadata.object"
    )
    if not title:
        raise ValueError(f"Assessment shell has no title: {label}")
    data = object_metadata.get("data")
    if data is not None and not isinstance(data, dict):
        raise ValueError(f"Assessment metadata.object.data must be an object or null: {label}")
    permission = None if data is None else _optional_string(data, "permission", f"{label}.metadata.object.data")
    return AncillaryMetadata(
        **_common_metadata(payload, source_kind="assessment"),
        activity_kind=_assessment_activity_kind(title),
        is_graded=is_graded,
        declared_question_count=question_count,
        assessment_permission=permission,
    )


def _validated_external_url(value: str | None, label: str) -> str:
    if not value:
        raise ValueError(f"External link has no URL: {label}")
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"External link URL must be HTTP(S): {label}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"External link URL may not contain credentials: {label}")
    return normalized


def _external_link_metadata(
    payload: dict[str, Any],
    label: str,
) -> tuple[AncillaryClassification, AncillaryMetadata]:
    metadata = _mapping(payload, "metadata", label)
    object_type = _optional_string(metadata, "objectType", f"{label}.metadata")
    if not object_type:
        raise ValueError(f"External link has no objectType: {label}")
    raw_url = (
        _optional_string(metadata, "url", f"{label}.metadata")
        or _optional_string(metadata, "contentLink", f"{label}.metadata")
        or _optional_string(payload, "sourcePath", label)
    )
    external_url = _validated_external_url(raw_url, label)
    hostname = (urlsplit(external_url).hostname or "").casefold()
    normalized_type = object_type.casefold()
    title = (
        _optional_string(payload, "unitTitle", label)
        or _optional_string(metadata, "title", f"{label}.metadata")
        or ""
    ).casefold()
    if normalized_type == "youtube" or hostname in {"youtube.com", "www.youtube.com", "youtu.be"}:
        classification: AncillaryClassification = "EXTERNAL_LEARNING_LINK"
    elif normalized_type == "lti" and "attendance" in title:
        classification = "NONLEARNING_LINK"
    else:
        raise ValueError(f"External link cannot be safely classified: {label}")
    return classification, AncillaryMetadata(
        **_common_metadata(payload, source_kind="external-link"),
        external_object_type=object_type,
        external_url=external_url,
    )


def _course_metadata(payload: dict[str, Any], label: str) -> AncillaryMetadata:
    metadata = _mapping(payload, "metadata", label)
    course_image = _mapping(metadata, "courseImage", f"{label}.metadata")
    raw_name = _optional_string(course_image, "name", f"{label}.metadata.courseImage")
    if not raw_name:
        raise ValueError(f"Course metadata has no course image name: {label}")
    cover_name = PurePosixPath(raw_name.replace("\\", "/")).name
    if not cover_name or cover_name in {".", ".."}:
        raise ValueError(f"Course metadata has an unsafe course image name: {label}")
    return AncillaryMetadata(
        **_common_metadata(payload, source_kind="course-metadata"),
        referenced_cover_name=cover_name,
    )


def _parse_json_record(record: _RawAncillary) -> tuple[AncillaryClassification, AncillaryMetadata]:
    payload = record.parsed_json
    if payload is None:
        raise ValueError(f"Expected JSON payload: {record.occurrence.logical_path}")
    kind = payload.get("kind")
    label = record.occurrence.logical_path
    member_name = record.occurrence.member_path.casefold()
    if kind == "assessment":
        if not member_name.endswith(".assessment.json"):
            raise ValueError(f"Assessment JSON does not use the expected suffix: {label}")
        return "ASSESSMENT_SHELL", _assessment_metadata(payload, label)
    if kind == "external-link":
        if not member_name.endswith(".link.json"):
            raise ValueError(f"External-link JSON does not use the expected suffix: {label}")
        return _external_link_metadata(payload, label)
    if kind == "course-metadata":
        return "COURSE_METADATA", _course_metadata(payload, label)
    raise ValueError(f"Unsupported ancillary JSON kind at {label}: {kind!r}")


def _matching_cover_metadata(
    record: _RawAncillary,
    course_metadata: list[AncillaryMetadata],
) -> AncillaryMetadata:
    label = record.occurrence.logical_path
    if not record.raw.startswith(b"\xff\xd8\xff") or not record.raw.endswith(b"\xff\xd9"):
        raise ValueError(f"Course cover is not a complete JPEG: {label}")
    filename = PurePosixPath(record.occurrence.member_path).name
    matches = [
        item
        for item in course_metadata
        if item.referenced_cover_name
        and (
            filename == item.referenced_cover_name
            or filename.endswith(" - " + item.referenced_cover_name)
        )
    ]
    if len(matches) != 1:
        raise ValueError(f"Ancillary JPEG does not match exactly one course cover reference: {label}")
    match = matches[0]
    return AncillaryMetadata(
        source_kind="course-cover",
        course_id=match.course_id,
        course_title=match.course_title,
        referenced_cover_name=match.referenced_cover_name,
    )


def _write_content_addressed(
    *,
    raw: bytes,
    digest: str,
    suffix: str,
    output_dir: Path,
) -> str:
    filename = f"ancillary_{digest.removeprefix('sha256:')}{suffix}"
    relative_path = str(PurePosixPath(output_dir.name) / filename)
    destination = output_dir / filename
    if destination.is_symlink():
        raise ValueError(f"Ancillary destination may not be a symlink: {destination}")
    if destination.is_file():
        if destination.stat().st_size != len(raw) or sha256_bytes(destination.read_bytes()) != digest:
            raise ValueError(f"Existing ancillary payload is stale: {destination}")
        return relative_path
    temporary = output_dir / f".{filename}.{os.getpid()}.partial"
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"Ancillary temporary path already exists: {temporary}")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
        os.replace(temporary, destination)
    finally:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
    return relative_path


def _read_ancillary(inventory: ArchiveInventory) -> list[_RawAncillary]:
    selected = sorted(
        (item for item in inventory.occurrences if item.status == "ANCILLARY"),
        key=lambda item: (item.archive_index, item.entry_index, item.occurrence_id),
    )
    grouped: dict[int, list[InventoryOccurrence]] = {}
    for occurrence in selected:
        grouped.setdefault(occurrence.archive_index, []).append(occurrence)

    records: list[_RawAncillary] = []
    for archive_index, occurrences in sorted(grouped.items()):
        if archive_index >= len(inventory.archives):
            raise ValueError(f"Ancillary occurrence has an invalid archive index: {archive_index}")
        descriptor = inventory.archives[archive_index]
        if any(item.archive_id != descriptor.archive_id for item in occurrences):
            raise ValueError("Ancillary occurrence archive index/ID provenance mismatch")
        source_path = Path(descriptor.source_path)
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError(f"Ancillary source archive is unavailable: {source_path}")
        if source_path.stat().st_size != descriptor.byte_size:
            raise ValueError(f"Ancillary source archive size mismatch: {source_path}")
        if _sha256_file(source_path) != descriptor.sha256:
            raise ValueError(f"Ancillary source archive hash mismatch: {source_path}")

        with zipfile.ZipFile(source_path) as archive:
            file_infos = [item for item in archive.infolist() if not item.is_dir()]
            if len(file_infos) != descriptor.entry_count:
                raise ValueError(f"Ancillary source archive entry count mismatch: {source_path}")
            if sum(item.file_size for item in file_infos) != descriptor.total_uncompressed_bytes:
                raise ValueError(f"Ancillary source archive expanded size mismatch: {source_path}")
            for occurrence in occurrences:
                if occurrence.entry_index >= len(file_infos):
                    raise ValueError(
                        f"Ancillary occurrence has an invalid archive entry index: {occurrence.logical_path}"
                    )
                info = file_infos[occurrence.entry_index]
                member_path = _normalized_member_path(info.filename)
                _validate_archive_info(info, member_path)
                if member_path != occurrence.member_path:
                    raise ValueError(f"Ancillary archive member provenance mismatch: {occurrence.logical_path}")
                expected_logical_path = f"{descriptor.name}/{member_path}"
                if occurrence.logical_path != expected_logical_path:
                    raise ValueError(f"Ancillary logical path provenance mismatch: {occurrence.logical_path}")
                if occurrence.suffix != PurePosixPath(member_path).suffix.casefold():
                    raise ValueError(f"Ancillary suffix provenance mismatch: {occurrence.logical_path}")
                if (
                    occurrence.byte_size != info.file_size
                    or occurrence.compressed_size != info.compress_size
                ):
                    raise ValueError(f"Ancillary size provenance mismatch: {occurrence.logical_path}")
                raw = _read_member(archive, info)
                if not raw:
                    raise ValueError(f"Ancillary payload is empty: {occurrence.logical_path}")
                parsed_json = (
                    _json_object(raw, occurrence.logical_path)
                    if occurrence.suffix == ".json"
                    else None
                )
                records.append(_RawAncillary(
                    occurrence=occurrence,
                    archive=descriptor,
                    raw=raw,
                    digest=sha256_bytes(raw),
                    parsed_json=parsed_json,
                ))
    return records


def export_ancillary(
    inventory: ArchiveInventory,
    *,
    output_dir: Path,
) -> AncillaryManifest:
    """Export exact ancillary payloads without admitting them into lessons or quizzes."""

    if inventory.inventory_sha256 != inventory_sha256(inventory):
        raise ValueError("Archive inventory hash mismatch")
    if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
        raise ValueError(f"Ancillary output directory is unsafe: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.name or PurePosixPath(output_dir.name).name != output_dir.name:
        raise ValueError(f"Ancillary output directory has an unsafe name: {output_dir}")

    records = _read_ancillary(inventory)
    classified: dict[str, tuple[AncillaryClassification, AncillaryMetadata]] = {}
    course_metadata: list[AncillaryMetadata] = []
    for record in records:
        if record.parsed_json is None:
            continue
        classification, metadata = _parse_json_record(record)
        classified[record.occurrence.occurrence_id] = (classification, metadata)
        if classification == "COURSE_METADATA":
            course_metadata.append(metadata)

    for record in records:
        if record.parsed_json is not None:
            continue
        if record.occurrence.suffix not in {".jpg", ".jpeg"}:
            raise ValueError(f"Unsupported ancillary binary payload: {record.occurrence.logical_path}")
        classified[record.occurrence.occurrence_id] = (
            "COURSE_COVER",
            _matching_cover_metadata(record, course_metadata),
        )

    raw_assets: dict[str, dict[str, Any]] = {}
    occurrences: list[AncillaryOccurrence] = []
    for record in records:
        classification, metadata = classified[record.occurrence.occurrence_id]
        ancillary_id = stable_id("ancillary", record.digest, length=24)
        row = raw_assets.get(record.digest)
        if row is None:
            suffix = ".json" if record.parsed_json is not None else record.occurrence.suffix
            media_type = "application/json" if record.parsed_json is not None else "image/jpeg"
            extracted_path = _write_content_addressed(
                raw=record.raw,
                digest=record.digest,
                suffix=suffix,
                output_dir=output_dir,
            )
            row = {
                "ancillary_id": ancillary_id,
                "sha256": record.digest,
                "suffix": suffix,
                "media_type": media_type,
                "byte_size": len(record.raw),
                "extracted_path": extracted_path,
                "classification": classification,
                "occurrence_ids": [],
                "logical_paths": [],
                "metadata": metadata,
                "schedulable": False,
                "quiz_payload_available": False,
            }
            raw_assets[record.digest] = row
        elif row["classification"] != classification or row["metadata"] != metadata:
            raise ValueError(
                f"Exact duplicate ancillary payload has conflicting semantics: {record.occurrence.logical_path}"
            )
        row["occurrence_ids"].append(record.occurrence.occurrence_id)
        row["logical_paths"].append(record.occurrence.logical_path)
        occurrences.append(AncillaryOccurrence(
            occurrence_id=record.occurrence.occurrence_id,
            ancillary_id=ancillary_id,
            archive_id=record.archive.archive_id,
            archive_sha256=record.archive.sha256,
            archive_name=record.archive.name,
            archive_index=record.occurrence.archive_index,
            entry_index=record.occurrence.entry_index,
            member_path=record.occurrence.member_path,
            logical_path=record.occurrence.logical_path,
        ))

    assets = [AncillaryAsset.model_validate(item) for item in raw_assets.values()]
    occurrence_classes = Counter(
        asset.classification
        for asset in assets
        for _ in asset.occurrence_ids
    )
    asset_classes = Counter(item.classification for item in assets)
    asset_by_id = {item.ancillary_id: item for item in assets}
    counts = AncillaryCounts(
        ancillary_occurrences=len(occurrences),
        unique_assets=len(assets),
        exact_duplicate_occurrences=len(occurrences) - len(assets),
        occurrences_by_classification=dict(sorted(occurrence_classes.items())),
        assets_by_classification=dict(sorted(asset_classes.items())),
        assessment_declared_question_count=sum(
            asset_by_id[item.ancillary_id].metadata.declared_question_count or 0
            for item in occurrences
        ),
        schedulable_assets=sum(item.schedulable for item in assets),
        quiz_payload_assets=sum(item.quiz_payload_available for item in assets),
    )
    placeholder = "sha256:" + "0" * 64
    manifest = AncillaryManifest(
        manifest_sha256=placeholder,
        inventory_sha256=inventory.inventory_sha256,
        assets=assets,
        occurrences=occurrences,
        counts=counts,
    )
    return manifest.model_copy(update={"manifest_sha256": ancillary_manifest_sha256(manifest)})
