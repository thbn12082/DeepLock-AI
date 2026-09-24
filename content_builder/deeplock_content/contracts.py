from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .models import ExtractedCorpus, PackData, StrictModel
from .util import (
    curriculum_illustration_id,
    read_json,
    resolve_content_id_seed,
    sha256_bytes,
    sha256_json,
    stable_id,
)


PACK_CONTRACT_FILENAME = "pack-contract.json"
Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class PackExpectedCounts(StrictModel):
    modules: int = Field(ge=1)
    full_lessons: int = Field(ge=1)
    atoms: int = Field(ge=1)
    lessons: int = Field(ge=1)
    pairs: int = Field(ge=1)
    questions: int = Field(ge=1)
    source_refs: int = Field(ge=1)
    source_excerpts: int = Field(ge=1)
    illustrations: int = Field(ge=0)
    mindmaps: int = Field(ge=1)


class PackContract(StrictModel):
    """Independent, deterministic curriculum expectations for one candidate."""

    schema_version: Literal["1.0"] = "1.0"
    contract_type: Literal["CURRICULUM"] = "CURRICULUM"
    curriculum_sha256: Sha256
    course_id: str = Field(min_length=1)
    course_sha256: Sha256
    source_hash: Sha256
    expected_counts: PackExpectedCounts
    questions_per_atom: int = Field(ge=1, le=20)
    ordered_module_ids: list[str] = Field(min_length=1)
    ordered_atom_ids: list[str] = Field(min_length=1)
    ordered_source_ref_ids_sha256: Sha256
    ordered_source_refs_sha256: Sha256
    ordered_source_excerpts_sha256: Sha256
    ordered_module_source_ref_ids_sha256: list[Sha256] = Field(min_length=1)
    identity_graph_sha256: Sha256
    illustration_identities_sha256: Sha256
    outline_assignment_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def internally_consistent(self) -> "PackContract":
        counts = self.expected_counts
        if counts.modules != len(self.ordered_module_ids):
            raise ValueError("expected module count does not match ordered_module_ids")
        if counts.atoms != len(self.ordered_atom_ids):
            raise ValueError("expected atom count does not match ordered_atom_ids")
        if len(self.ordered_module_ids) != len(set(self.ordered_module_ids)):
            raise ValueError("ordered_module_ids contains duplicates")
        if len(self.ordered_atom_ids) != len(set(self.ordered_atom_ids)):
            raise ValueError("ordered_atom_ids contains duplicates")
        if len(self.ordered_module_source_ref_ids_sha256) != counts.modules:
            raise ValueError("module source hashes must align with ordered_module_ids")
        if counts.full_lessons != counts.modules:
            raise ValueError("curriculum requires exactly one FullLesson per module")
        if counts.lessons != counts.atoms or counts.pairs != counts.atoms:
            raise ValueError("curriculum requires exactly one Lesson and LearningPair per atom")
        if counts.questions != counts.atoms * self.questions_per_atom:
            raise ValueError("expected question count does not match questions_per_atom")
        if counts.source_excerpts != counts.source_refs:
            raise ValueError("curriculum requires exactly one SourceExcerpt per SourceRef")
        return self


@dataclass(frozen=True)
class ContractCheck:
    code: str
    path: str
    message: str


def pack_contract_sha256(contract: PackContract) -> str:
    return sha256_json(contract.model_dump(mode="json"))


def outline_assignment_sha256(pack: PackData) -> str:
    return sha256_json([
        module.model_dump(mode="json")
        for module in pack.modules
    ])


def identity_graph_sha256(pack: PackData) -> str:
    return sha256_json({
        "full_lessons": [
            {
                "full_lesson_id": item.full_lesson_id,
                "module_id": item.module_id,
                "atom_ids": list(item.atom_ids),
            }
            for item in pack.full_lessons
        ],
        "lessons": [
            {
                "lesson_id": item.lesson_id,
                "pair_id": item.pair_id,
                "atom_id": item.atom_id,
            }
            for item in pack.lessons
        ],
        "pairs": [
            {
                "pair_id": item.pair_id,
                "atom_id": item.atom_id,
                "micro_lesson_id": item.micro_lesson_id,
                "question_ids": list(item.question_ids),
            }
            for item in pack.learning_pairs
        ],
        "questions": [
            {
                "question_id": item.question_id,
                "pair_id": item.pair_id,
                "atom_id": item.atom_id,
            }
            for item in pack.questions
        ],
    })


def _illustration_identity(
    *,
    illustration_id: str,
    source_ref_id: str,
    asset_member: str,
    mime_type: str,
    sha256: str,
    byte_size: int,
    width: int,
    height: int,
    page_number: int,
    caption: str,
    alt_text: str,
) -> dict[str, Any]:
    return {
        "illustration_id": illustration_id,
        "source_ref_id": source_ref_id,
        "asset_member": asset_member,
        "mime_type": mime_type,
        "sha256": sha256,
        "byte_size": byte_size,
        "width": width,
        "height": height,
        "page_number": page_number,
        "caption": caption,
        "alt_text": alt_text,
    }


def illustration_identities_sha256(pack: PackData) -> str:
    return sha256_json([
        _illustration_identity(
            illustration_id=item.illustration_id,
            source_ref_id=item.source_ref_id,
            asset_member=item.asset_member,
            mime_type=item.mime_type,
            sha256=item.sha256,
            byte_size=item.byte_size,
            width=item.width,
            height=item.height,
            page_number=item.page_number,
            caption=item.caption,
            alt_text=item.alt_text,
        )
        for item in pack.illustrations
    ])


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError("Contract illustration is not a valid PNG")
    return int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")


def build_pack_contract(
    *,
    source: ExtractedCorpus,
    curriculum: dict[str, Any],
    media_root: Path,
) -> PackContract:
    """Build expectations solely from inspected source plus curriculum.

    This deliberately does not inspect generation cache, candidate content, AI
    responses or media binaries.
    """

    # Keep generation's audited raster-page semantics as the single source of
    # truth without changing generate_candidate's API or execution path.
    from .generation.builder import _with_curriculum_page_overrides

    corpus = _with_curriculum_page_overrides(source, curriculum)
    content_id_seed = resolve_content_id_seed(corpus.source_hash, curriculum)
    deterministic_outline = None
    semantic_parts = None
    # Mirror generation: stale catalog booleans must not resurrect the
    # mechanical page outline. Only an explicit legacy mode opts into it.
    if curriculum.get("outline_mode") == "DETERMINISTIC_LEGACY":
        from .curriculum import build_deterministic_curriculum_outline

        deterministic_outline = build_deterministic_curriculum_outline(
            corpus=corpus,
            curriculum=curriculum,
        )
    else:
        from .curriculum import semantic_curriculum_parts

        semantic_parts = semantic_curriculum_parts(
            corpus=corpus,
            curriculum=curriculum,
        )
    raw_modules = curriculum.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise ValueError("Curriculum must contain at least one module")
    documents = {document.filename: document for document in corpus.documents}
    chunks_by_document: dict[str, list[Any]] = {}
    for chunk in corpus.chunks:
        chunks_by_document.setdefault(chunk.document_id, []).append(chunk)

    module_ids: list[str] = []
    module_atom_ids: list[list[str]] = []
    module_source_ref_ids_sha256: list[str] = []
    atom_ids: list[str] = []
    selected_document_ids: list[str] = []
    illustration_identities: list[dict[str, Any]] = []
    seen_lecture_ids: set[str] = set()
    seen_outputs: set[str] = set()
    seen_asset_members: set[str] = set()
    seen_illustration_ids: set[str] = set()

    if deterministic_outline is not None:
        for module in deterministic_outline.modules:
            part_atom_ids = [plan.atom_id for plan in module.atom_plans]
            module_ids.append(module.module_id)
            module_atom_ids.append(part_atom_ids)
            atom_ids.extend(part_atom_ids)
            module_source_ref_ids_sha256.append(sha256_json([
                source_ref_id
                for plan in module.atom_plans
                for source_ref_id in plan.source_ref_ids
            ]))
    else:
        assert semantic_parts is not None
        for part in semantic_parts:
            part_atom_ids = list(part.required_atom_ids)
            module_ids.append(part.module_id)
            module_atom_ids.append(part_atom_ids)
            atom_ids.extend(part_atom_ids)
            module_source_ref_ids_sha256.append(sha256_json([
                chunk.source_ref.source_ref_id
                for chunk in part.chunks
            ]))

    for lecture_index, raw_spec in enumerate(raw_modules):
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Curriculum module {lecture_index} must be an object")
        spec = dict(raw_spec)
        lecture_id = str(spec.get("module_id") or "").strip()
        filename = str(spec.get("output") or "").strip()
        title = str(spec.get("title") or "").strip()
        document = documents.get(filename)
        if not lecture_id or not filename or not title or document is None:
            raise ValueError(f"Curriculum module {lecture_index} has invalid id/title/output")
        if lecture_id in seen_lecture_ids or filename in seen_outputs:
            raise ValueError(f"Curriculum module id/output must be unique: {lecture_id}/{filename}")
        seen_lecture_ids.add(lecture_id)
        seen_outputs.add(filename)
        selected_document_ids.append(document.document_id)

        if spec.get("illustrations") is not None:
            visual_specs = list(spec.get("illustrations") or [])
        else:
            visual_specs = list(spec.get("additional_illustrations") or [])
            if spec.get("illustration_page") is not None:
                visual_specs.insert(0, {
                    "page": spec.get("illustration_page"),
                    "caption": spec.get("illustration_caption"),
                    "asset_member": f"illustration_{lecture_id}.png",
                })
        for raw_visual in visual_specs:
            if not isinstance(raw_visual, dict):
                raise ValueError(f"Illustration spec must be an object: {lecture_id}")
            visual = dict(raw_visual)
            page_number = int(visual.get("page") or 0)
            caption = str(visual.get("caption") or "").strip()
            asset_member = str(visual.get("asset_member") or "").strip()
            if not 1 <= page_number <= document.page_count or not caption or not asset_member:
                raise ValueError(f"Invalid illustration contract: {lecture_id} page {page_number}")
            page_chunks = [
                chunk
                for chunk in chunks_by_document.get(document.document_id, [])
                if chunk.page_start <= page_number <= chunk.page_end
            ]
            if not page_chunks:
                raise ValueError(f"Illustration page has no source ref: {filename} page {page_number}")
            source_ref_id = max(page_chunks, key=lambda chunk: len(chunk.normalized_text)).source_ref.source_ref_id
            occurrence_seed = str(visual.get("occurrence_id") or "").strip() or None
            illustration_id = curriculum_illustration_id(
                content_id_seed=content_id_seed,
                lecture_id=lecture_id,
                page_number=page_number,
                occurrence_seed=occurrence_seed,
            )
            if asset_member in seen_asset_members or illustration_id in seen_illustration_ids:
                raise ValueError(f"Duplicate illustration identity/member: {lecture_id} page {page_number}")
            seen_asset_members.add(asset_member)
            seen_illustration_ids.add(illustration_id)
            if Path(asset_member).name != asset_member:
                raise ValueError(f"Illustration asset_member must be flat: {asset_member}")
            media_path = media_root / asset_member
            if media_path.is_symlink() or not media_path.is_file():
                raise ValueError(f"Missing audited illustration media: {media_path}")
            payload = media_path.read_bytes()
            width, height = _png_dimensions(payload)
            alt_text = str(
                visual.get("alt_text")
                or f"{caption} Nguồn: {filename}, trang {page_number}."
            ).strip()
            if not 1 <= len(alt_text) <= 500:
                raise ValueError(
                    f"Illustration alt_text must contain at most 500 characters: {asset_member}"
                )
            illustration_identities.append(_illustration_identity(
                illustration_id=illustration_id,
                source_ref_id=source_ref_id,
                asset_member=asset_member,
                mime_type="image/png",
                sha256=sha256_bytes(payload),
                byte_size=len(payload),
                width=width,
                height=height,
                page_number=page_number,
                caption=caption,
                alt_text=alt_text,
            ))

    selected_document_id_set = set(selected_document_ids)
    corpus_document_ids = {document.document_id for document in corpus.documents}
    if selected_document_id_set != corpus_document_ids or len(selected_document_ids) != len(corpus.documents):
        raise ValueError(
            "Curriculum document set must exactly match inspected source documents; "
            f"missing={sorted(corpus_document_ids - selected_document_id_set)}, "
            f"extra={sorted(selected_document_id_set - corpus_document_ids)}"
        )
    selected_chunks = [
        chunk for chunk in corpus.chunks if chunk.document_id in selected_document_id_set
    ]
    ordered_source_ref_ids = [chunk.source_ref.source_ref_id for chunk in selected_chunks]
    if len(ordered_source_ref_ids) != len(set(ordered_source_ref_ids)):
        raise ValueError("Selected curriculum source refs are not unique")

    ordered_source_refs = [
        chunk.source_ref.model_dump(mode="json")
        for chunk in selected_chunks
    ]
    document_names = {document.document_id: document.filename for document in corpus.documents}
    ordered_source_excerpts = [
        {
            "source_excerpt_id": stable_id("excerpt", chunk.source_ref.source_ref_id),
            "source_ref_id": chunk.source_ref.source_ref_id,
            "document_id": chunk.source_ref.document_id,
            "chunk_id": chunk.source_ref.chunk_id,
            "document_label": document_names[chunk.source_ref.document_id],
            "heading_path": list(chunk.heading_path),
            "page_start": chunk.source_ref.page_start,
            "page_end": chunk.source_ref.page_end,
            "context_text": chunk.normalized_text,
            "highlight_start": 0,
            "highlight_end": len(chunk.normalized_text),
            "excerpt_hash": sha256_bytes(chunk.normalized_text.encode("utf-8")),
        }
        for chunk in selected_chunks
    ]
    expected_identity_graph = {
        "full_lessons": [
            {
                "full_lesson_id": stable_id("full", content_id_seed, module_id),
                "module_id": module_id,
                "atom_ids": list(part_atom_ids),
            }
            for module_id, part_atom_ids in zip(module_ids, module_atom_ids, strict=True)
        ],
        "lessons": [],
        "pairs": [],
        "questions": [],
    }
    for atom_id in atom_ids:
        pair_id = stable_id("pair", content_id_seed, atom_id)
        lesson_id = stable_id("lesson", pair_id)
        question_ids = [stable_id("q", pair_id, str(index)) for index in range(1, 6)]
        expected_identity_graph["lessons"].append({
            "lesson_id": lesson_id,
            "pair_id": pair_id,
            "atom_id": atom_id,
        })
        expected_identity_graph["pairs"].append({
            "pair_id": pair_id,
            "atom_id": atom_id,
            "micro_lesson_id": lesson_id,
            "question_ids": question_ids,
        })
        expected_identity_graph["questions"].extend(
            {
                "question_id": question_id,
                "pair_id": pair_id,
                "atom_id": atom_id,
            }
            for question_id in question_ids
        )

    counts = PackExpectedCounts(
        modules=len(module_ids),
        full_lessons=len(module_ids),
        atoms=len(atom_ids),
        lessons=len(atom_ids),
        pairs=len(atom_ids),
        questions=len(atom_ids) * 5,
        source_refs=len(ordered_source_ref_ids),
        source_excerpts=len(ordered_source_ref_ids),
        illustrations=len(illustration_identities),
        mindmaps=1,
    )
    expected_course = {
        "course_id": str(
            curriculum.get("course_id")
            or ("course_all_lectures_vi" if deterministic_outline else "course_deep_learning_core_vi")
        ),
        "title": str(
            curriculum.get("title")
            or ("Toàn bộ Lecture" if deterministic_outline else "Deep Learning từ bài giảng thực tế")
        ),
        "description": str(
            curriculum.get("description")
            or (
                "Nội dung nguyên bản được nhập từ toàn bộ kho Lecture."
                if deterministic_outline
                else "Khóa học được tạo từ bộ Lecture."
            )
        ),
        "language": "vi",
    }
    return PackContract(
        curriculum_sha256=sha256_json(curriculum),
        course_id=expected_course["course_id"],
        course_sha256=sha256_json(expected_course),
        source_hash=corpus.source_hash,
        expected_counts=counts,
        questions_per_atom=5,
        ordered_module_ids=module_ids,
        ordered_atom_ids=atom_ids,
        ordered_source_ref_ids_sha256=sha256_json(ordered_source_ref_ids),
        ordered_source_refs_sha256=sha256_json(ordered_source_refs),
        ordered_source_excerpts_sha256=sha256_json(ordered_source_excerpts),
        ordered_module_source_ref_ids_sha256=module_source_ref_ids_sha256,
        identity_graph_sha256=sha256_json(expected_identity_graph),
        illustration_identities_sha256=sha256_json(illustration_identities),
        outline_assignment_sha256=None,
    )


def check_pack_contract(
    pack: PackData,
    contract: PackContract,
    *,
    require_frozen_outline: bool = False,
) -> list[ContractCheck]:
    checks: list[ContractCheck] = []

    def add(code: str, path: str, message: str) -> None:
        checks.append(ContractCheck(code, path, message))

    counts = contract.expected_counts
    actual_counts = {
        "modules": len(pack.modules),
        "full_lessons": len(pack.full_lessons),
        "atoms": len(pack.atoms),
        "lessons": len(pack.lessons),
        "pairs": len(pack.learning_pairs),
        "questions": len(pack.questions),
        "source_refs": len(pack.source_refs),
        "source_excerpts": len(pack.source_excerpts),
        "illustrations": len(pack.illustrations),
        "mindmaps": len(pack.mindmaps),
    }
    for field, actual in actual_counts.items():
        expected = getattr(counts, field)
        if actual != expected:
            add(
                "PACK_CONTRACT_COUNT_MISMATCH",
                f"contract.expected_counts.{field}",
                f"Expected {expected}, found {actual}",
            )

    if pack.course.course_id != contract.course_id:
        add("PACK_CONTRACT_COURSE_MISMATCH", "course.course_id", contract.course_id)
    if sha256_json(pack.course.model_dump(mode="json")) != contract.course_sha256:
        add(
            "PACK_CONTRACT_COURSE_METADATA_MISMATCH",
            "course",
            "Course metadata differs from the curriculum contract",
        )
    if pack.source_hash != contract.source_hash:
        add("PACK_CONTRACT_SOURCE_HASH_MISMATCH", "source_hash", contract.source_hash)

    module_ids = [module.module_id for module in pack.modules]
    if module_ids != contract.ordered_module_ids:
        add(
            "PACK_CONTRACT_MODULE_IDS_MISMATCH",
            "modules",
            "Ordered module IDs differ from the frozen curriculum contract",
        )
    atom_ids = [atom.atom_id for atom in pack.atoms]
    if atom_ids != contract.ordered_atom_ids:
        add(
            "PACK_CONTRACT_ATOM_IDS_MISMATCH",
            "atoms",
            "Ordered atom IDs differ from the frozen curriculum contract",
        )
    planned_atom_ids = [plan.atom_id for module in pack.modules for plan in module.atom_plans]
    if planned_atom_ids != contract.ordered_atom_ids:
        add(
            "PACK_CONTRACT_PLAN_IDS_MISMATCH",
            "modules.atom_plans",
            "Ordered AtomPlan IDs differ from the frozen curriculum contract",
        )

    source_ref_ids = [source_ref.source_ref_id for source_ref in pack.source_refs]
    if sha256_json(source_ref_ids) != contract.ordered_source_ref_ids_sha256:
        add(
            "PACK_CONTRACT_SOURCE_REFS_MISMATCH",
            "source_refs",
            "Ordered global SourceRef identity hash differs from the curriculum contract",
        )
    if sha256_json([
        source_ref.model_dump(mode="json") for source_ref in pack.source_refs
    ]) != contract.ordered_source_refs_sha256:
        add(
            "PACK_CONTRACT_SOURCE_RECORDS_MISMATCH",
            "source_refs",
            "Ordered full SourceRef records differ from the curriculum contract",
        )
    if sha256_json([
        excerpt.model_dump(mode="json") for excerpt in pack.source_excerpts
    ]) != contract.ordered_source_excerpts_sha256:
        add(
            "PACK_CONTRACT_EXCERPTS_MISMATCH",
            "source_excerpts",
            "Ordered full SourceExcerpt records differ from the curriculum contract",
        )
    assigned_source_ref_ids = [
        source_ref_id
        for module in pack.modules
        for plan in module.atom_plans
        for source_ref_id in plan.source_ref_ids
    ]
    if Counter(assigned_source_ref_ids) != Counter({source_ref_id: 1 for source_ref_id in source_ref_ids}):
        add(
            "PACK_CONTRACT_SOURCE_ASSIGNMENT_MISMATCH",
            "modules.atom_plans.source_ref_ids",
            "Every contracted SourceRef must be assigned to exactly one AtomPlan",
        )
    for module_index, module in enumerate(pack.modules):
        if module_index >= len(contract.ordered_module_source_ref_ids_sha256):
            break
        module_source_ref_ids = [
            source_ref_id
            for plan in module.atom_plans
            for source_ref_id in plan.source_ref_ids
        ]
        if sha256_json(module_source_ref_ids) != contract.ordered_module_source_ref_ids_sha256[module_index]:
            add(
                "PACK_CONTRACT_MODULE_SOURCE_ASSIGNMENT_MISMATCH",
                f"module:{module.module_id}",
                "Module AtomPlans do not concatenate to the exact page-bounded source sequence",
            )

    if identity_graph_sha256(pack) != contract.identity_graph_sha256:
        add(
            "PACK_CONTRACT_IDENTITY_GRAPH_MISMATCH",
            "pack.identity_graph",
            "FullLesson/Lesson/Pair/Question identities differ from the curriculum contract",
        )

    if illustration_identities_sha256(pack) != contract.illustration_identities_sha256:
        add(
            "PACK_CONTRACT_ILLUSTRATIONS_MISMATCH",
            "illustrations",
            "Ordered illustration identity hash differs from the curriculum contract",
        )

    invalid_pair_sizes = [
        pair.pair_id
        for pair in pack.learning_pairs
        if len(pair.question_ids) != contract.questions_per_atom
    ]
    if invalid_pair_sizes:
        add(
            "PACK_CONTRACT_QUESTIONS_PER_ATOM_MISMATCH",
            "learning_pairs",
            f"Expected {contract.questions_per_atom} questions per atom: {invalid_pair_sizes[:5]}",
        )

    if contract.outline_assignment_sha256 is None:
        if require_frozen_outline:
            add(
                "PACK_CONTRACT_OUTLINE_UNFROZEN",
                "contract.outline_assignment_sha256",
                "Production validation requires a candidate-frozen outline assignment hash",
            )
    elif outline_assignment_sha256(pack) != contract.outline_assignment_sha256:
        add(
            "PACK_CONTRACT_OUTLINE_MISMATCH",
            "modules.atom_plans.source_ref_ids",
            "Outline source assignments differ from the candidate-frozen contract",
        )
    return checks


def freeze_outline_assignment(contract: PackContract, pack: PackData) -> PackContract:
    base_checks = check_pack_contract(pack, contract, require_frozen_outline=False)
    if base_checks:
        details = "; ".join(f"{item.code}:{item.path}" for item in base_checks)
        raise ValueError(f"Candidate cannot freeze an invalid PackContract: {details}")
    return contract.model_copy(update={
        "outline_assignment_sha256": outline_assignment_sha256(pack),
    })


def load_pack_contract(path: Path) -> PackContract:
    try:
        return PackContract.model_validate(read_json(path))
    except Exception as error:
        raise ValueError(f"Invalid PackContract sidecar {path}: {error}") from error


def resolve_pack_contract(
    *,
    explicit_path: Path | None = None,
    workdir: Path | None = None,
    candidate_path: Path | None = None,
    required: bool = False,
) -> PackContract | None:
    if explicit_path is not None:
        if not explicit_path.is_file():
            raise ValueError(f"PACK_CONTRACT_REQUIRED: missing explicit sidecar {explicit_path}")
        return load_pack_contract(explicit_path)

    candidates: list[Path] = []
    if candidate_path is not None:
        candidates.append(candidate_path.parent / PACK_CONTRACT_FILENAME)
    if workdir is not None:
        candidates.append(workdir / PACK_CONTRACT_FILENAME)
    seen: set[Path] = set()
    for path in candidates:
        normalized = path.resolve()
        if normalized in seen:
            continue
        seen.add(normalized)
        if path.is_file():
            return load_pack_contract(path)
    if required:
        location = candidates[0] if candidates else Path(PACK_CONTRACT_FILENAME)
        raise ValueError(f"PACK_CONTRACT_REQUIRED: missing {location}")
    return None
