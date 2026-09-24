from __future__ import annotations

import io
import posixpath
import re
import stat
import zipfile
from collections.abc import Iterable
from pathlib import PurePosixPath
from xml.etree import ElementTree

from ..util import normalize_text


MAX_OOXML_ENTRIES = 20_000
MAX_OOXML_UNCOMPRESSED_BYTES = 768 * 1024 * 1024
MAX_OOXML_MEMBER_BYTES = 256 * 1024 * 1024
MAX_OOXML_COMPRESSION_RATIO = 100.0
MAX_XML_MEMBER_BYTES = 64 * 1024 * 1024
DOCX_VIRTUAL_PAGE_CHARS = 12_000

_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _open_ooxml(raw: bytes, label: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise ValueError(f"Invalid OOXML document: {label}") from error
    infos = [item for item in archive.infolist() if not item.is_dir()]
    try:
        if len(infos) > MAX_OOXML_ENTRIES:
            raise ValueError(f"OOXML document has too many members: {label}")
        if sum(item.file_size for item in infos) > MAX_OOXML_UNCOMPRESSED_BYTES:
            raise ValueError(f"OOXML document expands past the safety limit: {label}")
        seen_parts: set[str] = set()
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            if (
                not normalized
                or "\x00" in normalized
                or normalized.startswith("/")
                or not parts
                or any(item in {"", ".", ".."} for item in parts)
            ):
                raise ValueError(f"OOXML package contains an unsafe member: {label}!/{info.filename}")
            canonical = "/".join(parts)
            if canonical in seen_parts:
                raise ValueError(f"OOXML package contains a duplicate member: {label}!/{canonical}")
            seen_parts.add(canonical)
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise ValueError(f"OOXML symlink member is not allowed: {label}!/{canonical}")
            if info.flag_bits & 0x1:
                raise ValueError(f"Encrypted OOXML member is not allowed: {label}!/{canonical}")
            if info.file_size > MAX_OOXML_MEMBER_BYTES:
                raise ValueError(f"OOXML member is too large: {label}!/{canonical}")
            if info.file_size and info.compress_size == 0:
                raise ValueError(f"OOXML member has an invalid compressed size: {label}!/{canonical}")
            if info.file_size / max(1, info.compress_size) > MAX_OOXML_COMPRESSION_RATIO:
                raise ValueError(f"OOXML member compression ratio is unsafe: {label}!/{canonical}")
    except Exception:
        archive.close()
        raise
    return archive


def _read_xml(archive: zipfile.ZipFile, name: str, label: str) -> ElementTree.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise ValueError(f"OOXML document is missing {name}: {label}") from error
    if info.file_size > MAX_XML_MEMBER_BYTES:
        raise ValueError(f"OOXML XML member is too large: {label}!/{name}")
    try:
        payload = archive.read(info)
        if re.search(br"<!DOCTYPE|<!ENTITY", payload, flags=re.IGNORECASE):
            raise ValueError(f"OOXML XML declarations are unsafe: {label}!/{name}")
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ValueError(f"Malformed OOXML XML: {label}!/{name}") from error


def _relationship_targets(
    archive: zipfile.ZipFile,
    relationships_path: str,
    label: str,
) -> dict[str, str]:
    root = _read_xml(archive, relationships_path, label)
    result: dict[str, str] = {}
    for child in root:
        if child.tag != f"{{{_RELATIONSHIPS_NS}}}Relationship":
            continue
        relationship_id = child.attrib.get("Id")
        target = child.attrib.get("Target")
        target_mode = child.attrib.get("TargetMode", "Internal")
        if relationship_id and target and target_mode.casefold() != "external":
            if relationship_id in result:
                raise ValueError(
                    f"OOXML relationship ID is duplicated: {label}!/{relationships_path}#{relationship_id}"
                )
            result[relationship_id] = target.replace("\\", "/")
    return result


def _presentation_slide_paths(archive: zipfile.ZipFile, label: str) -> list[str]:
    presentation = _read_xml(archive, "ppt/presentation.xml", label)
    relationships = _relationship_targets(
        archive,
        "ppt/_rels/presentation.xml.rels",
        label,
    )
    result: list[str] = []
    for slide_id in presentation.findall(
        f".//{{{_PRESENTATION_NS}}}sldId"
    ):
        relationship_id = slide_id.attrib.get(f"{{{_OFFICE_REL_NS}}}id")
        if not relationship_id or relationship_id not in relationships:
            raise ValueError(f"PPTX slide order has a missing relationship: {label}")
        target = relationships[relationship_id]
        path = (
            posixpath.normpath(target.lstrip("/"))
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("ppt", target))
        )
        if path.startswith("../") or not path.startswith("ppt/slides/"):
            raise ValueError(f"PPTX slide relationship escapes its package: {label}")
        result.append(path)
    if not result:
        raise ValueError(f"PPTX contains no slides: {label}")
    if len(result) != len(set(result)):
        raise ValueError(f"PPTX presentation references a slide more than once: {label}")
    return result


def _text_from_drawing_xml(root: ElementTree.Element) -> str:
    lines: list[str] = []
    for paragraph in root.iter():
        if _local_name(paragraph.tag) != "p":
            continue
        values = [
            item.text or ""
            for item in paragraph.iter()
            if _local_name(item.tag) in {"t", "br"}
        ]
        value = normalize_text(" ".join(item for item in values if item))
        if value:
            lines.append(value)
    descriptions: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "cNvPr":
            continue
        for key in ("title", "descr"):
            value = normalize_text(element.attrib.get(key, ""))
            if value and value not in descriptions:
                descriptions.append(value)
    lines.extend(f"Mô tả hình: {item}" for item in descriptions)
    return normalize_text("\n".join(lines))


def extract_pptx(raw: bytes, label: str) -> list[tuple[int, str]]:
    """Extract visible slide text in actual presentation order."""

    with _open_ooxml(raw, label) as archive:
        pages: list[tuple[int, str]] = []
        for page_number, slide_path in enumerate(
            _presentation_slide_paths(archive, label),
            start=1,
        ):
            slide = _read_xml(archive, slide_path, label)
            pages.append((page_number, _text_from_drawing_xml(slide)))
        return pages


def _word_paragraph_text(paragraph: ElementTree.Element) -> str:
    values: list[str] = []
    for element in paragraph.iter():
        name = _local_name(element.tag)
        if name == "t":
            values.append(element.text or "")
        elif name in {"tab", "br", "cr"}:
            values.append("\n" if name != "tab" else "\t")
    return normalize_text("".join(values))


def _paragraph_style(paragraph: ElementTree.Element) -> str:
    properties = paragraph.find(f"{{{_WORD_NS}}}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{{{_WORD_NS}}}pStyle")
    if style is None:
        return ""
    return style.attrib.get(f"{{{_WORD_NS}}}val", "")


def _table_text(table: ElementTree.Element) -> str:
    rows: list[str] = []
    for row in table:
        if _local_name(row.tag) != "tr":
            continue
        cells: list[str] = []
        for cell in row:
            if _local_name(cell.tag) != "tc":
                continue
            paragraphs = [
                _word_paragraph_text(item)
                for item in cell.iter()
                if _local_name(item.tag) == "p"
            ]
            cells.append(" / ".join(item for item in paragraphs if item))
        if any(cells):
            rows.append(" | ".join(cells))
    return normalize_text("\n".join(rows))


def _main_document_blocks(root: ElementTree.Element) -> Iterable[tuple[str, str]]:
    body = root.find(f"{{{_WORD_NS}}}body")
    if body is None:
        return []
    result: list[tuple[str, str]] = []
    for child in body:
        name = _local_name(child.tag)
        if name == "p":
            text = _word_paragraph_text(child)
            if text:
                result.append((_paragraph_style(child), text))
        elif name == "tbl":
            text = _table_text(child)
            if text:
                result.append(("Table", text))
    return result


def _is_heading(style: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", style.casefold())
    return normalized.startswith("heading") or normalized in {
        "title",
        "subtitle",
        "tieude",
    }


def _append_virtual_page(
    pages: list[str],
    blocks: list[str],
) -> None:
    text = normalize_text("\n".join(blocks))
    if text:
        pages.append(text)


def extract_docx(raw: bytes, label: str) -> list[tuple[int, str]]:
    """Extract DOCX body in heading-aware, size-bounded virtual pages."""

    with _open_ooxml(raw, label) as archive:
        root = _read_xml(archive, "word/document.xml", label)
        pages: list[str] = []
        current: list[str] = []
        current_chars = 0
        for style, text in _main_document_blocks(root):
            starts_section = _is_heading(style) and bool(current)
            exceeds_limit = current and current_chars + len(text) + 1 > DOCX_VIRTUAL_PAGE_CHARS
            if starts_section or exceeds_limit:
                _append_virtual_page(pages, current)
                current = []
                current_chars = 0
            current.append(text)
            current_chars += len(text) + 1
        _append_virtual_page(pages, current)
        if not pages:
            raise ValueError(f"DOCX contains no extractable body text: {label}")
        return list(enumerate(pages, start=1))
