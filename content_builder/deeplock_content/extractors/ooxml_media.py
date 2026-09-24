from __future__ import annotations

import os
import posixpath
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from pydantic import Field, model_validator

from ..models import Document, StrictModel
from ..util import normalize_text, sha256_bytes, sha256_json, stable_id
from .ooxml import (
    DOCX_VIRTUAL_PAGE_CHARS,
    _is_heading,
    _local_name,
    _open_ooxml,
    _paragraph_style,
    _presentation_slide_paths,
    _read_xml,
    _table_text,
    _text_from_drawing_xml,
    _word_paragraph_text,
)


_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MIME_TYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".emf": "image/emf",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".jfif": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".wmf": "image/wmf",
}
_MAPPING_KINDS = Literal[
    "PPTX_SLIDE_EMBED",
    "PPTX_SLIDE_RELATIONSHIP",
    "PPTX_OTHER_RELATIONSHIP",
    "DOCX_BODY_EMBED",
    "DOCX_BODY_RELATIONSHIP",
    "DOCX_OTHER_RELATIONSHIP",
    "PACKAGE_MEDIA_UNREFERENCED",
]


class OoxmlMediaOccurrence(StrictModel):
    occurrence_id: str
    media_id: str
    document_id: str
    source_format: Literal["PPTX", "DOCX"]
    package_part: str
    relationship_source_part: str | None = None
    relationship_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    section_title: str | None = None
    alt_text: str | None = None
    mapping_kind: _MAPPING_KINDS


class OoxmlMediaAsset(StrictModel):
    media_id: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    mime_type: str
    suffix: str
    byte_size: int = Field(ge=1)
    extracted_path: str
    occurrence_ids: list[str] = Field(min_length=1)
    document_ids: list[str] = Field(min_length=1)


class OoxmlMediaCounts(StrictModel):
    documents_scanned: int = Field(ge=0)
    documents_with_media: int = Field(ge=0)
    package_media_parts: int = Field(ge=0)
    media_occurrences: int = Field(ge=0)
    unique_assets: int = Field(ge=0)
    exact_duplicate_media_parts: int = Field(ge=0)
    unreferenced_media_parts: int = Field(ge=0)
    by_mime_type: dict[str, int]


class OoxmlMediaManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    assets: list[OoxmlMediaAsset]
    occurrences: list[OoxmlMediaOccurrence]
    counts: OoxmlMediaCounts

    @model_validator(mode="after")
    def internally_consistent(self) -> "OoxmlMediaManifest":
        asset_ids = [item.media_id for item in self.assets]
        occurrence_ids = [item.occurrence_id for item in self.occurrences]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("OOXML media asset IDs must be unique")
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ValueError("OOXML media occurrence IDs must be unique")
        known_assets = set(asset_ids)
        known_occurrences = set(occurrence_ids)
        if any(item.media_id not in known_assets for item in self.occurrences):
            raise ValueError("OOXML media occurrence references an unknown asset")
        occurrence_by_id = {item.occurrence_id: item for item in self.occurrences}
        for asset in self.assets:
            if any(item not in known_occurrences for item in asset.occurrence_ids):
                raise ValueError("OOXML media asset references an unknown occurrence")
            expected_occurrences = {
                item.occurrence_id
                for item in self.occurrences
                if item.media_id == asset.media_id
            }
            if set(asset.occurrence_ids) != expected_occurrences:
                raise ValueError("OOXML media asset occurrence provenance mismatch")
            expected_documents = {
                occurrence_by_id[item].document_id for item in asset.occurrence_ids
            }
            if set(asset.document_ids) != expected_documents:
                raise ValueError("OOXML media asset document provenance mismatch")
        if self.counts.unique_assets != len(self.assets):
            raise ValueError("OOXML unique media asset count mismatch")
        if self.counts.media_occurrences != len(self.occurrences):
            raise ValueError("OOXML media occurrence count mismatch")
        if self.counts.package_media_parts < self.counts.unique_assets:
            raise ValueError("OOXML package media part count is below its unique asset count")
        if (
            self.counts.exact_duplicate_media_parts
            != self.counts.package_media_parts - self.counts.unique_assets
        ):
            raise ValueError("OOXML exact duplicate media part count mismatch")
        if sum(self.counts.by_mime_type.values()) != self.counts.package_media_parts:
            raise ValueError("OOXML media MIME counts do not cover every package part")
        unreferenced = sum(
            item.mapping_kind == "PACKAGE_MEDIA_UNREFERENCED"
            for item in self.occurrences
        )
        if self.counts.unreferenced_media_parts != unreferenced:
            raise ValueError("OOXML unreferenced media part count mismatch")
        return self


def _manifest_hash_payload(manifest: OoxmlMediaManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json", exclude={"manifest_sha256"})


def ooxml_media_manifest_sha256(manifest: OoxmlMediaManifest) -> str:
    return sha256_json(_manifest_hash_payload(manifest))


def _safe_package_part(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or "\x00" in normalized or normalized.startswith("/"):
        raise ValueError(f"Unsafe OOXML package part: {value!r}")
    parts = PurePosixPath(normalized).parts
    if not parts or any(item in {"", ".", ".."} for item in parts):
        raise ValueError(f"Unsafe OOXML package part: {value!r}")
    return "/".join(parts)


def _relationship_part(source_part: str) -> str:
    directory, filename = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", filename + ".rels")


def _source_part_from_relationship_part(relationship_part: str) -> str | None:
    directory, filename = posixpath.split(relationship_part)
    if not filename.endswith(".rels") or posixpath.basename(directory) != "_rels":
        return None
    source_directory = posixpath.dirname(directory)
    source_filename = filename[:-5]
    if not source_filename:
        return None
    if source_filename == ".rels" and not source_directory:
        return None
    return posixpath.join(source_directory, source_filename)


def _resolve_relationship_target(source_part: str, target: str) -> str:
    normalized_target = target.replace("\\", "/")
    if normalized_target.startswith("/"):
        resolved = posixpath.normpath(normalized_target.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_part), normalized_target)
        )
    if resolved in {"", ".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        raise ValueError(
            f"OOXML relationship target escapes its package: {source_part!r} -> {target!r}"
        )
    return _safe_package_part(resolved)


def _relationship_records(
    archive: object,
    source_part: str,
    label: str,
) -> list[tuple[str, str]]:
    relationship_part = _relationship_part(source_part)
    if relationship_part not in archive.namelist():
        return []
    root = _read_xml(archive, relationship_part, label)
    records: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for child in root:
        if child.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
            continue
        relationship_id = child.attrib.get("Id")
        target = child.attrib.get("Target")
        target_mode = child.attrib.get("TargetMode", "Internal")
        if not relationship_id or not target or target_mode.casefold() == "external":
            continue
        if relationship_id in seen_ids:
            raise ValueError(
                f"OOXML relationship ID is duplicated: {label}!/{relationship_part}#{relationship_id}"
            )
        seen_ids.add(relationship_id)
        records.append(
            (relationship_id, _resolve_relationship_target(source_part, target))
        )
    return records


def _alt_text_for_reference(
    reference: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
) -> str | None:
    direct_values: list[str] = []
    for key, value in reference.attrib.items():
        if _local_name(key) in {"title", "descr", "alt"}:
            cleaned = normalize_text(value)
            if cleaned:
                direct_values.append(cleaned)
    current: ElementTree.Element | None = reference
    while current is not None:
        name = _local_name(current.tag)
        if name in {"pic", "sp", "graphicFrame", "drawing", "pict", "object", "bgPr"}:
            values = list(direct_values)
            for item in current.iter():
                if _local_name(item.tag) not in {"cNvPr", "docPr"}:
                    continue
                for key in ("descr", "title"):
                    value = normalize_text(item.attrib.get(key, ""))
                    if value and value not in values:
                        values.append(value)
            if values:
                return normalize_text("; ".join(values))
        current = parents.get(current)
    return normalize_text("; ".join(direct_values)) or None


def _embedded_references(
    root: ElementTree.Element,
) -> list[tuple[str, str | None, int]]:
    parents = {child: parent for parent in root.iter() for child in parent}
    result: list[tuple[str, str | None, int]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        relationship_id = None
        if name == "blip":
            relationship_id = element.attrib.get(f"{{{_OFFICE_REL_NS}}}embed")
        elif name == "imagedata":
            relationship_id = element.attrib.get(f"{{{_OFFICE_REL_NS}}}id")
        if relationship_id:
            result.append(
                (
                    relationship_id,
                    _alt_text_for_reference(element, parents),
                    len(result),
                )
            )
    return result


def _docx_body_locations(
    root: ElementTree.Element,
) -> list[tuple[ElementTree.Element, int, str | None]]:
    body = next((item for item in root if _local_name(item.tag) == "body"), None)
    if body is None:
        return []
    locations: list[tuple[ElementTree.Element, int, str | None]] = []
    page_number = 1
    current_chars = 0
    current_has_text = False
    section_title: str | None = None
    for child in body:
        name = _local_name(child.tag)
        style = ""
        text = ""
        if name == "p":
            style = _paragraph_style(child)
            text = _word_paragraph_text(child)
        elif name == "tbl":
            style = "Table"
            text = _table_text(child)
        starts_section = bool(text and _is_heading(style) and current_has_text)
        exceeds_limit = bool(
            text
            and current_has_text
            and current_chars + len(text) + 1 > DOCX_VIRTUAL_PAGE_CHARS
        )
        if starts_section or exceeds_limit:
            page_number += 1
            current_chars = 0
            current_has_text = False
        if text and _is_heading(style):
            section_title = text
        locations.append((child, page_number, section_title))
        if text:
            current_chars += len(text) + 1
            current_has_text = True
    return locations


def _media_parts(archive: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        part = _safe_package_part(info.filename)
        if not (part.startswith("ppt/media/") or part.startswith("word/media/")):
            continue
        if part in seen:
            raise ValueError(f"OOXML package contains a duplicate media part: {part}")
        seen.add(part)
        result.append(part)
    return sorted(result)


def _mime_type(part: str) -> str:
    return _MIME_TYPES.get(PurePosixPath(part).suffix.casefold(), "application/octet-stream")


def _write_content_addressed(
    *,
    raw: bytes,
    digest: str,
    suffix: str,
    output_dir: Path,
) -> str:
    filename = f"media_{digest.removeprefix('sha256:')}{suffix}"
    relative_path = str(PurePosixPath(output_dir.name) / filename)
    destination = output_dir / filename
    if destination.is_symlink():
        raise ValueError(f"OOXML media destination may not be a symlink: {destination}")
    if destination.is_file():
        if sha256_bytes(destination.read_bytes()) != digest:
            raise ValueError(f"Existing OOXML media asset is stale: {destination}")
        return relative_path
    temporary = output_dir / f".{filename}.{os.getpid()}.partial"
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"OOXML media temporary path already exists: {temporary}")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
        os.replace(temporary, destination)
    finally:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
    return relative_path


def export_ooxml_media(
    documents: list[Document],
    *,
    output_dir: Path,
) -> OoxmlMediaManifest:
    """Export all OOXML package media with exact slide/section provenance where available."""

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_assets: dict[str, dict[str, object]] = {}
    raw_occurrences: list[dict[str, object]] = []
    documents_scanned = 0
    documents_with_media: set[str] = set()
    package_media_parts = 0
    unreferenced_media_parts = 0
    mime_part_counts: Counter[str] = Counter()

    for document in sorted(documents, key=lambda item: item.document_id):
        source_format = document.source_format
        if source_format not in {"PPTX", "DOCX"}:
            continue
        if not document.source_path:
            raise ValueError(f"OOXML document has no source path: {document.document_id}")
        source_path = Path(document.source_path)
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError(f"OOXML source is unavailable: {source_path}")
        package_raw = source_path.read_bytes()
        if sha256_bytes(package_raw) != document.sha256:
            raise ValueError(f"OOXML source hash mismatch: {source_path}")
        documents_scanned += 1

        with _open_ooxml(package_raw, document.display_name or document.filename) as archive:
            media_parts = _media_parts(archive)
            if media_parts:
                documents_with_media.add(document.document_id)
            package_media_parts += len(media_parts)
            media_by_part: dict[str, tuple[str, str]] = {}
            for part in media_parts:
                raw = archive.read(part)
                if not raw:
                    raise ValueError(f"OOXML media part is empty: {document.document_id}!/{part}")
                digest = sha256_bytes(raw)
                suffix = PurePosixPath(part).suffix.casefold() or ".bin"
                mime_type = _mime_type(part)
                mime_part_counts[mime_type] += 1
                media_by_part[part] = (digest, mime_type)
                if digest not in raw_assets:
                    extracted_path = _write_content_addressed(
                        raw=raw,
                        digest=digest,
                        suffix=suffix,
                        output_dir=output_dir,
                    )
                    raw_assets[digest] = {
                        "media_id": stable_id("media", digest, length=24),
                        "sha256": digest,
                        "mime_type": mime_type,
                        "suffix": suffix,
                        "byte_size": len(raw),
                        "extracted_path": extracted_path,
                        "occurrence_ids": [],
                        "document_ids": [],
                    }

            page_by_slide: dict[str, int] = {}
            mapped_relationships: set[tuple[str, str, str]] = set()
            if source_format == "PPTX":
                slide_paths = _presentation_slide_paths(
                    archive,
                    document.display_name or document.filename,
                )
                page_by_slide = {
                    slide_path: page_number
                    for page_number, slide_path in enumerate(slide_paths, start=1)
                }
                for slide_path, page_number in page_by_slide.items():
                    slide = _read_xml(
                        archive,
                        slide_path,
                        document.display_name or document.filename,
                    )
                    relationships = dict(_relationship_records(
                        archive,
                        slide_path,
                        document.display_name or document.filename,
                    ))
                    slide_text = _text_from_drawing_xml(slide)
                    section_title = slide_text.splitlines()[0] if slide_text else None
                    for relationship_id, alt_text, embedding_index in _embedded_references(slide):
                        package_part = relationships.get(relationship_id)
                        if package_part not in media_by_part:
                            continue
                        digest, _ = media_by_part[package_part]
                        media_id = raw_assets[digest]["media_id"]
                        occurrence_id = stable_id(
                            "media_occ",
                            document.document_id,
                            slide_path,
                            relationship_id,
                            str(embedding_index),
                            length=24,
                        )
                        raw_occurrences.append({
                            "occurrence_id": occurrence_id,
                            "media_id": media_id,
                            "document_id": document.document_id,
                            "source_format": source_format,
                            "package_part": package_part,
                            "relationship_source_part": slide_path,
                            "relationship_id": relationship_id,
                            "page_number": page_number,
                            "section_title": section_title,
                            "alt_text": alt_text,
                            "mapping_kind": "PPTX_SLIDE_EMBED",
                        })
                        mapped_relationships.add((slide_path, relationship_id, package_part))
            else:
                main_part = "word/document.xml"
                root = _read_xml(
                    archive,
                    main_part,
                    document.display_name or document.filename,
                )
                relationships = dict(_relationship_records(
                    archive,
                    main_part,
                    document.display_name or document.filename,
                ))
                embedding_index = 0
                for element, page_number, section_title in _docx_body_locations(root):
                    for relationship_id, alt_text, _ in _embedded_references(element):
                        package_part = relationships.get(relationship_id)
                        if package_part not in media_by_part:
                            continue
                        digest, _ = media_by_part[package_part]
                        media_id = raw_assets[digest]["media_id"]
                        occurrence_id = stable_id(
                            "media_occ",
                            document.document_id,
                            main_part,
                            relationship_id,
                            str(embedding_index),
                            length=24,
                        )
                        embedding_index += 1
                        raw_occurrences.append({
                            "occurrence_id": occurrence_id,
                            "media_id": media_id,
                            "document_id": document.document_id,
                            "source_format": source_format,
                            "package_part": package_part,
                            "relationship_source_part": main_part,
                            "relationship_id": relationship_id,
                            "page_number": page_number,
                            "section_title": section_title,
                            "alt_text": alt_text,
                            "mapping_kind": "DOCX_BODY_EMBED",
                        })
                        mapped_relationships.add((main_part, relationship_id, package_part))

            related_parts: set[str] = set()
            for info in archive.infolist():
                relationship_part = info.filename.replace("\\", "/")
                if not relationship_part.endswith(".rels"):
                    continue
                source_part = _source_part_from_relationship_part(relationship_part)
                if not source_part:
                    continue
                for relationship_id, package_part in _relationship_records(
                    archive,
                    source_part,
                    document.display_name or document.filename,
                ):
                    if package_part not in media_by_part:
                        continue
                    related_parts.add(package_part)
                    key = (source_part, relationship_id, package_part)
                    if key in mapped_relationships:
                        continue
                    digest, _ = media_by_part[package_part]
                    media_id = raw_assets[digest]["media_id"]
                    page_number = page_by_slide.get(source_part)
                    if source_format == "PPTX":
                        mapping_kind = (
                            "PPTX_SLIDE_RELATIONSHIP"
                            if page_number is not None
                            else "PPTX_OTHER_RELATIONSHIP"
                        )
                    else:
                        mapping_kind = (
                            "DOCX_BODY_RELATIONSHIP"
                            if source_part == "word/document.xml"
                            else "DOCX_OTHER_RELATIONSHIP"
                        )
                    occurrence_id = stable_id(
                        "media_occ",
                        document.document_id,
                        source_part,
                        relationship_id,
                        package_part,
                        "relationship",
                        length=24,
                    )
                    raw_occurrences.append({
                        "occurrence_id": occurrence_id,
                        "media_id": media_id,
                        "document_id": document.document_id,
                        "source_format": source_format,
                        "package_part": package_part,
                        "relationship_source_part": source_part,
                        "relationship_id": relationship_id,
                        "page_number": page_number,
                        "section_title": None,
                        "alt_text": None,
                        "mapping_kind": mapping_kind,
                    })

            for package_part in sorted(set(media_parts) - related_parts):
                unreferenced_media_parts += 1
                digest, _ = media_by_part[package_part]
                media_id = raw_assets[digest]["media_id"]
                occurrence_id = stable_id(
                    "media_occ",
                    document.document_id,
                    package_part,
                    "unreferenced",
                    length=24,
                )
                raw_occurrences.append({
                    "occurrence_id": occurrence_id,
                    "media_id": media_id,
                    "document_id": document.document_id,
                    "source_format": source_format,
                    "package_part": package_part,
                    "relationship_source_part": None,
                    "relationship_id": None,
                    "page_number": None,
                    "section_title": None,
                    "alt_text": None,
                    "mapping_kind": "PACKAGE_MEDIA_UNREFERENCED",
                })

    occurrences = [
        OoxmlMediaOccurrence.model_validate(item)
        for item in sorted(raw_occurrences, key=lambda value: str(value["occurrence_id"]))
    ]
    assets_by_id = {
        str(item["media_id"]): item
        for item in raw_assets.values()
    }
    for occurrence in occurrences:
        asset = assets_by_id[occurrence.media_id]
        asset["occurrence_ids"].append(occurrence.occurrence_id)
        if occurrence.document_id not in asset["document_ids"]:
            asset["document_ids"].append(occurrence.document_id)
    assets = [
        OoxmlMediaAsset.model_validate(item)
        for item in sorted(raw_assets.values(), key=lambda value: str(value["media_id"]))
    ]
    placeholder = "sha256:" + "0" * 64
    manifest = OoxmlMediaManifest(
        manifest_sha256=placeholder,
        assets=assets,
        occurrences=occurrences,
        counts=OoxmlMediaCounts(
            documents_scanned=documents_scanned,
            documents_with_media=len(documents_with_media),
            package_media_parts=package_media_parts,
            media_occurrences=len(occurrences),
            unique_assets=len(assets),
            exact_duplicate_media_parts=package_media_parts - len(assets),
            unreferenced_media_parts=unreferenced_media_parts,
            by_mime_type=dict(sorted(mime_part_counts.items())),
        ),
    )
    return manifest.model_copy(
        update={"manifest_sha256": ooxml_media_manifest_sha256(manifest)}
    )
