from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from ..models import StrictModel
from ..util import sha256_json, stable_id


DOCUMENT_SUFFIXES = {".pdf", ".pptx", ".docx", ".md", ".markdown", ".txt"}
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 768 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


class ArchiveDescriptor(StrictModel):
    archive_id: str
    name: str
    source_path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: int = Field(ge=1)
    entry_count: int = Field(ge=0)
    total_uncompressed_bytes: int = Field(ge=0)


class InventoryOccurrence(StrictModel):
    occurrence_id: str
    archive_id: str
    archive_index: int = Field(ge=0)
    entry_index: int = Field(ge=0)
    member_path: str
    logical_path: str
    suffix: str
    media_type: str
    byte_size: int = Field(ge=0)
    compressed_size: int = Field(ge=0)
    status: Literal["IN_SCOPE_DOCUMENT", "ANCILLARY"]
    document_id: str | None = None
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reason: str | None = None


class InventoryDocument(StrictModel):
    document_id: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    suffix: str
    media_type: str
    byte_size: int = Field(ge=1)
    canonical_occurrence_id: str
    occurrence_ids: list[str] = Field(min_length=1)
    logical_paths: list[str] = Field(min_length=1)
    extracted_path: str


class InventoryCounts(StrictModel):
    archive_count: int = Field(ge=1)
    entry_count: int = Field(ge=0)
    document_occurrences: int = Field(ge=0)
    unique_documents: int = Field(ge=0)
    exact_duplicate_occurrences: int = Field(ge=0)
    ancillary_occurrences: int = Field(ge=0)
    by_suffix: dict[str, int]


class ArchiveInventory(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    archives: list[ArchiveDescriptor] = Field(min_length=1)
    documents: list[InventoryDocument]
    occurrences: list[InventoryOccurrence]
    counts: InventoryCounts

    @model_validator(mode="after")
    def internally_consistent(self) -> "ArchiveInventory":
        archive_ids = [item.archive_id for item in self.archives]
        occurrence_ids = [item.occurrence_id for item in self.occurrences]
        document_ids = [item.document_id for item in self.documents]
        if len(archive_ids) != len(set(archive_ids)):
            raise ValueError("archive IDs must be unique")
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ValueError("occurrence IDs must be unique")
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("document IDs must be unique")
        known_archives = set(archive_ids)
        known_occurrences = set(occurrence_ids)
        in_scope = [item for item in self.occurrences if item.status == "IN_SCOPE_DOCUMENT"]
        if any(item.archive_id not in known_archives for item in self.occurrences):
            raise ValueError("occurrence references an unknown archive")
        if any(item.document_id not in set(document_ids) for item in in_scope):
            raise ValueError("in-scope occurrence references an unknown document")
        for document in self.documents:
            if document.canonical_occurrence_id not in document.occurrence_ids:
                raise ValueError("canonical occurrence must belong to its document")
            if len(document.occurrence_ids) != len(document.logical_paths):
                raise ValueError("document occurrence IDs and logical paths must align")
            if any(item not in known_occurrences for item in document.occurrence_ids):
                raise ValueError("document references an unknown occurrence")
        expected = self.counts
        if expected.archive_count != len(self.archives):
            raise ValueError("archive count mismatch")
        if expected.entry_count != len(self.occurrences):
            raise ValueError("entry count mismatch")
        if expected.document_occurrences != len(in_scope):
            raise ValueError("document occurrence count mismatch")
        if expected.unique_documents != len(self.documents):
            raise ValueError("unique document count mismatch")
        if expected.exact_duplicate_occurrences != len(in_scope) - len(self.documents):
            raise ValueError("duplicate occurrence count mismatch")
        if expected.ancillary_occurrences != len(self.occurrences) - len(in_scope):
            raise ValueError("ancillary occurrence count mismatch")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _normalized_member_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if not normalized or "\x00" in normalized:
        raise ValueError("Archive contains an empty or NUL member path")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"Archive member path is absolute: {value!r}")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Archive member path is unsafe: {value!r}")
    return "/".join(parts)


def _validate_archive_info(info: zipfile.ZipInfo, member_path: str) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise ValueError(f"Archive symlink is not allowed: {member_path}")
    if info.flag_bits & 0x1:
        raise ValueError(f"Encrypted archive member is not allowed: {member_path}")
    if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError(f"Archive member is too large: {member_path}")
    if info.file_size and info.compress_size == 0:
        raise ValueError(f"Archive member has an invalid compressed size: {member_path}")
    ratio = info.file_size / max(1, info.compress_size)
    if ratio > MAX_COMPRESSION_RATIO:
        raise ValueError(f"Archive member compression ratio is unsafe: {member_path}")


def _stream_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    temporary_path: Path | None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    if temporary_path is not None and (temporary_path.exists() or temporary_path.is_symlink()):
        raise ValueError(f"Archive extraction temporary path exists: {temporary_path}")
    destination = temporary_path.open("xb") if temporary_path is not None else None
    try:
        with archive.open(info, "r") as source:
            while block := source.read(1024 * 1024):
                byte_count += len(block)
                if byte_count > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(f"Archive member expanded past its declared limit: {info.filename}")
                digest.update(block)
                if destination is not None:
                    destination.write(block)
    except Exception:
        if destination is not None:
            destination.close()
        if temporary_path is not None and temporary_path.is_file() and not temporary_path.is_symlink():
            temporary_path.unlink()
        raise
    else:
        if destination is not None:
            destination.close()
    if byte_count != info.file_size:
        raise ValueError(f"Archive member size/CRC verification failed: {info.filename}")
    return "sha256:" + digest.hexdigest(), byte_count


def _inventory_hash_payload(inventory: ArchiveInventory) -> dict[str, object]:
    payload = inventory.model_dump(mode="json", exclude={"inventory_sha256"})
    for archive in payload["archives"]:
        archive.pop("source_path", None)
    return payload


def inventory_sha256(inventory: ArchiveInventory) -> str:
    return sha256_json(_inventory_hash_payload(inventory))


def build_archive_inventory(
    archive_paths: list[Path],
    *,
    extract_dir: Path | None = None,
) -> ArchiveInventory:
    """Audit ZIPs and optionally extract one content-addressed copy per unique document."""

    if not archive_paths:
        raise ValueError("At least one archive is required")
    extracted_prefix = "documents"
    if extract_dir is not None:
        extract_dir.mkdir(parents=True, exist_ok=True)
        extracted_prefix = extract_dir.resolve().name
        if not extracted_prefix or PurePosixPath(extracted_prefix).name != extracted_prefix:
            raise ValueError(f"Archive extraction directory has an unsafe name: {extract_dir}")

    archives: list[ArchiveDescriptor] = []
    occurrences: list[InventoryOccurrence] = []
    document_rows: dict[str, dict[str, object]] = {}
    suffix_counts: Counter[str] = Counter()

    for archive_index, raw_path in enumerate(archive_paths):
        path = raw_path.resolve()
        if path.suffix.lower() != ".zip" or not path.is_file():
            raise ValueError(f"Only readable ZIP archives are supported: {raw_path}")
        archive_digest = _sha256_file(path)
        archive_id = stable_id("archive", archive_digest)
        with zipfile.ZipFile(path) as archive:
            file_infos = [item for item in archive.infolist() if not item.is_dir()]
            if len(file_infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(f"Archive has too many entries: {path.name}")
            total_uncompressed = sum(item.file_size for item in file_infos)
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError(f"Archive expands past the safety limit: {path.name}")
            archives.append(ArchiveDescriptor(
                archive_id=archive_id,
                name=unicodedata.normalize("NFC", path.name),
                source_path=str(path),
                sha256=archive_digest,
                byte_size=path.stat().st_size,
                entry_count=len(file_infos),
                total_uncompressed_bytes=total_uncompressed,
            ))
            for entry_index, info in enumerate(file_infos):
                member_path = _normalized_member_path(info.filename)
                _validate_archive_info(info, member_path)
                suffix = PurePosixPath(member_path).suffix.lower()
                logical_path = f"{path.name}/{member_path}"
                occurrence_id = stable_id(
                    "occurrence",
                    archive_digest,
                    str(entry_index),
                    member_path,
                )
                if suffix not in DOCUMENT_SUFFIXES:
                    occurrences.append(InventoryOccurrence(
                        occurrence_id=occurrence_id,
                        archive_id=archive_id,
                        archive_index=archive_index,
                        entry_index=entry_index,
                        member_path=member_path,
                        logical_path=logical_path,
                        suffix=suffix,
                        media_type="application/octet-stream",
                        byte_size=info.file_size,
                        compressed_size=info.compress_size,
                        status="ANCILLARY",
                        reason="UNSUPPORTED_NON_DOCUMENT",
                    ))
                    continue
                if info.file_size == 0:
                    raise ValueError(f"In-scope document is empty: {logical_path}")

                temporary_path = None
                if extract_dir is not None:
                    temporary_path = extract_dir / f".{occurrence_id}.{os.getpid()}.partial"
                digest, byte_size = _stream_member(archive, info, temporary_path)
                document_id = stable_id("doc", digest)
                suffix_counts[suffix] += 1
                row = document_rows.get(digest)
                if row is None:
                    filename = f"{document_id}{suffix}"
                    relative_path = str(PurePosixPath(extracted_prefix) / filename)
                    if temporary_path is not None:
                        final_path = extract_dir / filename
                        if final_path.is_symlink():
                            raise ValueError(f"Extracted document may not be a symlink: {final_path}")
                        if final_path.is_file():
                            if final_path.stat().st_size != byte_size or _sha256_file(final_path) != digest:
                                raise ValueError(f"Existing extracted document is stale: {final_path}")
                            temporary_path.unlink()
                        else:
                            os.replace(temporary_path, final_path)
                    row = {
                        "document_id": document_id,
                        "sha256": digest,
                        "suffix": suffix,
                        "media_type": MEDIA_TYPES[suffix],
                        "byte_size": byte_size,
                        "canonical_occurrence_id": occurrence_id,
                        "occurrence_ids": [],
                        "logical_paths": [],
                        "extracted_path": relative_path,
                    }
                    document_rows[digest] = row
                elif temporary_path is not None:
                    temporary_path.unlink()
                row["occurrence_ids"].append(occurrence_id)
                row["logical_paths"].append(logical_path)
                occurrences.append(InventoryOccurrence(
                    occurrence_id=occurrence_id,
                    archive_id=archive_id,
                    archive_index=archive_index,
                    entry_index=entry_index,
                    member_path=member_path,
                    logical_path=logical_path,
                    suffix=suffix,
                    media_type=MEDIA_TYPES[suffix],
                    byte_size=byte_size,
                    compressed_size=info.compress_size,
                    status="IN_SCOPE_DOCUMENT",
                    document_id=document_id,
                    sha256=digest,
                ))

    documents = [InventoryDocument.model_validate(row) for row in document_rows.values()]
    in_scope_count = sum(item.status == "IN_SCOPE_DOCUMENT" for item in occurrences)
    counts = InventoryCounts(
        archive_count=len(archives),
        entry_count=len(occurrences),
        document_occurrences=in_scope_count,
        unique_documents=len(documents),
        exact_duplicate_occurrences=in_scope_count - len(documents),
        ancillary_occurrences=len(occurrences) - in_scope_count,
        by_suffix=dict(sorted(suffix_counts.items())),
    )
    placeholder = "sha256:" + "0" * 64
    inventory = ArchiveInventory(
        inventory_sha256=placeholder,
        archives=archives,
        documents=documents,
        occurrences=occurrences,
        counts=counts,
    )
    return inventory.model_copy(update={"inventory_sha256": inventory_sha256(inventory)})
