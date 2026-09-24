from __future__ import annotations

import re

from ..models import DocumentChunk, ExtractedCorpus, SourceRef
from ..util import normalize_text, sha256_json, sha256_bytes, stable_id


def _sections(text: str) -> list[tuple[list[str], str]]:
    headings: list[str] = []
    current: list[str] = []
    output: list[tuple[list[str], str]] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            if current:
                output.append((headings.copy(), normalize_text("\n".join(current))))
            level = len(match.group(1))
            headings = headings[: level - 1] + [match.group(2).strip()]
            current = [line]
        else:
            current.append(line)
    if current:
        output.append((headings.copy(), normalize_text("\n".join(current))))
    return [(heading, body) for heading, body in output if body]


def _split_long(text: str, max_chars: int = 5200, overlap: int = 700) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    result: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = text.rfind("\n\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary
        result.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def build_corpus(extracted: list[tuple[object, list[tuple[int, str]]]]) -> ExtractedCorpus:
    documents = [item[0] for item in extracted]
    chunks: list[DocumentChunk] = []
    for document, pages in extracted:
        for page_number, page_text in pages:
            for heading_path, section in _sections(page_text):
                for part in _split_long(section):
                    text_hash = sha256_bytes(part.encode("utf-8"))
                    chunk_id = stable_id("chunk", document.document_id, str(page_number), text_hash)
                    source_id = stable_id("src", chunk_id)
                    ref = SourceRef(
                        source_ref_id=source_id,
                        document_id=document.document_id,
                        chunk_id=chunk_id,
                        page_start=page_number,
                        page_end=page_number,
                        anchor_start=0,
                        anchor_end=len(part),
                    )
                    chunks.append(
                        DocumentChunk(
                            chunk_id=chunk_id,
                            document_id=document.document_id,
                            page_start=page_number,
                            page_end=page_number,
                            heading_path=heading_path,
                            normalized_text=part,
                            text_hash=text_hash,
                            source_ref=ref,
                        )
                    )
    if not chunks:
        raise ValueError("Extraction produced no useful chunks")
    source_hash = sha256_json([chunk.model_dump(mode="json") for chunk in chunks])
    return ExtractedCorpus(documents=documents, chunks=chunks, source_hash=source_hash)

