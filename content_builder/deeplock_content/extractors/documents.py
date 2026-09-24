from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from ..models import Document
from ..util import normalize_text, safe_filename, sha256_bytes, stable_id
from .archives import ArchiveInventory, DOCUMENT_SUFFIXES, MEDIA_TYPES
from .ooxml import extract_docx, extract_pptx


SUPPORTED_SUFFIXES = DOCUMENT_SUFFIXES
SOURCE_FORMATS = {
    ".pdf": "PDF",
    ".pptx": "PPTX",
    ".docx": "DOCX",
    ".md": "MARKDOWN",
    ".markdown": "MARKDOWN",
    ".txt": "TEXT",
}


class OcrRequiredError(RuntimeError):
    pass


def _extract_pdf_bytes(
    raw: bytes,
    label: str,
    *,
    require_text_layer: bool,
) -> list[tuple[int, str]]:
    import pymupdf

    result: list[tuple[int, str]] = []
    previous_error_display = bool(pymupdf.TOOLS.mupdf_display_errors())
    previous_warning_display = bool(pymupdf.TOOLS.mupdf_display_warnings())
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as document:
            for index, page in enumerate(document):
                result.append((index + 1, normalize_text(page.get_text("text"))))
    except Exception as error:
        raise ValueError(f"Invalid or unreadable PDF: {label}: {error}") from error
    finally:
        pymupdf.TOOLS.mupdf_display_errors(previous_error_display)
        pymupdf.TOOLS.mupdf_display_warnings(previous_warning_display)
    if not result:
        raise ValueError(f"PDF contains no pages: {label}")
    if require_text_layer:
        useful = [len(text) for _, text in result]
        empty_ratio = sum(length < 10 for length in useful) / len(useful)
        average = sum(useful) / len(useful)
        if average < 40 or empty_ratio > 0.60:
            raise OcrRequiredError(f"OCR_REQUIRED: {label}")
    return result


def _extract_text_bytes(raw: bytes, label: str) -> list[tuple[int, str]]:
    try:
        text = normalize_text(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ValueError(f"Text document must be UTF-8: {label}") from error
    if not text:
        raise ValueError(f"Text document is empty: {label}")
    return [(1, text)]


def extract_document_bytes(
    raw: bytes,
    *,
    suffix: str,
    display_name: str,
    source_path: str,
    logical_path: str | None = None,
    source_aliases: list[str] | None = None,
    filename: str | None = None,
    require_pdf_text_layer: bool = True,
) -> tuple[Document, list[tuple[int, str]]]:
    suffix = suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported document type: {suffix}")
    digest = sha256_bytes(raw)
    if suffix == ".pdf":
        pages = _extract_pdf_bytes(
            raw,
            display_name,
            require_text_layer=require_pdf_text_layer,
        )
    elif suffix == ".pptx":
        pages = extract_pptx(raw, display_name)
    elif suffix == ".docx":
        pages = extract_docx(raw, display_name)
    else:
        pages = _extract_text_bytes(raw, display_name)
    document = Document(
        document_id=stable_id("doc", digest),
        filename=filename or safe_filename(display_name),
        mime_type=MEDIA_TYPES.get(suffix) or mimetypes.guess_type(display_name)[0] or "text/plain",
        sha256=digest,
        page_count=len(pages),
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        source_path=source_path,
        display_name=display_name,
        logical_path=logical_path,
        source_aliases=list(source_aliases or []),
        source_format=SOURCE_FORMATS[suffix],
    )
    return document, pages


def discover_inputs(path: Path) -> list[Path]:
    if path.is_file():
        candidates = [path]
    elif path.is_dir():
        candidates = sorted(item for item in path.rglob("*") if item.is_file())
    else:
        raise FileNotFoundError(path)
    result = [item.resolve() for item in candidates if item.suffix.lower() in SUPPORTED_SUFFIXES]
    if not result:
        raise ValueError("No supported PDF/PPTX/DOCX/Markdown/TXT files found")
    return result


def extract_inputs(path: Path) -> list[tuple[Document, list[tuple[int, str]]]]:
    output: list[tuple[Document, list[tuple[int, str]]]] = []
    for item in discover_inputs(path):
        output.append(extract_document_bytes(
            item.read_bytes(),
            suffix=item.suffix,
            display_name=item.name,
            source_path=str(item),
        ))
    return output


def extract_inventory_documents(
    inventory: ArchiveInventory,
    *,
    workdir: Path,
    require_pdf_text_layer: bool = False,
) -> list[tuple[Document, list[tuple[int, str]]]]:
    """Extract every unique inventory document while preserving all archive aliases."""

    occurrences = {item.occurrence_id: item for item in inventory.occurrences}
    output: list[tuple[Document, list[tuple[int, str]]]] = []
    for item in inventory.documents:
        canonical = occurrences[item.canonical_occurrence_id]
        unresolved = workdir / Path(item.extracted_path)
        if unresolved.is_symlink():
            raise ValueError(f"Extracted inventory document may not be a symlink: {unresolved}")
        source = unresolved.resolve()
        try:
            source.relative_to(workdir.resolve())
        except ValueError as error:
            raise ValueError(f"Inventory extracted path escapes its workdir: {item.extracted_path}") from error
        if not source.is_file():
            raise ValueError(f"Missing extracted inventory document: {source}")
        raw = source.read_bytes()
        if len(raw) != item.byte_size or sha256_bytes(raw) != item.sha256:
            raise ValueError(f"Extracted inventory document hash mismatch: {source}")
        document, pages = extract_document_bytes(
            raw,
            suffix=item.suffix,
            display_name=Path(canonical.member_path).name,
            source_path=str(source),
            logical_path=canonical.logical_path,
            source_aliases=list(item.logical_paths),
            filename=Path(item.extracted_path).name,
            require_pdf_text_layer=require_pdf_text_layer,
        )
        if document.document_id != item.document_id:
            raise ValueError(f"Inventory document identity mismatch: {source}")
        output.append((document, pages))
    return output
