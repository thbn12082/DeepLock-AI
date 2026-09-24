from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..models import AtomPlan, Course, CourseOutline, DocumentChunk, ExtractedCorpus, ModuleOutline


_CONTENT_IDENTITY_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")


def semantic_content_identity_namespace(curriculum: dict[str, Any]) -> str:
    """Return the stable identity namespace for semantically rewritten content.

    Semantic course-v2 lessons deliberately use identities disjoint from the
    legacy page-outline catalog. Android progress, attempts and quiz state are
    keyed by the derived atom/pair/question IDs, so changing this namespace
    lets a rewritten lecture start cleanly without deleting any user data.

    Workspaces planned before this field was introduced are course-v2
    workspaces and therefore default to ``v2``. Explicit legacy generation
    bypasses this helper and retains its historical unnamespaced IDs.
    """

    raw_namespace = curriculum.get("content_identity_namespace")
    if raw_namespace is None:
        namespace = "v2"
    elif isinstance(raw_namespace, str):
        namespace = raw_namespace
    else:
        namespace = ""
    if not _CONTENT_IDENTITY_NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(
            "content_identity_namespace must be 1..32 ASCII letters, digits, '_' or '-', "
            "starting with a letter or digit"
        )
    return namespace


@dataclass(frozen=True)
class SemanticCurriculumPart:
    """One source-complete unit whose learner structure is planned by the model."""

    order_index: int
    lecture_index: int
    lecture_id: str
    lecture_title: str
    filename: str
    document_id: str
    module_id: str
    page_start: int
    page_end: int
    chunks: tuple[DocumentChunk, ...]
    atom_count: int

    @property
    def required_atom_ids(self) -> list[str]:
        return [
            f"{self.module_id}_atom_{index + 1}"
            for index in range(self.atom_count)
        ]


def curriculum_part_ranges(
    *,
    page_count: int,
    pages_per_part: int,
    explicit_parts: object = None,
) -> list[tuple[int, int]]:
    """Return validated, contiguous page ranges covering a document exactly once."""

    if page_count < 1:
        raise ValueError("page_count must be positive")
    if explicit_parts is None:
        if not 1 <= pages_per_part <= 48:
            raise ValueError("pages_per_part must be between 1 and 48")
        return [
            (start, min(page_count, start + pages_per_part - 1))
            for start in range(1, page_count + 1, pages_per_part)
        ]
    if not isinstance(explicit_parts, list) or not explicit_parts:
        raise ValueError("parts must be a non-empty array")
    ranges: list[tuple[int, int]] = []
    expected_start = 1
    for index, raw in enumerate(explicit_parts):
        if not isinstance(raw, dict):
            raise ValueError(f"parts[{index}] must be an object")
        start = int(raw.get("page_start") or 0)
        end = int(raw.get("page_end") or 0)
        if start != expected_start or end < start or end > page_count:
            raise ValueError(
                f"parts[{index}] must continue at page {expected_start} and stay within 1..{page_count}"
            )
        ranges.append((start, end))
        expected_start = end + 1
    if expected_start != page_count + 1:
        raise ValueError(f"parts do not cover final pages {expected_start}..{page_count}")
    return ranges


def _page_groups(chunks: Iterable[DocumentChunk]) -> list[list[DocumentChunk]]:
    grouped: dict[tuple[str, int, int], list[DocumentChunk]] = {}
    order: list[tuple[str, int, int]] = []
    for chunk in chunks:
        key = (chunk.document_id, chunk.page_start, chunk.page_end)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(chunk)
    return [grouped[key] for key in order]


def _atom_count(
    page_groups: list[list[DocumentChunk]],
    target_pages_per_atom: int,
    max_atoms_per_part: int = 5,
) -> int:
    if not page_groups:
        raise ValueError("A curriculum part contains no source chunks")
    if not 1 <= target_pages_per_atom <= 24:
        raise ValueError("target_pages_per_atom must be between 1 and 24")
    if not 1 <= max_atoms_per_part <= 10:
        raise ValueError("max_atoms_per_part must be between 1 and 10")
    return min(
        max_atoms_per_part,
        len(page_groups),
        max(1, math.ceil(len(page_groups) / target_pages_per_atom)),
    )


def _validate_part_granularity_capacity(
    *,
    page_group_count: int,
    atom_count: int,
    target_pages_per_atom: int,
    max_atoms_per_part: int,
    lecture_id: str,
    page_start: int,
    page_end: int,
) -> None:
    """Fail before model calls when the fixed atom contract cannot fit the part."""

    max_page_groups_per_atom = target_pages_per_atom + 1
    capacity = atom_count * max_page_groups_per_atom
    if page_group_count > capacity:
        raise ValueError(
            "Semantic curriculum granularity capacity is insufficient for "
            f"{lecture_id} pages {page_start}-{page_end}: {page_group_count} physical "
            f"page-groups require more than {atom_count} atoms x "
            f"{max_page_groups_per_atom} groups/atom (max_atoms_per_part="
            f"{max_atoms_per_part}). Increase max_atoms_per_part or reduce pages_per_part."
        )


def _balanced_groups(
    page_groups: list[list[DocumentChunk]],
    count: int,
) -> list[list[DocumentChunk]]:
    """Split contiguous physical pages without separating text and visual supplements."""

    weights = [max(1, sum(len(chunk.normalized_text) for chunk in group)) for group in page_groups]
    remaining_weight = sum(weights)
    output: list[list[DocumentChunk]] = []
    cursor = 0
    for atom_index in range(count):
        atoms_left = count - atom_index
        groups_left = len(page_groups) - cursor
        take = 1
        if atoms_left > 1:
            target = remaining_weight / atoms_left
            accumulated = weights[cursor]
            max_take = groups_left - (atoms_left - 1)
            while take < max_take:
                next_weight = weights[cursor + take]
                if abs(accumulated - target) <= abs(accumulated + next_weight - target):
                    break
                accumulated += next_weight
                take += 1
        selected = page_groups[cursor : cursor + take]
        output.append([chunk for group in selected for chunk in group])
        consumed = sum(weights[cursor : cursor + take])
        remaining_weight -= consumed
        cursor += take
    if cursor != len(page_groups):
        output[-1].extend(chunk for group in page_groups[cursor:] for chunk in group)
    return output


def semantic_curriculum_parts(
    *,
    corpus: ExtractedCorpus,
    curriculum: dict[str, Any],
) -> list[SemanticCurriculumPart]:
    """Derive stable semantic-planning units and dynamic atom identities.

    This function deliberately decides only source ownership, stable IDs and
    atom counts. Titles, objectives and concept boundaries remain model work.
    Both generation and the independent pack contract call this function so
    their counts and identities cannot drift.
    """

    raw_modules = curriculum.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise ValueError("Curriculum must contain at least one module")
    pages_per_part = int(curriculum.get("pages_per_part", 20))
    target_pages_per_atom = int(curriculum.get("target_pages_per_atom", 4))
    max_atoms_per_part = int(curriculum.get("max_atoms_per_part", 5))
    identity_namespace = semantic_content_identity_namespace(curriculum)
    if not 1 <= pages_per_part <= 48:
        raise ValueError("pages_per_part must be between 1 and 48")
    if not 1 <= target_pages_per_atom <= 24:
        raise ValueError("target_pages_per_atom must be between 1 and 24")
    if not 1 <= max_atoms_per_part <= 10:
        raise ValueError("max_atoms_per_part must be between 1 and 10")

    documents = {document.filename: document for document in corpus.documents}
    corpus_order = {
        chunk.source_ref.source_ref_id: index
        for index, chunk in enumerate(corpus.chunks)
    }
    if len(corpus_order) != len(corpus.chunks):
        raise ValueError("Curriculum source_ref IDs must be unique")
    chunks_by_document: dict[str, list[DocumentChunk]] = {}
    for chunk in corpus.chunks:
        chunks_by_document.setdefault(chunk.document_id, []).append(chunk)

    parts: list[SemanticCurriculumPart] = []
    seen_lecture_ids: set[str] = set()
    seen_filenames: set[str] = set()
    selected_document_ids: list[str] = []
    for lecture_index, raw_spec in enumerate(raw_modules):
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Curriculum lecture {lecture_index} must be an object")
        spec = dict(raw_spec)
        filename = str(spec.get("output") or "").strip()
        lecture_id = str(spec.get("module_id") or "").strip()
        lecture_title = str(spec.get("title") or "").strip()
        document = documents.get(filename)
        if document is None or not lecture_id or not lecture_title:
            raise ValueError(
                f"Curriculum lecture {lecture_index} has an unknown output or blank id/title"
            )
        if lecture_id in seen_lecture_ids or filename in seen_filenames:
            raise ValueError(
                f"Curriculum lecture id/output must be unique: {lecture_id}/{filename}"
            )
        seen_lecture_ids.add(lecture_id)
        seen_filenames.add(filename)
        selected_document_ids.append(document.document_id)

        ranges = curriculum_part_ranges(
            page_count=document.page_count,
            pages_per_part=pages_per_part,
            explicit_parts=spec.get("parts"),
        )
        ordered_chunks = sorted(
            chunks_by_document.get(document.document_id, []),
            key=lambda item: (
                item.page_start,
                item.page_end,
                corpus_order[item.source_ref.source_ref_id],
            ),
        )
        populated: list[tuple[int, int, tuple[DocumentChunk, ...], int]] = []
        for page_start, page_end in ranges:
            part_chunks = tuple(
                chunk
                for chunk in ordered_chunks
                if page_start <= chunk.page_start <= page_end
            )
            page_groups = _page_groups(part_chunks)
            # Covers/dividers may have been removed by the visual/editorial
            # audit. A now-empty physical range must not create a fake lesson.
            if not page_groups:
                continue
            atom_count = _atom_count(
                page_groups,
                target_pages_per_atom,
                max_atoms_per_part,
            )
            _validate_part_granularity_capacity(
                page_group_count=len(page_groups),
                atom_count=atom_count,
                target_pages_per_atom=target_pages_per_atom,
                max_atoms_per_part=max_atoms_per_part,
                lecture_id=lecture_id,
                page_start=page_start,
                page_end=page_end,
            )
            populated.append((
                page_start,
                page_end,
                part_chunks,
                atom_count,
            ))
        if not populated:
            raise ValueError(f"Curriculum lecture {lecture_id} contains no learning source chunks")

        for part_index, (page_start, page_end, part_chunks, atom_count) in enumerate(
            populated,
            start=1,
        ):
            parts.append(SemanticCurriculumPart(
                order_index=len(parts),
                lecture_index=lecture_index,
                lecture_id=lecture_id,
                lecture_title=lecture_title,
                filename=filename,
                document_id=document.document_id,
                module_id=f"{lecture_id}_{identity_namespace}_part_{part_index:03d}",
                page_start=page_start,
                page_end=page_end,
                chunks=part_chunks,
                atom_count=atom_count,
            ))

    selected = set(selected_document_ids)
    corpus_documents = {document.document_id for document in corpus.documents}
    if selected != corpus_documents or len(selected_document_ids) != len(corpus.documents):
        raise ValueError(
            "Curriculum document set must exactly match inspected source documents; "
            f"missing={sorted(corpus_documents - selected)}, "
            f"extra={sorted(selected - corpus_documents)}"
        )
    expected_source_ids = [
        chunk.source_ref.source_ref_id
        for document in corpus.documents
        for chunk in sorted(
            chunks_by_document.get(document.document_id, []),
            key=lambda item: (
                item.page_start,
                item.page_end,
                corpus_order[item.source_ref.source_ref_id],
            ),
        )
    ]
    planned_source_ids = [
        chunk.source_ref.source_ref_id
        for part in parts
        for chunk in part.chunks
    ]
    if planned_source_ids != expected_source_ids:
        raise ValueError(
            "Semantic curriculum parts do not preserve exact ordered source_ref coverage"
        )
    return parts


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .-_\t\r\n")
    return value[:180] or "Nội dung bài giảng"


def _atom_title(base_title: str, chunks: list[DocumentChunk]) -> str:
    first = chunks[0]
    last = chunks[-1]
    page_label = (
        f"trang {first.page_start}"
        if first.page_start == last.page_end
        else f"trang {first.page_start}–{last.page_end}"
    )
    heading = next(
        (
            _clean_title(item)
            for chunk in chunks
            for item in reversed(chunk.heading_path)
            if _clean_title(item)
        ),
        "",
    )
    if not heading:
        first_line = next(
            (line.strip() for line in first.normalized_text.splitlines() if len(line.strip()) >= 4),
            "",
        )
        heading = _clean_title(first_line) if first_line else base_title
    return _clean_title(f"{heading} · {page_label}")


def build_deterministic_curriculum_outline(
    *,
    corpus: ExtractedCorpus,
    curriculum: dict[str, Any],
) -> CourseOutline:
    """Create a complete source assignment without spending an LLM call on bookkeeping."""

    raw_modules = curriculum.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise ValueError("Curriculum must contain at least one module")
    pages_per_part = int(curriculum.get("pages_per_part", 24))
    target_pages_per_atom = int(curriculum.get("target_pages_per_atom", 6))
    max_atoms_per_part = int(curriculum.get("max_atoms_per_part", 5))
    if not 1 <= max_atoms_per_part <= 10:
        raise ValueError("max_atoms_per_part must be between 1 and 10")
    documents = {document.filename: document for document in corpus.documents}
    chunks_by_document: dict[str, list[DocumentChunk]] = {}
    for chunk in corpus.chunks:
        chunks_by_document.setdefault(chunk.document_id, []).append(chunk)
    modules: list[ModuleOutline] = []
    previous_by_lecture: dict[str, str] = {}
    for lecture_index, raw_spec in enumerate(raw_modules):
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Curriculum lecture {lecture_index} must be an object")
        spec = dict(raw_spec)
        filename = str(spec.get("output") or "")
        document = documents.get(filename)
        lecture_id = str(spec.get("module_id") or "").strip()
        lecture_title = _clean_title(str(spec.get("title") or ""))
        if document is None or not lecture_id:
            raise ValueError(f"Curriculum lecture {lecture_index} has an unknown output or blank ID")
        ranges = curriculum_part_ranges(
            page_count=document.page_count,
            pages_per_part=pages_per_part,
            explicit_parts=spec.get("parts"),
        )
        ordered_chunks = sorted(
            chunks_by_document.get(document.document_id, []),
            key=lambda item: (item.page_start, item.page_end, item.chunk_id),
        )
        populated_ranges: list[tuple[int, int, list[list[DocumentChunk]]]] = []
        for page_start, page_end in ranges:
            part_chunks = [
                chunk for chunk in ordered_chunks if page_start <= chunk.page_start <= page_end
            ]
            groups = _page_groups(part_chunks)
            # A visual audit may legitimately remove a trailing/interstitial
            # cover, divider or thank-you-only range. Such a range owns no
            # learning source and therefore must not become an empty module.
            if groups:
                populated_ranges.append((page_start, page_end, groups))
        if not populated_ranges:
            raise ValueError(f"Curriculum lecture {lecture_id} contains no learning source chunks")

        for part_index, (page_start, page_end, groups) in enumerate(
            populated_ranges,
            start=1,
        ):
            count = _atom_count(groups, target_pages_per_atom, max_atoms_per_part)
            assignments = _balanced_groups(groups, count)
            module_id = f"{lecture_id}_part_{part_index:03d}"
            plans: list[AtomPlan] = []
            previous_atom_id = previous_by_lecture.get(lecture_id)
            for atom_index, selected in enumerate(assignments, start=1):
                atom_id = f"{module_id}_atom_{atom_index}"
                title = _atom_title(lecture_title, selected)
                plans.append(
                    AtomPlan(
                        atom_id=atom_id,
                        order_index=atom_index - 1,
                        title=title,
                        objective=f"Học đầy đủ nội dung {title} từ bài giảng gốc.",
                        prerequisite_ids=[previous_atom_id] if previous_atom_id else [],
                        source_ref_ids=[chunk.source_ref.source_ref_id for chunk in selected],
                    )
                )
                previous_atom_id = atom_id
            previous_by_lecture[lecture_id] = previous_atom_id
            module_title = (
                f"{lecture_title} · Phần {part_index}/{len(populated_ranges)} "
                f"(trang {page_start}–{page_end})"
            )
            modules.append(
                ModuleOutline(
                    module_id=module_id,
                    order_index=len(modules),
                    title=module_title,
                    objective=f"Nắm vững toàn bộ {module_title}.",
                    atom_plans=plans,
                )
            )
    course = Course(
        course_id=str(curriculum.get("course_id") or "course_all_lectures_vi"),
        title=str(curriculum.get("title") or "Toàn bộ Lecture"),
        description=str(
            curriculum.get("description")
            or "Nội dung nguyên bản được nhập từ toàn bộ kho Lecture."
        ),
        language="vi",
    )
    return CourseOutline(course=course, modules=modules)
