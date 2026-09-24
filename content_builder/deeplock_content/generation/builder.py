from __future__ import annotations

import json
import re
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Literal

from ..cache import BuildCache
from ..curriculum import (
    build_deterministic_curriculum_outline,
    semantic_curriculum_parts,
)
from ..editorial import (
    has_observable_learning_objective,
    is_course_v2_prompt_version,
    validate_lesson_editorial,
)
from ..models import (
    _require_supported_model,
    AtomPlan,
    AtomContent,
    Course,
    CourseOutline,
    DocumentChunk,
    ExtractedCorpus,
    FullLesson,
    IllustrationAsset,
    KnowledgeAtom,
    LearningPair,
    MindMap,
    MindMapEdge,
    MindMapNode,
    ModuleOutline,
    PackData,
    SourceExcerpt,
    SourceRef,
)
from ..settings import Settings
from ..quiz_order import rebalance_question_options, validate_quiz_editorial
from ..util import (
    PROMPT_ROOT,
    canonical_json,
    curriculum_illustration_id,
    load_schema,
    normalize_text,
    responses_strict_schema,
    resolve_content_id_seed,
    sha256_bytes,
    sha256_json,
    stable_id,
    write_json,
)
from .api import DEFAULT_OUTPUT_VERBOSITY, ModelResponse, ResponsesAdapter
from .adapters import build_adapter


class ContentGenerationStagePaused(RuntimeError):
    """Signal that a bounded content canary succeeded and stopped intentionally."""

    def __init__(self, *, requests_started: int, atoms_cached: int) -> None:
        self.requests_started = requests_started
        self.atoms_cached = atoms_cached
        super().__init__(
            "Content generation canary paused intentionally after "
            f"{requests_started} upstream request(s); {atoms_cached} atom(s) are cached. "
            "No retry wave, per-atom fallback, mind-map request, or AI review was started."
        )


class ContentGenerationStageFailed(RuntimeError):
    """Signal that a bounded content canary response failed its request or gate."""


class _ContentRequestBudget:
    """Thread-safe hard cap around actual cache-miss adapter calls."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._started = 0
        self._lock = Lock()

    @property
    def started(self) -> int:
        with self._lock:
            return self._started

    @property
    def exhausted(self) -> bool:
        return self.started >= self.limit

    def acquire(self) -> None:
        with self._lock:
            if self._started >= self.limit:
                raise RuntimeError(
                    f"Content generation request budget exhausted ({self.limit})"
                )
            self._started += 1


class _BudgetedContentAdapter:
    """Count only real content calls; READY cache hits never reach this wrapper."""

    def __init__(self, delegate: ResponsesAdapter, budget: _ContentRequestBudget) -> None:
        self._delegate = delegate
        self._budget = budget

    def structured(self, **kwargs: Any) -> ModelResponse:
        self._budget.acquire()
        return self._delegate.structured(**kwargs)


def _prompt(name: str) -> str:
    return (PROMPT_ROOT / name).read_text(encoding="utf-8")


def _source_context(corpus: ExtractedCorpus, source_ids: set[str] | None = None) -> str:
    blocks: list[str] = []
    for chunk in corpus.chunks:
        if source_ids and chunk.source_ref.source_ref_id not in source_ids:
            continue
        blocks.append(
            f'<source id="{chunk.source_ref.source_ref_id}" document_id="{chunk.document_id}" '
            f'chunk_id="{chunk.chunk_id}" page_start="{chunk.page_start}" page_end="{chunk.page_end}">\n'
            f"{chunk.normalized_text}\n</source>"
        )
    return "\n\n".join(blocks)


def _cache_key(*parts: Any) -> str:
    return sha256_json(parts)


_MISSING_SENTENCE_SPACE_RE = re.compile(r"(?<=[a-z\u00e0-\u1ef90-9])[.!?](?=[A-Z\u00c0-\u1ef8])")


def _normalize_takeaway_spacing(takeaways: list[str]) -> list[str]:
    """Repair only the sentence-boundary spacing enforced by the editorial gate."""

    return [
        _MISSING_SENTENCE_SPACE_RE.sub(lambda match: f"{match.group(0)} ", value.strip())
        for value in takeaways
    ]


def _validate_takeaway_editorial(takeaways: list[str]) -> None:
    """Reject common model serialization/runaway artifacts in user-visible takeaways."""

    for index, raw_text in enumerate(takeaways):
        value = raw_text.strip()
        if len(value) > 500:
            raise ValueError(f"Takeaway {index + 1} exceeds 500 characters")
        if value.endswith((",", ";", ":")):
            raise ValueError(f"Takeaway {index + 1} ends with stray punctuation")
        if _MISSING_SENTENCE_SPACE_RE.search(value):
            raise ValueError(f"Takeaway {index + 1} is missing a space between sentences")
        if value.count('"') % 2 or value.count("\u201c") != value.count("\u201d"):
            raise ValueError(f"Takeaway {index + 1} contains unbalanced quotation marks")


def _mini_lab_matches_runtime(
    plan: Any,
    lab_type: str,
    allowed_atom_ids: set[str] | None = None,
) -> bool:
    """Return false when the finite Android lab engine cannot model the lesson operation."""

    if allowed_atom_ids is not None and plan.atom_id not in allowed_atom_ids:
        return False
    topic = f"{plan.title} {plan.objective}".casefold()
    if lab_type == "CONVOLUTION_2D" and "transposed" in topic:
        return False
    return True


def _cached_call(
    *,
    cache: BuildCache,
    adapter: ResponsesAdapter,
    settings: Settings,
    step_type: str,
    input_hash: str,
    prompt_version: str,
    logical_model: str,
    instructions: str,
    input_text: str,
    schema_name: str,
    schema: dict[str, Any],
    resume: bool,
    output_validator: Callable[[dict[str, Any]], None] | None = None,
    validation_feedback_max_chars: int = 600,
    include_invalid_output_in_correction: bool = False,
    correction_output_verbosity: Literal["low", "medium", "high"] | None = None,
    correction_context: dict[str, Any] | None = None,
) -> ModelResponse:
    if not 1 <= validation_feedback_max_chars <= 8_000:
        raise ValueError("validation_feedback_max_chars must be between 1 and 8000")
    output_verbosity = DEFAULT_OUTPUT_VERBOSITY
    key = _cache_key(
        input_hash,
        step_type,
        prompt_version,
        logical_model,
        settings.transport_model(logical_model),
        settings.generator_reasoning_effort,
        sha256_bytes(instructions.encode("utf-8")),
        sha256_json(schema),
        f"output-verbosity:{output_verbosity}",
        "schema-2",
    )
    cached_validation_error: Exception | None = None
    invalid_output_for_correction: dict[str, Any] | None = None
    if resume and (hit := cache.get("generation_steps", key)):
        cached_response = ModelResponse(
            data=hit["output"],
            response_id=hit.get("response_id") or "cache",
            model_snapshot=hit.get("model_snapshot") or logical_model,
        )
        try:
            if output_validator:
                output_validator(cached_response.data)
        except (TypeError, ValueError) as error:
            cached_validation_error = error
            invalid_output_for_correction = cached_response.data
        else:
            return cached_response

    # A previous validator may have rejected a structurally returned model
    # response because the deterministic validator itself was too strict or
    # buggy.  Re-run the *current* validator before promoting that payload;
    # transport failures are deliberately ineligible even if their message
    # object happens to satisfy a permissive validator.
    if resume and output_validator and (
        failed_hit := cache.get_failed("generation_steps", key)
    ) and failed_hit.get("error_code") == "OUTPUT_VALIDATION_FAILED":
        failed_response = ModelResponse(
            data=failed_hit["output"],
            response_id=failed_hit.get("response_id") or "cache",
            model_snapshot=failed_hit.get("model_snapshot") or logical_model,
        )
        try:
            output_validator(failed_response.data)
        except (TypeError, ValueError) as error:
            cached_validation_error = error
            invalid_output_for_correction = failed_response.data
        else:
            cache.record_attempt(
                "generation_steps",
                {
                    "cache_key": key,
                    "step_type": failed_hit["step_type"],
                    "input_hash": failed_hit["input_hash"],
                    "prompt_version": failed_hit["prompt_version"],
                    "logical_model": failed_hit["logical_model"],
                    "model_snapshot": failed_hit.get("model_snapshot"),
                    "schema_version": failed_hit["schema_version"],
                    "status": "READY",
                    "attempt_count": failed_hit["attempt_count"],
                    "error_code": None,
                    "response_id": failed_hit.get("response_id"),
                    "output_json": failed_response.data,
                },
            )
            return failed_response

    structured_validation_failure: ModelResponse | None = None
    semantic_attempt = 0
    try:
        validation_error: Exception | None = cached_validation_error
        for semantic_attempt in range(settings.max_api_attempts):
            corrected_input = input_text
            request_output_verbosity = (
                correction_output_verbosity
                if validation_error is not None and correction_output_verbosity is not None
                else output_verbosity
            )
            if validation_error is not None:
                correction_message = (
                    "CORRECTION REQUIRED: the previous structured response failed this deterministic "
                    "contract check: "
                    f"{str(validation_error)[:validation_feedback_max_chars]}."
                )
                if (
                    include_invalid_output_in_correction
                    and invalid_output_for_correction is not None
                ):
                    corrected_input = (
                        correction_message
                        + "\n\nThe JSON below is the complete bounded response to edit. Work "
                        "directly from it; do not regenerate a different lesson from the earlier "
                        "source payload. Preserve every valid fact and builder-owned field while "
                        "resolving every listed correction.\n\n"
                        "PREVIOUS_INVALID_STRUCTURED_RESPONSE:\n"
                        + json.dumps(
                            invalid_output_for_correction,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                else:
                    corrected_input += (
                        "\n\n"
                        + correction_message
                        + " Regenerate from the source and obey every REQUIRED_* and "
                        "FULL-COVERAGE rule exactly."
                    )
            if validation_error is not None and correction_context is not None:
                corrected_input += "\n\nAUTHORITATIVE_IMMUTABLE_CONTRACT (copy these identities and source fields exactly; preserve the requested atom order):\n" + json.dumps(correction_context, ensure_ascii=False, sort_keys=True)
            response = adapter.structured(
                logical_model=logical_model,
                reviewer=False,
                reasoning_effort=settings.generator_reasoning_effort,
                instructions=instructions,
                input_text=corrected_input,
                schema_name=schema_name,
                schema=schema,
                output_verbosity=request_output_verbosity,
            )
            try:
                if output_validator:
                    output_validator(response.data)
            except (TypeError, ValueError) as error:
                validation_error = error
                invalid_output_for_correction = response.data
                if semantic_attempt + 1 < settings.max_api_attempts:
                    continue
                structured_validation_failure = response
                raise
            break
    except Exception as error:
        if structured_validation_failure is not None:
            failed_output = structured_validation_failure.data
            failed_error_code = "OUTPUT_VALIDATION_FAILED"
            failed_model_snapshot = structured_validation_failure.model_snapshot
            failed_response_id = structured_validation_failure.response_id
            failed_attempt_count = semantic_attempt + 1
        else:
            failed_output = {"message": str(error)}
            failed_error_code = "OPENAI_REQUEST_FAILED"
            failed_model_snapshot = None
            failed_response_id = None
            failed_attempt_count = settings.max_api_attempts
        cache.record_attempt(
            "generation_steps",
            {
                "cache_key": key,
                "step_type": step_type,
                "input_hash": input_hash,
                "prompt_version": prompt_version,
                "logical_model": logical_model,
                "model_snapshot": failed_model_snapshot,
                "schema_version": "1",
                "status": "FAILED",
                "attempt_count": failed_attempt_count,
                "error_code": failed_error_code,
                "response_id": failed_response_id,
                "output_json": failed_output,
            },
        )
        raise
    cache.record_attempt(
        "generation_steps",
        {
            "cache_key": key,
            "step_type": step_type,
            "input_hash": input_hash,
            "prompt_version": prompt_version,
            "logical_model": logical_model,
            "model_snapshot": response.model_snapshot,
            "schema_version": "1",
            "status": "READY",
            "attempt_count": semantic_attempt + 1,
            "error_code": None,
            "response_id": response.response_id,
            "output_json": response.data,
        },
    )
    return response


def _with_curriculum_page_overrides(
    corpus: ExtractedCorpus,
    curriculum: dict[str, Any],
) -> ExtractedCorpus:
    """Add visually audited text for raster-only and partially extracted PDF pages."""

    updated = corpus.model_copy(deep=True)
    documents = {document.filename: document for document in updated.documents}
    chunks: list[DocumentChunk] = []
    chunk_by_source_ref_id: dict[str, DocumentChunk] = {}
    for chunk in updated.chunks:
        source_ref_id = chunk.source_ref.source_ref_id
        previous = chunk_by_source_ref_id.get(source_ref_id)
        if previous is None:
            chunk_by_source_ref_id[source_ref_id] = chunk
            chunks.append(chunk)
            continue
        if previous.model_dump(mode="json") != chunk.model_dump(mode="json"):
            raise ValueError(f"Conflicting source chunks share source_ref_id {source_ref_id}")
        # Some PDF code-cell extractors repeat an identical block on the same
        # physical page. It carries no additional evidence and cannot be owned
        # twice by a source-complete PackContract, so retain one exact copy.
    excluded_nontechnical_by_file: dict[str, set[int]] = {}
    removed_chunk_ids: set[str] = set()
    for raw_exclusion in curriculum.get("excluded_nontechnical_pages") or []:
        exclusion = dict(raw_exclusion)
        filename = str(exclusion.get("file") or "")
        page_number = int(exclusion.get("page") or 0)
        reason = str(exclusion.get("reason") or "").strip()
        document = documents.get(filename)
        if document is None or not 1 <= page_number <= document.page_count or not reason:
            raise ValueError(f"Invalid excluded nontechnical page: {exclusion}")
        pages = excluded_nontechnical_by_file.setdefault(filename, set())
        if page_number in pages:
            raise ValueError(f"Duplicate excluded nontechnical page: {filename} page {page_number}")
        overlapping = [
            chunk
            for chunk in chunks
            if chunk.document_id == document.document_id
            and chunk.page_start <= page_number <= chunk.page_end
        ]
        if not overlapping:
            raise ValueError(
                f"Excluded nontechnical page has no extracted text: {filename} page {page_number}"
            )
        if any(chunk.page_start != page_number or chunk.page_end != page_number for chunk in overlapping):
            raise ValueError(
                f"Cannot exclude nontechnical page from a multi-page chunk: {filename} page {page_number}"
            )
        pages.add(page_number)
        removed_chunk_ids.update(chunk.chunk_id for chunk in overlapping)
    chunks = [chunk for chunk in chunks if chunk.chunk_id not in removed_chunk_ids]
    occupied = {(chunk.document_id, chunk.page_start) for chunk in chunks}
    excluded_by_file: dict[str, set[int]] = {}
    for raw_exclusion in curriculum.get("excluded_image_only_pages") or []:
        exclusion = dict(raw_exclusion)
        filename = str(exclusion.get("file") or "")
        page_number = int(exclusion.get("page") or 0)
        reason = str(exclusion.get("reason") or "").strip()
        document = documents.get(filename)
        if document is None or not 1 <= page_number <= document.page_count or not reason:
            raise ValueError(f"Invalid excluded image-only page: {exclusion}")
        pages = excluded_by_file.setdefault(filename, set())
        if page_number in pages:
            raise ValueError(f"Duplicate excluded image-only page: {filename} page {page_number}")
        if page_number in excluded_nontechnical_by_file.get(filename, set()):
            raise ValueError(f"Page cannot use two exclusion types: {filename} page {page_number}")
        if (document.document_id, page_number) in occupied:
            raise ValueError(f"Excluded page already has extracted text: {filename} page {page_number}")
        pages.add(page_number)

    for raw_spec in curriculum.get("modules", []):
        spec = dict(raw_spec)
        filename = str(spec.get("output") or "")
        document = documents.get(filename)
        if document is None:
            raise ValueError(f"Curriculum source was not inspected: {filename}")
        raw_overrides = spec.get("page_text_overrides") or {}
        raw_supplements = spec.get("page_text_supplements") or {}
        if not isinstance(raw_overrides, dict):
            raise ValueError(f"page_text_overrides must be an object: {filename}")
        if not isinstance(raw_supplements, dict):
            raise ValueError(f"page_text_supplements must be an object: {filename}")
        override_pages = {int(page) for page in raw_overrides}
        supplement_pages = {int(page) for page in raw_supplements}
        excluded_pages = (
            excluded_by_file.get(filename, set())
            | excluded_nontechnical_by_file.get(filename, set())
        )
        if supplement_pages & override_pages:
            raise ValueError(f"Page cannot be both overridden and supplemented: {filename}")
        if override_pages & excluded_pages:
            raise ValueError(f"Page cannot be both transcribed and excluded: {filename}")
        if supplement_pages & excluded_pages:
            raise ValueError(f"Page cannot be both supplemented and excluded: {filename}")
        extracted_pages = {
            chunk.page_start for chunk in chunks if chunk.document_id == document.document_id
        }
        replaced_pages = override_pages & extracted_pages
        if replaced_pages:
            raise ValueError(
                f"Page override would replace extracted source text: {filename} page {min(replaced_pages)}"
            )
        supplement_without_text = supplement_pages - extracted_pages
        if supplement_without_text:
            raise ValueError(
                f"Page supplement requires existing extracted text: {filename} "
                f"page {min(supplement_without_text)}"
            )
        missing_pages = set(range(1, document.page_count + 1)) - extracted_pages
        declared_pages = override_pages | excluded_pages
        if missing_pages != declared_pages:
            raise ValueError(
                f"Image-only page audit mismatch for {filename}; "
                f"undeclared={sorted(missing_pages - declared_pages)}, "
                f"declared_but_not_missing={sorted(declared_pages - missing_pages)}"
            )
    for raw_spec in curriculum.get("modules", []):
        spec = dict(raw_spec)
        filename = str(spec.get("output") or "")
        document = documents.get(filename)
        if document is None:
            raise ValueError(f"Curriculum source was not inspected: {filename}")
        overrides = spec.get("page_text_overrides") or {}
        supplements = spec.get("page_text_supplements") or {}
        if not isinstance(overrides, dict):
            raise ValueError(f"page_text_overrides must be an object: {filename}")
        for raw_page, raw_text in overrides.items():
            page_number = int(raw_page)
            if not 1 <= page_number <= document.page_count:
                raise ValueError(f"Page override is outside {filename}: {page_number}")
            if (document.document_id, page_number) in occupied:
                raise ValueError(f"Page override would replace extracted source text: {filename} page {page_number}")
            text = normalize_text(str(raw_text))
            if len(text) < 10:
                raise ValueError(f"Page override is too short: {filename} page {page_number}")
            text_hash = sha256_bytes(text.encode("utf-8"))
            chunk_id = stable_id("chunk", document.document_id, str(page_number), text_hash)
            source_ref_id = stable_id("src", chunk_id)
            source_ref = SourceRef(
                source_ref_id=source_ref_id,
                document_id=document.document_id,
                chunk_id=chunk_id,
                page_start=page_number,
                page_end=page_number,
                anchor_start=0,
                anchor_end=len(text),
            )
            chunks.append(DocumentChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                page_start=page_number,
                page_end=page_number,
                heading_path=[f"Trang ảnh {page_number} · bản chép trực quan đã kiểm tra"],
                normalized_text=text,
                text_hash=text_hash,
                source_ref=source_ref,
            ))
            occupied.add((document.document_id, page_number))
        if not isinstance(supplements, dict):
            raise ValueError(f"page_text_supplements must be an object: {filename}")
        for raw_page, raw_text in supplements.items():
            page_number = int(raw_page)
            if not 1 <= page_number <= document.page_count:
                raise ValueError(f"Page supplement is outside {filename}: {page_number}")
            if (document.document_id, page_number) not in occupied:
                raise ValueError(f"Page supplement requires extracted text: {filename} page {page_number}")
            text = normalize_text(str(raw_text))
            if len(text) < 10:
                raise ValueError(f"Page supplement is too short: {filename} page {page_number}")
            text_hash = sha256_bytes(text.encode("utf-8"))
            chunk_id = stable_id("chunk", document.document_id, str(page_number), "visual-supplement", text_hash)
            source_ref_id = stable_id("src", chunk_id)
            source_ref = SourceRef(
                source_ref_id=source_ref_id,
                document_id=document.document_id,
                chunk_id=chunk_id,
                page_start=page_number,
                page_end=page_number,
                anchor_start=0,
                anchor_end=len(text),
            )
            chunks.append(DocumentChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                page_start=page_number,
                page_end=page_number,
                heading_path=[f"Page {page_number} visual supplement (audited)"],
                normalized_text=text,
                text_hash=text_hash,
                source_ref=source_ref,
            ))
    document_order = {document.document_id: index for index, document in enumerate(updated.documents)}
    chunks.sort(key=lambda chunk: (document_order[chunk.document_id], *_curriculum_chunk_order_key(chunk)))
    return ExtractedCorpus(
        documents=updated.documents,
        chunks=chunks,
        source_hash=sha256_json([chunk.model_dump(mode="json") for chunk in chunks]),
    )


def _curriculum_chunk_order_key(chunk: Any) -> tuple[int, int, int, str]:
    """Keep extracted page text before its audited visual supplement."""

    is_visual_supplement = any(
        heading.endswith("visual supplement (audited)")
        for heading in chunk.heading_path
    )
    return (
        int(chunk.page_start),
        int(chunk.page_end),
        1 if is_visual_supplement else 0,
        str(chunk.chunk_id),
    )


def _validate_page_atomic_assignments(
    atom_source_lists: list[list[str]],
    part_chunks: list[Any],
    module_id: str,
    max_page_groups_per_atom: int | None = None,
) -> None:
    """Enforce physical-page atomicity and the deterministic granularity cap."""

    if max_page_groups_per_atom is not None and max_page_groups_per_atom < 1:
        raise ValueError("max_page_groups_per_atom must be positive")

    atom_by_source = {
        source_ref_id: atom_index
        for atom_index, source_ids in enumerate(atom_source_lists)
        for source_ref_id in source_ids
    }
    page_groups: dict[tuple[str, int, int], list[str]] = {}
    for chunk in part_chunks:
        key = (str(chunk.document_id), int(chunk.page_start), int(chunk.page_end))
        page_groups.setdefault(key, []).append(chunk.source_ref.source_ref_id)
    page_group_counts = [0] * len(atom_source_lists)
    for (_, page_start, page_end), source_ids in page_groups.items():
        assigned_atoms = {
            atom_by_source[source_ref_id]
            for source_ref_id in source_ids
            if source_ref_id in atom_by_source
        }
        if len(assigned_atoms) > 1:
            raise ValueError(
                f"Physical page {page_start}-{page_end} is split across atoms for {module_id}"
            )
        if len(assigned_atoms) == 1:
            page_group_counts[next(iter(assigned_atoms))] += 1
    if max_page_groups_per_atom is not None:
        for atom_index, page_group_count in enumerate(page_group_counts, start=1):
            if page_group_count > max_page_groups_per_atom:
                raise ValueError(
                    f"Atom {atom_index} for {module_id} spans {page_group_count} physical "
                    f"page-groups; maximum is {max_page_groups_per_atom}"
                )


_INTERNAL_OUTLINE_TITLE_RE = re.compile(
    r"(?:\b(?:trang(?:\s+ảnh)?|slide|pages?)\s*(?:số\s*)?\d+\b|"
    r"visual\s+supplement|bản\s+chép\s+trực\s+quan|\baudited\b|"
    r"\b(?:phần|part)\s+\d+(?:\s*/\s*\d+)?\b)",
    re.IGNORECASE,
)


def _validate_semantic_outline_title(value: str, path: str) -> None:
    title = " ".join(value.split()).strip()
    if not 4 <= len(title) <= 180:
        raise ValueError(f"{path} must be a concise non-blank semantic title")
    if _INTERNAL_OUTLINE_TITLE_RE.search(title):
        raise ValueError(f"{path} exposes a page/range/internal vision label: {title!r}")


def _curriculum_outline(
    *,
    corpus: ExtractedCorpus,
    curriculum: dict[str, Any],
    cache: BuildCache,
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    prompt_version: str,
    resume: bool,
) -> tuple[CourseOutline, list[str]]:
    """Plan semantic lessons while preserving exact deterministic source ownership."""

    # The old boolean appears in already-planned catalog workspaces. Do not
    # let it silently bypass course-v2 semantic planning; deterministic mode
    # now requires an explicit legacy-only outline mode.
    if curriculum.get("outline_mode") == "DETERMINISTIC_LEGACY":
        return build_deterministic_curriculum_outline(
            corpus=corpus,
            curriculum=curriculum,
        ), []

    parts = semantic_curriculum_parts(corpus=corpus, curriculum=curriculum)
    target_pages_per_atom = int(curriculum.get("target_pages_per_atom", 4))
    default_max_page_groups_per_atom = target_pages_per_atom + 1
    max_page_groups_per_atom = int(
        curriculum.get("max_page_groups_per_atom", default_max_page_groups_per_atom)
    )
    if max_page_groups_per_atom < default_max_page_groups_per_atom:
        max_page_groups_per_atom = default_max_page_groups_per_atom
    outline_schema = responses_strict_schema(load_schema("course-outline.schema.json"))
    system = _prompt(f"{prompt_version}/system.txt")

    def plan_module(part: Any) -> tuple[int, str, ModuleOutline, str]:
        order_index = int(part.order_index)
        module_id = str(part.module_id)
        part_chunks = list(part.chunks)
        atom_count = int(part.atom_count)
        part_source_ids = {chunk.source_ref.source_ref_id for chunk in part_chunks}
        ordered_source_ids = [chunk.source_ref.source_ref_id for chunk in part_chunks]
        source_order = {source_ref_id: index for index, source_ref_id in enumerate(ordered_source_ids)}
        page_groups: dict[tuple[str, int, int], list[str]] = {}
        for chunk in part_chunks:
            page_key = (str(chunk.document_id), int(chunk.page_start), int(chunk.page_end))
            page_groups.setdefault(page_key, []).append(chunk.source_ref.source_ref_id)
        atomic_page_rule = (
            "All source_ref_ids from the same physical page must stay together in one atom; "
            "never put a page's extracted text and visual supplement in different atoms. "
            if any(len(group) > 1 for group in page_groups.values())
            else ""
        )
        required_atom_ids = list(part.required_atom_ids)
        task = (
            "TASK: Design one coherent Vietnamese learning module from this contiguous source unit.\n"
            f"REQUIRED_MODULE_ID: {module_id}\n"
            f"LECTURE_CONTEXT_TITLE: {part.lecture_title}\n"
            f"REQUIRED_ATOM_COUNT: {atom_count}\n"
            f"MAX_PHYSICAL_PAGE_GROUPS_PER_ATOM: {max_page_groups_per_atom}\n"
            f"REQUIRED_ATOM_IDS_IN_ORDER: {json.dumps(required_atom_ids)}\n"
            f"REQUIRED_SOURCE_REF_IDS_EACH_EXACTLY_ONCE: {json.dumps(ordered_source_ids)}\n"
            "Return exactly one module. Derive module.title and module.objective from the concepts actually taught, "
            "not from the filename, page range, slide heading, or extraction metadata. Each atom must teach one "
            "central concept with a concrete learner outcome; do not combine unrelated objectives merely because "
            "they are adjacent in the source. Titles are learner-facing Vietnamese labels: never mention page, "
            "slide, part numbering, visual supplement, audited transcription, or other internal import terms. "
            "Plan atoms in source order. Together they must cover every substantive supplied source block, including "
            "definitions, formulas, diagrams, procedures, examples, caveats and comparisons. Assign every REQUIRED "
            "source_ref_id to exactly one atom with no missing, extra or duplicate IDs, keep each atom's sources "
            "contiguous, keep every atom within MAX_PHYSICAL_PAGE_GROUPS_PER_ATOM, and place boundaries at "
            "genuine concept transitions. "
            + atomic_page_rule +
            "The returned course wrapper is temporary.\n\n"
            "SOURCE_CONTEXT:\n"
            + _source_context(corpus, part_source_ids)
        )
        def validate_outline_output(data: dict[str, Any]) -> None:
            candidate = CourseOutline.model_validate(data)
            if len(candidate.modules) != 1:
                raise ValueError(f"Expected exactly one module for {module_id}")
            raw_module = candidate.modules[0]
            if len(raw_module.atom_plans) != atom_count:
                raise ValueError(f"Expected exactly {atom_count} atoms for {module_id}")
            # Stable identity and ordering are builder-owned metadata, not
            # pedagogical model output. Restore them before semantic checks and
            # caching instead of spending retries because the model paraphrased
            # an ID while still returning a valid concept/source partition.
            raw_module.module_id = module_id
            raw_module.order_index = order_index
            for atom_index, plan in enumerate(raw_module.atom_plans):
                plan.atom_id = required_atom_ids[atom_index]
                plan.order_index = atom_index
                plan.prerequisite_ids = []
            page_source_groups: list[list[str]] = []
            current_page_key: tuple[str, int, int] | None = None
            for chunk in part_chunks:
                page_key = (
                    str(chunk.document_id),
                    int(chunk.page_start),
                    int(chunk.page_end),
                )
                if page_key != current_page_key:
                    page_source_groups.append([])
                    current_page_key = page_key
                page_source_groups[-1].append(chunk.source_ref.source_ref_id)
            if len(page_source_groups) >= len(raw_module.atom_plans):
                base_count, remainder = divmod(
                    len(page_source_groups),
                    len(raw_module.atom_plans),
                )
                cursor = 0
                for atom_index, plan in enumerate(raw_module.atom_plans):
                    take = base_count + (1 if atom_index < remainder else 0)
                    assigned_groups = page_source_groups[cursor : cursor + take]
                    plan.source_ref_ids = [
                        source_ref_id
                        for group in assigned_groups
                        for source_ref_id in group
                    ]
                    cursor += take
            _validate_semantic_outline_title(raw_module.title, f"{module_id}.title")
            if not raw_module.objective.strip():
                raise ValueError(f"{module_id}.objective must state a concrete learner outcome")
            title_keys: set[str] = set()
            objective_keys: set[str] = set()
            assigned: list[str] = []
            previous_last_rank = -1
            for atom_index, plan in enumerate(raw_module.atom_plans):
                _validate_semantic_outline_title(
                    plan.title,
                    f"{module_id}.atom_plans[{atom_index}].title",
                )
                title_key = " ".join(plan.title.casefold().split())
                objective_key = " ".join(plan.objective.casefold().split())
                if title_key in title_keys or objective_key in objective_keys or not objective_key:
                    raise ValueError(f"Atoms must have distinct concepts/objectives for {module_id}")
                title_keys.add(title_key)
                objective_keys.add(objective_key)
                selected = plan.source_ref_ids
                if not selected or len(selected) != len(set(selected)):
                    raise ValueError(f"Atom {atom_index + 1} has empty/duplicate sources for {module_id}")
                unknown = set(selected) - part_source_ids
                if unknown:
                    raise ValueError(f"Atom {atom_index + 1} has unknown sources for {module_id}: {sorted(unknown)}")
                ranks = [source_order[source_ref_id] for source_ref_id in selected]
                if ranks != sorted(ranks) or ranks != list(range(min(ranks), max(ranks) + 1)):
                    raise ValueError(f"Atom {atom_index + 1} sources are not contiguous for {module_id}")
                if ranks[0] != previous_last_rank + 1:
                    raise ValueError(f"Atom source order overlaps/reverses chunks for {module_id}")
                previous_last_rank = ranks[-1]
                assigned.extend(selected)
            if len(assigned) != len(set(assigned)) or set(assigned) != part_source_ids:
                raise ValueError(
                    f"Module source coverage mismatch for {module_id}; "
                    f"missing={sorted(part_source_ids - set(assigned))}, "
                    f"duplicate_count={len(assigned) - len(set(assigned))}"
                )
            _validate_page_atomic_assignments(
                [list(plan.source_ref_ids) for plan in candidate.modules[0].atom_plans],
                part_chunks,
                module_id,
                max_page_groups_per_atom,
            )
            data.clear()
            data.update(candidate.model_dump(mode="json", by_alias=True))

        response = _cached_call(
            cache=cache,
            adapter=adapter,
            settings=settings,
            step_type=f"GENERATE_MODULE_OUTLINE:{module_id}",
            input_hash=sha256_bytes(task.encode("utf-8")),
            prompt_version=prompt_version,
            logical_model=model,
            instructions=system,
            input_text=task,
            schema_name=f"course_module_{order_index}",
            schema=outline_schema,
            resume=resume,
            output_validator=validate_outline_output,
        )
        proposed = CourseOutline.model_validate(response.data)
        if len(proposed.modules) != 1:
            raise ValueError(f"Module planner returned {len(proposed.modules)} modules for {module_id}")
        raw_module = proposed.modules[0]
        if len(raw_module.atom_plans) != atom_count:
            raise ValueError(
                f"Module planner returned {len(raw_module.atom_plans)} atoms for {module_id}; "
                f"expected {atom_count}"
            )
        plans: list[AtomPlan] = []
        for atom_index, raw_plan in enumerate(raw_module.atom_plans):
            selected = sorted(raw_plan.source_ref_ids, key=source_order.__getitem__)
            plans.append(
                AtomPlan(
                    atom_id=required_atom_ids[atom_index],
                    order_index=atom_index,
                    title=raw_plan.title,
                    objective=raw_plan.objective,
                    prerequisite_ids=[],
                    source_ref_ids=selected,
                )
            )
        return order_index, str(part.lecture_id), ModuleOutline(
            module_id=module_id,
            order_index=order_index,
            title=raw_module.title.strip(),
            objective=raw_module.objective,
            atom_plans=plans,
        ), response.model_snapshot

    completed: list[tuple[int, str, ModuleOutline, str]] = []
    with ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix="9router-outline") as pool:
        futures = [
            pool.submit(plan_module, part)
            for part in parts
        ]
        for future in as_completed(futures):
            completed.append(future.result())
    completed.sort(key=lambda item: item[0])
    modules = [item[2] for item in completed]

    expected_source_ids = [
        chunk.source_ref.source_ref_id
        for part in parts
        for chunk in part.chunks
    ]
    actual_source_ids = [
        source_ref_id
        for module in modules
        for plan in module.atom_plans
        for source_ref_id in plan.source_ref_ids
    ]
    if actual_source_ids != expected_source_ids:
        raise ValueError("Semantic outline changed exact ordered source_ref coverage")

    previous_by_lecture: dict[str, str] = {}
    for _, lecture_id, module, _ in completed:
        previous_atom_id = previous_by_lecture.get(lecture_id)
        for atom_index, plan in enumerate(module.atom_plans):
            plan.prerequisite_ids = [previous_atom_id] if previous_atom_id else []
            previous_atom_id = plan.atom_id
        if previous_atom_id is None:
            raise ValueError(f"Lecture part has no atoms: {module.module_id}")
        previous_by_lecture[lecture_id] = previous_atom_id
    course = Course(
        course_id=str(curriculum.get("course_id") or "course_deep_learning_core_vi"),
        title=str(curriculum.get("title") or "Deep Learning từ bài giảng thực tế"),
        description=str(curriculum.get("description") or "Khóa học được tạo từ bộ Lecture."),
        language="vi",
    )
    return CourseOutline(course=course, modules=modules), [item[3] for item in completed]


def _deterministic_mindmap(outline: CourseOutline, source_hash: str) -> MindMap:
    nodes = [
        MindMapNode(id=outline.course.course_id, label=outline.course.title, type="COURSE", atom_id=None),
    ]
    edges: list[MindMapEdge] = []
    for module in outline.modules:
        nodes.append(MindMapNode(id=module.module_id, label=module.title, type="MODULE", atom_id=None))
        edges.append(MindMapEdge(**{"from": outline.course.course_id, "to": module.module_id, "type": "CONTAINS"}))
        for plan in module.atom_plans:
            nodes.append(MindMapNode(id=plan.atom_id, label=plan.title, type="ATOM", atom_id=plan.atom_id))
            edges.append(MindMapEdge(**{"from": module.module_id, "to": plan.atom_id, "type": "CONTAINS"}))
            edges.extend(
                MindMapEdge(**{"from": prerequisite, "to": plan.atom_id, "type": "PREREQUISITE"})
                for prerequisite in plan.prerequisite_ids
            )
    return MindMap(
        mind_map_id=stable_id("map", source_hash, outline.course.course_id),
        root_node_id=outline.course.course_id,
        nodes=nodes,
        edges=edges,
    )


def _synchronize_outline_with_generated_lessons(
    outline: CourseOutline,
    generated: dict[str, tuple[AtomContent, str]],
) -> None:
    """Promote final lesson wording before any learner-facing graph is built."""

    for module in outline.modules:
        for plan in module.atom_plans:
            lesson = generated[plan.atom_id][0].lesson
            _validate_semantic_outline_title(
                lesson.title,
                f"{module.module_id}.{plan.atom_id}.lesson.title",
            )
            plan.title = lesson.title.strip()
            plan.objective = lesson.learning_objective.strip()


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError("Illustration is not a valid PNG")
    return struct.unpack(">II", payload[16:24])


def _attach_curriculum_illustrations(
    *,
    corpus: ExtractedCorpus,
    curriculum: dict[str, Any],
    workdir: Path,
    outline: CourseOutline,
    lessons: list[Any],
    content_id_seed: str,
) -> list[IllustrationAsset]:
    """Attach each audited figure to the exact page-bounded atom that owns it."""

    documents = {document.filename: document for document in corpus.documents}
    chunks_by_document: dict[str, list[Any]] = {}
    for chunk in corpus.chunks:
        chunks_by_document.setdefault(chunk.document_id, []).append(chunk)
    lessons_by_atom = {lesson.atom_id: lesson for lesson in lessons}
    module_by_source_ref_id = {
        source_ref_id: module
        for module in outline.modules
        for plan in module.atom_plans
        for source_ref_id in plan.source_ref_ids
    }
    illustrations: list[IllustrationAsset] = []
    for raw_spec in curriculum.get("modules", []):
        spec = dict(raw_spec)
        lecture_id = str(spec["module_id"])
        filename = str(spec["output"])
        document = documents.get(filename)
        if document is None:
            raise ValueError(f"Cannot attach illustration for unknown source: {filename}")
        if spec.get("illustrations") is not None:
            visual_specs = list(spec.get("illustrations") or [])
        else:
            visual_specs = list(spec.get("additional_illustrations") or [])
            if spec.get("illustration_page") is not None:
                visual_specs.insert(0, {
                    "page": spec["illustration_page"],
                    "caption": spec["illustration_caption"],
                    "asset_member": f"illustration_{lecture_id}.png",
                })
        for raw_visual in visual_specs:
            visual = dict(raw_visual)
            page_number = int(visual["page"])
            caption = str(visual["caption"]).strip()
            asset_member = str(visual["asset_member"])
            occurrence_seed = str(visual.get("occurrence_id") or "").strip() or None
            page_chunks = [
                chunk
                for chunk in chunks_by_document.get(document.document_id, [])
                if chunk.page_start <= page_number <= chunk.page_end
            ]
            if not page_chunks:
                raise ValueError(f"Illustration page {page_number} has no source ref in {filename}")
            source_ref_id = max(page_chunks, key=lambda chunk: len(chunk.normalized_text)).source_ref.source_ref_id
            module = module_by_source_ref_id.get(source_ref_id)
            if module is None:
                raise ValueError(
                    f"Cannot attach illustration outside generated modules: {lecture_id} page {page_number}"
                )
            target_plan = next(
                (plan for plan in module.atom_plans if source_ref_id in plan.source_ref_ids),
                None,
            )
            if target_plan is None:
                raise ValueError(f"Illustration source is not assigned to an atom: {lecture_id} page {page_number}")
            target_lesson = lessons_by_atom[target_plan.atom_id]
            if source_ref_id not in target_lesson.source_ref_ids:
                raise ValueError(f"Illustration source is outside generated lesson: {target_lesson.lesson_id}")
            if source_ref_id not in target_lesson.illustration_source_ref_ids:
                target_lesson.illustration_source_ref_ids.append(source_ref_id)

            media_path = workdir / "media" / asset_member
            if not media_path.is_file():
                raise ValueError(f"Missing visually audited illustration: {media_path}")
            payload = media_path.read_bytes()
            width, height = _png_dimensions(payload)
            illustrations.append(
                IllustrationAsset(
                    illustration_id=curriculum_illustration_id(
                        content_id_seed=content_id_seed,
                        lecture_id=lecture_id,
                        page_number=page_number,
                        occurrence_seed=occurrence_seed,
                    ),
                    source_ref_id=source_ref_id,
                    asset_member=asset_member,
                    mime_type="image/png",
                    sha256=sha256_bytes(payload),
                    byte_size=len(payload),
                    width=width,
                    height=height,
                    page_number=page_number,
                    caption=caption,
                    alt_text=str(
                        visual.get("alt_text")
                        or f"{caption} Nguồn: {filename}, trang {page_number}."
                    ).strip(),
                )
            )
    return illustrations


def _atom_task_prompt(
    plan: Any,
    module_id: str,
    corpus: ExtractedCorpus,
    content_id_seed: str | None = None,
) -> str:
    identity_seed = content_id_seed or corpus.source_hash
    pair_id = stable_id("pair", identity_seed, plan.atom_id)
    lesson_id = stable_id("lesson", pair_id)
    question_ids = [stable_id("q", pair_id, str(index)) for index in range(1, 6)]
    lab_id = stable_id("lab", pair_id)
    return (
        "TASK: Generate one KnowledgeAtom content bundle with five candidate questions.\n"
        "LANGUAGE: Vietnamese; preserve standard English technical terms.\n"
        f"MODULE_ID: {module_id}\nATOM_PLAN: {json.dumps(plan.model_dump(mode='json'), ensure_ascii=False)}\n"
        f"REQUIRED_PAIR_ID: {pair_id}\nREQUIRED_LESSON_ID: {lesson_id}\n"
        f"REQUIRED_QUESTION_IDS_IN_ORDER: {json.dumps(question_ids)}\nREQUIRED_MINI_LAB_ID: {lab_id}\n"
        f"REQUIRED_SOURCE_REF_IDS_IN_ORDER: {json.dumps(plan.source_ref_ids)}\n"
        f"REQUIRED_PREREQUISITE_IDS: {json.dumps(plan.prerequisite_ids)}\n"
        "FULL-COVERAGE RULE: the lesson must teach all substantive material in every supplied source block, "
        "including definitions, equations, diagram meaning, procedures, examples, comparisons and caveats. "
        "Do not collapse the sources into headline-only notes. Emit lesson.source_ref_ids exactly as required. "
        "Use mini_lab=null only if none of the five whitelisted lab types is supported by evidence. "
        "Set all ai_review_status fields to PENDING. Do not emit IDs not listed in source context.\n\n"
        "SOURCE_CONTEXT:\n"
        + _source_context(corpus, set(plan.source_ref_ids))
    )


def _module_atom_batch_task_prompt(
    module: ModuleOutline,
    corpus: ExtractedCorpus,
    content_id_seed: str,
    plans: list[AtomPlan] | None = None,
) -> str:
    selected_plans = list(module.atom_plans if plans is None else plans)
    if not selected_plans:
        raise ValueError(f"Module atom group cannot be empty: {module.module_id}")
    required: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for plan in selected_plans:
        pair_id = stable_id("pair", content_id_seed, plan.atom_id)
        source_ids.update(plan.source_ref_ids)
        required.append({
            "atom_plan": plan.model_dump(mode="json"),
            "required_pair_id": pair_id,
            "required_lesson_id": stable_id("lesson", pair_id),
            "required_question_ids_in_order": [
                stable_id("q", pair_id, str(index)) for index in range(1, 6)
            ],
            "required_mini_lab_id": stable_id("lab", pair_id),
        })
    return (
        "TASK: Generate one contiguous group of KnowledgeAtom content bundles from a lecture part.\n"
        "LANGUAGE: Vietnamese; preserve standard English technical terms.\n"
        f"MODULE_ID: {module.module_id}\n"
        f"GROUP_SIZE: {len(selected_plans)}\n"
        f"REQUIRED_ATOM_BUNDLES_IN_ORDER: {json.dumps(required, ensure_ascii=False)}\n"
        "Return atom_contents in exactly that order, one item per required atom. Each lesson must teach all "
        "substantive material in every source block assigned to that atom, including definitions, equations, "
        "diagram meaning, procedures, examples, comparisons and caveats. Do not collapse sources into "
        "headline-only notes. Copy all required stable IDs, prerequisite IDs and source_ref_ids exactly. "
        "Generate exactly five questions for every atom. Use mini_lab=null unless a supported lab is directly "
        "grounded. Set every ai_review_status to PENDING.\n\nSOURCE_CONTEXT:\n"
        + _source_context(corpus, source_ids)
    )


def _contiguous_module_atom_groups(
    module: ModuleOutline,
    max_atoms_per_call: int,
) -> list[list[AtomPlan]]:
    """Partition a module without reordering atoms or crossing group boundaries."""

    limit = max(1, min(5, int(max_atoms_per_call)))
    plans = list(module.atom_plans)
    return [plans[index:index + limit] for index in range(0, len(plans), limit)]


def _atom_content_batch_schema(atom_schema: dict[str, Any], count: int) -> dict[str, Any]:
    if not 1 <= count <= 10:
        raise ValueError("Atom batch count must be between 1 and 10")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["atom_contents"],
        "properties": {
            "atom_contents": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": atom_schema,
            }
        },
    }


_OBJECTIVE_WORD_RE = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)
_VAGUE_OBJECTIVE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:sau\s+(?:bài(?:\s+học)?\s+này|khi\s+học\s+xong)[,:]?\s*)?"
    r"(?:(?:người\s+học|bạn)\s+)?(?:(?:sẽ|có\s+thể)\s+)?)"
    r"(?:hiểu(?:\s+được)?|nắm(?:\s+được)?|biết)\s+(?:rõ\s+)?(?:về\s+)?",
    re.IGNORECASE,
)


def _first_top_level_clause(value: str) -> str:
    """Return the first prose clause without splitting code/list delimiters."""

    opening = {"(": ")", "[": "]", "{": "}"}
    closing = set(opening.values())
    stack: list[str] = []
    in_backticks = False
    for index, character in enumerate(value):
        if character == "`":
            in_backticks = not in_backticks
            continue
        if in_backticks:
            continue
        if character in opening:
            stack.append(opening[character])
            continue
        if character in closing:
            if stack and character == stack[-1]:
                stack.pop()
            continue
        if not stack and character in ",;:":
            return value[:index].strip()
        if not stack and character in ".!?" and (
            index + 1 == len(value) or value[index + 1].isspace()
        ):
            return value[:index].strip()
    return value.strip()


def _canonical_plan_objective(plan: Any) -> str:
    """Create one concise, source-grounded capability from a semantic plan."""

    objective = " ".join(str(plan.objective).split())
    first_clause = _first_top_level_clause(objective)
    word_count = len(_OBJECTIVE_WORD_RE.findall(first_clause))
    if (
        6 <= word_count <= 28
        and has_observable_learning_objective(first_clause)
    ):
        return first_clause.rstrip(".,;:!?") + "."

    # Preserve the plan's actual capability when it uses a natural but
    # non-observable opener ("hiểu/nắm/biết"). Replacing the whole objective
    # with "giải thích khái niệm <title>" erased the operation, condition and
    # expected result that made the lesson objective specific.
    semantic_body = _VAGUE_OBJECTIVE_PREFIX_RE.sub("", first_clause).strip(" .,:;!?")
    semantic_word_count = len(_OBJECTIVE_WORD_RE.findall(semantic_body))
    if 4 <= semantic_word_count <= 24 and semantic_body != first_clause.strip(" .,:;!?"):
        return f"Người học có thể mô tả {semantic_body}."

    title = " ".join(str(plan.title).split()).strip(" .,:;!?")
    return f"Người học có thể mô tả vai trò của {title} trong nội dung đang học."


def _is_generic_plan_fallback(value: str, plan: Any) -> bool:
    """Recognize the old title-only fallback so cached atoms can be upgraded."""

    title = " ".join(str(plan.title).split()).strip(" .,:;!?")
    legacy_fallback = f"Người học có thể giải thích khái niệm {title}."
    current_fallback = (
        f"Người học có thể mô tả vai trò của {title} trong nội dung đang học."
    )
    normalized = " ".join(value.split()).casefold()
    return normalized in {legacy_fallback.casefold(), current_fallback.casefold()}


def _normalize_generated_atom(
    content: AtomContent,
    plan: Any,
    corpus: ExtractedCorpus,
    allowed_mini_lab_atom_ids: set[str] | None = None,
    suppress_formula_block_atom_ids: set[str] | None = None,
    content_id_seed: str | None = None,
    pedagogical: bool = False,
) -> AtomContent:
    identity_seed = content_id_seed or corpus.source_hash
    pair_id = stable_id("pair", identity_seed, plan.atom_id)
    lesson_id = stable_id("lesson", pair_id)
    expected_questions = [stable_id("q", pair_id, str(index)) for index in range(1, 6)]
    allowed_sources = set(plan.source_ref_ids)
    if set(content.lesson.source_ref_ids) != allowed_sources:
        missing = allowed_sources - set(content.lesson.source_ref_ids)
        extra = set(content.lesson.source_ref_ids) - allowed_sources
        raise ValueError(
            f"Lesson source coverage mismatch for {plan.atom_id}; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    content.lesson.source_ref_ids = list(plan.source_ref_ids)
    content.lesson.atom_id = plan.atom_id
    content.lesson.pair_id = pair_id
    content.lesson.lesson_id = lesson_id
    content.lesson.prerequisite_ids = list(plan.prerequisite_ids)
    content.lesson.illustration_source_ref_ids = []
    if (
        pedagogical
        and (
            not has_observable_learning_objective(content.lesson.learning_objective)
            or _is_generic_plan_fallback(content.lesson.learning_objective, plan)
        )
    ):
        content.lesson.learning_objective = _canonical_plan_objective(plan)
    content.lesson.takeaways = _normalize_takeaway_spacing(content.lesson.takeaways)
    if suppress_formula_block_atom_ids is not None and plan.atom_id in suppress_formula_block_atom_ids:
        content.lesson.formula_blocks = []
    if len(content.quiz_bundle.questions) != len(expected_questions):
        raise ValueError(f"Generator must return exactly five questions for {plan.atom_id}")
    for question, question_id in zip(
        content.quiz_bundle.questions,
        expected_questions,
        strict=True,
    ):
        if not set(question.source_ref_ids) <= allowed_sources:
            raise ValueError(
                f"Question escaped source boundaries for {plan.atom_id}: "
                f"question={question.question_id}, "
                f"extra={sorted(set(question.source_ref_ids) - allowed_sources)}, "
                f"allowed={sorted(allowed_sources)}"
            )
        question.question_id = question_id
        question.atom_id = plan.atom_id
        question.pair_id = pair_id
    rebalance_question_options(content.quiz_bundle.questions, plan.atom_id)
    # This gate deliberately runs after every builder-owned normalization and
    # option rotation. It therefore revalidates old READY cache payloads before
    # they can be assembled into a candidate pack; a real duplicate triggers
    # _cached_call's correction/regeneration path instead of failing much later
    # at the pack validator.
    # Pass one deliberately preserves the rough, source-complete payload for
    # the learner rewrite. Learner-facing metadata/distractor polish belongs to
    # pass two; structural quiz safety still runs here.
    validate_quiz_editorial(
        content.quiz_bundle.questions,
        learner_polish=False,
    )
    if content.mini_lab and not _mini_lab_matches_runtime(
        plan,
        content.mini_lab.lab_type,
        allowed_mini_lab_atom_ids,
    ):
        content.mini_lab = None
    if content.mini_lab and content.mini_lab.atom_id != plan.atom_id:
        raise ValueError(f"Mini Lab atom mismatch for {plan.atom_id}")
    if content.mini_lab and not set(content.mini_lab.source_ref_ids) <= allowed_sources:
        raise ValueError(f"Mini Lab escaped source boundaries for {plan.atom_id}")
    return content


def _validate_generated_lesson_editorial(
    content: AtomContent,
    *,
    pedagogical: bool = False,
) -> None:
    issues = validate_lesson_editorial(
        content.lesson,
        pedagogical=pedagogical,
        learner_polish=False,
    )
    if issues:
        details = ", ".join(
            f"{issue.code}@{issue.path}"
            for issue in issues
        )
        raise ValueError(f"Lesson editorial gate failed: {details}")


def _build_source_excerpts(
    corpus: ExtractedCorpus,
    source_ids: set[str] | None = None,
) -> list[SourceExcerpt]:
    documents = {item.document_id: item for item in corpus.documents}
    excerpts: list[SourceExcerpt] = []
    for chunk in corpus.chunks:
        if source_ids is not None and chunk.source_ref.source_ref_id not in source_ids:
            continue
        ref = chunk.source_ref
        excerpts.append(
            SourceExcerpt(
                source_excerpt_id=stable_id("excerpt", ref.source_ref_id),
                source_ref_id=ref.source_ref_id,
                document_id=ref.document_id,
                chunk_id=ref.chunk_id,
                document_label=documents[ref.document_id].filename,
                heading_path=chunk.heading_path,
                page_start=ref.page_start,
                page_end=ref.page_end,
                context_text=chunk.normalized_text,
                highlight_start=0,
                highlight_end=len(chunk.normalized_text),
                excerpt_hash=sha256_bytes(chunk.normalized_text.encode("utf-8")),
            )
        )
    return excerpts


def generate_candidate(
    *,
    corpus: ExtractedCorpus,
    workdir: Path,
    settings: Settings,
    model: str,
    prompt_version: str,
    resume: bool,
    curriculum: dict[str, Any] | None = None,
    adapter: ResponsesAdapter | None = None,
    content_request_limit: int | None = None,
) -> PackData:
    _require_supported_model(model, stage="generation")
    if content_request_limit is not None:
        if (
            isinstance(content_request_limit, bool)
            or not isinstance(content_request_limit, int)
            or content_request_limit < 1
        ):
            raise ValueError("content_request_limit must be a positive integer")
        if settings.concurrency != 1 or settings.max_api_attempts != 1:
            raise ValueError(
                "Bounded content generation requires concurrency=1 and max_api_attempts=1"
            )
    if curriculum:
        corpus = _with_curriculum_page_overrides(corpus, curriculum)
    content_id_seed = resolve_content_id_seed(corpus.source_hash, curriculum)
    adapter = adapter or build_adapter(settings)
    content_budget = (
        _ContentRequestBudget(content_request_limit)
        if content_request_limit is not None
        else None
    )
    content_adapter = (
        _BudgetedContentAdapter(adapter, content_budget)
        if content_budget is not None
        else adapter
    )
    cache = BuildCache(workdir / "cache.sqlite3")
    try:
        system = _prompt(f"{prompt_version}/system.txt")
        outline_snapshots: list[str]
        if curriculum:
            outline, outline_snapshots = _curriculum_outline(
                corpus=corpus,
                curriculum=curriculum,
                cache=cache,
                adapter=adapter,
                settings=settings,
                model=model,
                prompt_version=prompt_version,
                resume=resume,
            )
        else:
            outline_schema = responses_strict_schema(load_schema("course-outline.schema.json"))
            outline_response = _cached_call(
                cache=cache,
                adapter=adapter,
                settings=settings,
                step_type="GENERATE_OUTLINE",
                input_hash=corpus.source_hash,
                prompt_version=prompt_version,
                logical_model=model,
                instructions=system,
                input_text=(
                    "TASK: Create one compact course outline. Produce 3-5 atoms total for this fixture; "
                    "source_ref_ids must come from context. Stable atom/module/course IDs should be concise.\n\n"
                    "SOURCE_CONTEXT:\n" + _source_context(corpus)
                ),
                schema_name="course_outline",
                schema=outline_schema,
                resume=resume,
            )
            outline = CourseOutline.model_validate(outline_response.data)
            outline_snapshots = [outline_response.model_snapshot]
        all_plans = [(module, plan) for module in outline.modules for plan in module.atom_plans]
        allowed_mini_lab_atom_ids = (
            set(curriculum.get("allowed_mini_lab_atom_ids", [])) if curriculum is not None else None
        )
        allowed_mini_lab_types = (
            dict(curriculum.get("allowed_mini_lab_types", {})) if curriculum is not None else None
        )
        suppress_formula_block_atom_ids = (
            set(curriculum.get("suppress_formula_block_atom_ids", []))
            if curriculum is not None
            else None
        )
        known_atom_ids = {plan.atom_id for _, plan in all_plans}
        if allowed_mini_lab_atom_ids is not None and not allowed_mini_lab_atom_ids <= known_atom_ids:
            raise ValueError(
                "Curriculum mini-lab allowlist contains unknown atom IDs: "
                f"{sorted(allowed_mini_lab_atom_ids - known_atom_ids)}"
            )
        if (
            allowed_mini_lab_atom_ids is not None
            and allowed_mini_lab_types is not None
            and set(allowed_mini_lab_types) != allowed_mini_lab_atom_ids
        ):
            raise ValueError("Curriculum mini-lab ID allowlist and type contract must match exactly")
        if (
            suppress_formula_block_atom_ids is not None
            and not suppress_formula_block_atom_ids <= known_atom_ids
        ):
            raise ValueError(
                "Curriculum formula suppression contains unknown atom IDs: "
                f"{sorted(suppress_formula_block_atom_ids - known_atom_ids)}"
            )
        source_ids = {chunk.source_ref.source_ref_id for chunk in corpus.chunks}
        if any(not set(plan.source_ref_ids) <= source_ids for _, plan in all_plans):
            raise ValueError("Outline contains source_ref IDs outside the extracted corpus")

        atom_schema = responses_strict_schema(load_schema("atom-content.schema.json"))
        generated: dict[str, tuple[AtomContent, str]] = {}

        def run_atom(module: Any, plan: Any) -> tuple[str, AtomContent, str]:
            task = _atom_task_prompt(plan, module.module_id, corpus, content_id_seed)
            def validate_atom_output(data: dict[str, Any]) -> None:
                candidate = AtomContent.model_validate(data)
                expected_lab_type = (
                    allowed_mini_lab_types.get(plan.atom_id)
                    if allowed_mini_lab_types is not None
                    else None
                )
                if expected_lab_type is not None and (
                    candidate.mini_lab is None or candidate.mini_lab.lab_type != expected_lab_type
                ):
                    raise ValueError(
                        f"Atom {plan.atom_id} requires audited Mini Lab type {expected_lab_type}"
                    )
                _normalize_generated_atom(
                    candidate,
                    plan,
                    corpus,
                    allowed_mini_lab_atom_ids,
                    suppress_formula_block_atom_ids,
                    content_id_seed,
                    pedagogical=is_course_v2_prompt_version(prompt_version),
                )
                _validate_generated_lesson_editorial(
                    candidate,
                    pedagogical=is_course_v2_prompt_version(prompt_version),
                )
                _validate_takeaway_editorial(candidate.lesson.takeaways)

            response = _cached_call(
                cache=cache,
                adapter=content_adapter,
                settings=settings,
                step_type="GENERATE_ATOM_CONTENT",
                input_hash=sha256_bytes(task.encode("utf-8")),
                prompt_version=prompt_version,
                logical_model=model,
                instructions=_prompt(f"{prompt_version}/atom-system.txt"),
                input_text=task,
                schema_name="atom_content",
                schema=atom_schema,
                resume=resume,
                output_validator=validate_atom_output,
                validation_feedback_max_chars=4_000,
                include_invalid_output_in_correction=True,
                correction_output_verbosity="low",
                correction_context={
                    "module_id": module.module_id,
                    "atom_id": plan.atom_id,
                    "required_lesson_source_ref_ids": list(plan.source_ref_ids),
                    "allowed_question_and_mini_lab_source_ref_ids": list(plan.source_ref_ids),
                    "prerequisite_ids": list(plan.prerequisite_ids),
                    "source_context": _source_context(corpus, set(plan.source_ref_ids)),
                },
            )
            content = _normalize_generated_atom(
                AtomContent.model_validate(response.data),
                plan,
                corpus,
                allowed_mini_lab_atom_ids,
                suppress_formula_block_atom_ids,
                content_id_seed,
                pedagogical=is_course_v2_prompt_version(prompt_version),
            )
            return plan.atom_id, content, response.model_snapshot

        def run_module_batch(
            module: ModuleOutline,
            plans: list[AtomPlan],
            group_index: int,
            group_count: int,
        ) -> list[tuple[str, AtomContent, str]]:
            if not 2 <= len(plans) <= settings.max_atoms_per_generation_call:
                raise ValueError(
                    f"Invalid atom batch size for {module.module_id}: {len(plans)}"
                )
            atom_ids = [plan.atom_id for plan in plans]
            group_identity = sha256_json({
                "module_id": module.module_id,
                "atom_ids": atom_ids,
            })
            task = _module_atom_batch_task_prompt(
                module,
                corpus,
                content_id_seed,
                plans,
            )
            task += (
                f"\nGROUP_INDEX: {group_index + 1}\n"
                f"GROUP_COUNT: {group_count}\n"
                f"GROUP_IDENTITY_SHA256: {group_identity}\n"
            )

            def parse_batch(data: dict[str, Any]) -> list[AtomContent]:
                raw_items = data.get("atom_contents")
                if not isinstance(raw_items, list) or len(raw_items) != len(plans):
                    raise ValueError(
                        f"Module group {module.module_id}/{group_index + 1} returned "
                        "the wrong number of atoms"
                    )
                contents = [AtomContent.model_validate(item) for item in raw_items]
                for plan, candidate in zip(plans, contents, strict=True):
                    expected_lab_type = (
                        allowed_mini_lab_types.get(plan.atom_id)
                        if allowed_mini_lab_types is not None
                        else None
                    )
                    if expected_lab_type is not None and (
                        candidate.mini_lab is None
                        or candidate.mini_lab.lab_type != expected_lab_type
                    ):
                        raise ValueError(
                            f"Atom {plan.atom_id} requires audited Mini Lab type {expected_lab_type}"
                        )
                    _normalize_generated_atom(
                        candidate,
                        plan,
                        corpus,
                        allowed_mini_lab_atom_ids,
                        suppress_formula_block_atom_ids,
                        content_id_seed,
                        pedagogical=is_course_v2_prompt_version(prompt_version),
                    )
                    _validate_generated_lesson_editorial(
                        candidate,
                        pedagogical=is_course_v2_prompt_version(prompt_version),
                    )
                    _validate_takeaway_editorial(candidate.lesson.takeaways)
                return contents

            response = _cached_call(
                cache=cache,
                adapter=content_adapter,
                settings=settings,
                step_type=(
                    f"GENERATE_MODULE_CONTENT_GROUP:{module.module_id}:"
                    f"LIMIT:{settings.max_atoms_per_generation_call}:"
                    f"INDEX:{group_index + 1}-OF-{group_count}:SIZE:{len(plans)}:"
                    f"ATOMS:{group_identity[7:23]}"
                ),
                input_hash=sha256_bytes(task.encode("utf-8")),
                prompt_version=prompt_version,
                logical_model=model,
                instructions=_prompt(f"{prompt_version}/atom-system.txt"),
                input_text=task,
                schema_name=f"module_atom_contents_{len(plans)}_group_{group_index + 1}",
                schema=_atom_content_batch_schema(atom_schema, len(plans)),
                resume=resume,
                output_validator=lambda data: parse_batch(data),
            )
            return [
                (plan.atom_id, content, response.model_snapshot)
                for plan, content in zip(plans, parse_batch(response.data), strict=True)
            ]

        def run_individual_plans(
            plans_to_run: list[tuple[ModuleOutline, AtomPlan]],
        ) -> dict[str, Exception]:
            remaining = list(plans_to_run)
            failures: dict[str, Exception] = {}
            for _ in range(settings.max_api_attempts):
                failures = {}
                with ThreadPoolExecutor(
                    max_workers=settings.concurrency,
                    thread_name_prefix="9router-generator",
                ) as pool:
                    futures = {
                        pool.submit(run_atom, module, plan): plan.atom_id
                        for module, plan in remaining
                    }
                    for future in as_completed(futures):
                        atom_id = futures[future]
                        try:
                            completed_atom_id, content, snapshot = future.result()
                        except Exception as error:
                            failures[atom_id] = error
                        else:
                            generated[completed_atom_id] = (content, snapshot)
                if not failures:
                    break
                remaining = [
                    (module, plan)
                    for module, plan in remaining
                    if plan.atom_id in failures
                ]
            return failures

        generation_failures: dict[str, Exception]
        if curriculum is not None and curriculum.get("batch_atoms_by_module") is True:
            group_tasks: list[
                tuple[str, ModuleOutline, list[AtomPlan], int, int]
            ] = []
            for module in outline.modules:
                groups = _contiguous_module_atom_groups(
                    module,
                    settings.max_atoms_per_generation_call,
                )
                for group_index, plans in enumerate(groups):
                    group_key = sha256_json({
                        "module_id": module.module_id,
                        "group_index": group_index,
                        "group_count": len(groups),
                        "max_atoms_per_generation_call": (
                            settings.max_atoms_per_generation_call
                        ),
                        "atom_ids": [plan.atom_id for plan in plans],
                    })
                    group_tasks.append(
                        (group_key, module, plans, group_index, len(groups))
                    )

            def run_group(
                task_spec: tuple[str, ModuleOutline, list[AtomPlan], int, int],
            ) -> list[tuple[str, AtomContent, str]]:
                _group_key, module, plans, group_index, group_count = task_spec
                # A one-atom tail has no batch-size benefit. Reuse the normal
                # per-atom request/cache contract instead of wrapping it.
                if len(plans) == 1:
                    atom_id, content, snapshot = run_atom(module, plans[0])
                    return [(atom_id, content, snapshot)]
                return run_module_batch(
                    module,
                    plans,
                    group_index,
                    group_count,
                )

            if content_budget is not None:
                # Canary mode is deliberately sequential. READY cache hits are
                # traversed for free until the first cache miss reaches the
                # budgeted adapter. Whether that one response passes or fails,
                # stop here: never enter the normal second wave or atom-level
                # fallback path.
                for task_spec in group_tasks:
                    started_before = content_budget.started
                    try:
                        completed_items = run_group(task_spec)
                    except Exception as error:
                        if content_budget.started > started_before:
                            raise ContentGenerationStageFailed(
                                "Content generation canary failed after exactly "
                                f"{content_budget.started} upstream request(s); retries and "
                                f"fallback were skipped: {type(error).__name__}: {error}"
                            ) from error
                        raise
                    for atom_id, content, snapshot in completed_items:
                        generated[atom_id] = (content, snapshot)
                    if content_budget.exhausted:
                        raise ContentGenerationStagePaused(
                            requests_started=content_budget.started,
                            atoms_cached=len(generated),
                        )
                # Every group was already READY. Still stop before downstream
                # generation/review; a canary run is never a packaging run.
                raise ContentGenerationStagePaused(
                    requests_started=content_budget.started,
                    atoms_cached=len(generated),
                )
            else:
                remaining_groups = list(group_tasks)
                group_failures: dict[
                    str,
                    tuple[tuple[str, ModuleOutline, list[AtomPlan], int, int], Exception],
                ] = {}
                for _ in range(settings.max_api_attempts):
                    group_failures = {}
                    with ThreadPoolExecutor(
                        max_workers=settings.concurrency,
                        thread_name_prefix="9router-atom-group-generator",
                    ) as pool:
                        futures = {
                            pool.submit(run_group, task_spec): task_spec
                            for task_spec in remaining_groups
                        }
                        for future in as_completed(futures):
                            task_spec = futures[future]
                            group_key = task_spec[0]
                            try:
                                completed_items = future.result()
                            except Exception as error:
                                group_failures[group_key] = (task_spec, error)
                            else:
                                for atom_id, content, snapshot in completed_items:
                                    generated[atom_id] = (content, snapshot)
                    if not group_failures:
                        break
                    remaining_groups = [
                        task_spec
                        for task_spec in remaining_groups
                        if task_spec[0] in group_failures
                    ]
                # A long grouped response may be truncated or time out. Fall back
                # only for atoms in a failed multi-atom group; every successful
                # group is already durable in generation_steps and is never
                # regenerated. A one-atom group already used run_atom, so preserve
                # its final error instead of redundantly calling run_atom again.
                singleton_failures = {
                    task_spec[2][0].atom_id: error
                    for _group_key, (task_spec, error) in group_failures.items()
                    if len(task_spec[2]) == 1
                }
                fallback_plans = [
                    (module, plan)
                    for _group_key, module, plans, _group_index, _group_count in remaining_groups
                    if _group_key in group_failures
                    if len(plans) > 1
                    for plan in plans
                ]
                generation_failures = dict(singleton_failures)
                if fallback_plans:
                    generation_failures.update(run_individual_plans(fallback_plans))
        else:
            if content_budget is not None:
                for module, plan in all_plans:
                    started_before = content_budget.started
                    try:
                        atom_id, content, snapshot = run_atom(module, plan)
                    except Exception as error:
                        if content_budget.started > started_before:
                            raise ContentGenerationStageFailed(
                                "Content generation canary failed after exactly "
                                f"{content_budget.started} upstream request(s); retries and "
                                f"fallback were skipped: {type(error).__name__}: {error}"
                            ) from error
                        raise
                    generated[atom_id] = (content, snapshot)
                    if content_budget.exhausted:
                        raise ContentGenerationStagePaused(
                            requests_started=content_budget.started,
                            atoms_cached=len(generated),
                        )
                raise ContentGenerationStagePaused(
                    requests_started=content_budget.started,
                    atoms_cached=len(generated),
                )
            else:
                generation_failures = run_individual_plans(list(all_plans))
        if generation_failures:
            failed_ids = ", ".join(sorted(generation_failures))
            details = "; ".join(
                f"{atom_id}: {type(error).__name__}: {error}"
                for atom_id, error in sorted(generation_failures.items())
            )
            raise RuntimeError(
                f"Atom generation failed after retry wave: {failed_ids}; causes: {details}"
            )

        # The lesson writer is the final authority on learner-facing concept
        # wording. Synchronize before building any downstream graph/atom/full-
        # lesson objects so provisional outline or OCR-derived labels cannot
        # leak into the app.
        _synchronize_outline_with_generated_lessons(outline, generated)

        mindmap_snapshot: str | None = None
        if curriculum:
            mindmap = _deterministic_mindmap(outline, content_id_seed)
        else:
            mindmap_response = _cached_call(
                cache=cache,
                adapter=adapter,
                settings=settings,
                step_type="GENERATE_MINDMAP",
                input_hash=sha256_json(outline.model_dump(mode="json")),
                prompt_version=prompt_version,
                logical_model=model,
                instructions=system,
                input_text=(
                    "TASK: Build a mind map for this course outline. Use only IDs in the outline. "
                    "Use CONTAINS for hierarchy and PREREQUISITE only for declared prerequisites.\n"
                    + json.dumps(outline.model_dump(mode="json"), ensure_ascii=False)
                ),
                schema_name="mind_map",
                schema=responses_strict_schema(load_schema("mind-map.schema.json")),
                resume=resume,
            )
            mindmap = MindMap.model_validate(mindmap_response.data)
            mindmap_snapshot = mindmap_response.model_snapshot

        lessons = [generated[plan.atom_id][0].lesson for _, plan in all_plans]
        questions = [question for _, plan in all_plans for question in generated[plan.atom_id][0].quiz_bundle.questions]
        labs = [generated[plan.atom_id][0].mini_lab for _, plan in all_plans if generated[plan.atom_id][0].mini_lab]
        illustrations = _attach_curriculum_illustrations(
            corpus=corpus,
            curriculum=curriculum,
            workdir=workdir,
            outline=outline,
            lessons=lessons,
            content_id_seed=content_id_seed,
        ) if curriculum else []
        atoms = [
            KnowledgeAtom(
                atom_id=plan.atom_id,
                module_id=module.module_id,
                order_index=plan.order_index,
                title=plan.title,
                grounding_type=generated[plan.atom_id][0].lesson.grounding_type,
                prerequisite_ids=plan.prerequisite_ids,
                estimated_seconds=generated[plan.atom_id][0].lesson.estimated_seconds,
            )
            for module, plan in all_plans
        ]
        full_lessons = [
            FullLesson(
                full_lesson_id=stable_id("full", content_id_seed, module.module_id),
                module_id=module.module_id,
                order_index=module.order_index,
                title=module.title,
                objective=module.objective,
                atom_ids=[plan.atom_id for plan in module.atom_plans],
                estimated_minutes=max(5, round(sum(generated[plan.atom_id][0].lesson.estimated_seconds for plan in module.atom_plans) / 60)),
            )
            for module in outline.modules
        ]
        pairs: list[LearningPair] = []
        for _, plan in all_plans:
            content = generated[plan.atom_id][0]
            base = {
                "pair_id": content.lesson.pair_id,
                "atom_id": plan.atom_id,
                "micro_lesson_id": content.lesson.lesson_id,
                "question_ids": [item.question_id for item in content.quiz_bundle.questions],
                "revision": 1,
            }
            pairs.append(LearningPair(**base, content_hash=sha256_json(base), ai_review_status="PENDING"))

        used_source_ids = {
            source_ref_id
            for module in outline.modules
            for plan in module.atom_plans
            for source_ref_id in plan.source_ref_ids
        }
        for lesson in lessons:
            used_source_ids.update(lesson.source_ref_ids)
        for question in questions:
            used_source_ids.update(question.source_ref_ids)
        for lab in labs:
            used_source_ids.update(lab.source_ref_ids)
        used_source_ids.update(item.source_ref_id for item in illustrations)

        snapshots = sorted({
            *outline_snapshots,
            *(value[1] for value in generated.values()),
            *([mindmap_snapshot] if mindmap_snapshot else []),
        })
        pack = PackData(
            course=outline.course,
            modules=outline.modules,
            full_lessons=full_lessons,
            atoms=atoms,
            lessons=lessons,
            learning_pairs=pairs,
            questions=questions,
            mindmaps=[mindmap],
            mini_labs=labs,
            illustrations=illustrations,
            source_refs=[
                chunk.source_ref
                for chunk in corpus.chunks
                if chunk.source_ref.source_ref_id in used_source_ids
            ],
            source_excerpts=_build_source_excerpts(corpus, used_source_ids),
            source_hash=corpus.source_hash,
            generator_model_snapshot=",".join(snapshots),
            generator_prompt_version=prompt_version,
        )
        write_json(workdir / "candidate.json", pack.model_dump(mode="json", by_alias=True))
        return pack
    finally:
        cache.close()
