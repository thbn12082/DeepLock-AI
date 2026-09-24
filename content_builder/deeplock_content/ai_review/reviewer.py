from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..cache import BuildCache
from ..contracts import PackContract, pack_contract_sha256, resolve_pack_contract
from ..editorial import is_course_v2_prompt_version, validate_lesson_editorial
from ..generation.api import ModelResponse, ResponsesAdapter
from ..generation.adapters import build_adapter
from ..generation.builder import _cached_call, _normalize_takeaway_spacing, _validate_takeaway_editorial
from ..models import AIReviewBatch, AtomContent, MindMap, PackData, QuizBundle
from ..models import _require_supported_model
from ..quiz_order import rebalance_question_options, validate_quiz_editorial
from ..settings import Settings
from ..util import PROMPT_ROOT, load_schema, responses_strict_schema, sha256_bytes, sha256_json
from ..validators import validate_pack


REVIEW_SCHEMA_VERSION = "1"
REVIEW_OUTPUT_VERBOSITY = "low"


def review_schema_sha256() -> str:
    return sha256_json(load_schema("ai-review-result.schema.json"))


def review_prompt_sha256(prompt_version: str) -> str:
    return sha256_bytes((PROMPT_ROOT / prompt_version / "system.txt").read_bytes())


def _review_cache_key(
    candidate_hash: str,
    source_hash: str,
    prompt_version: str,
    model: str,
    configured_model: str,
    reasoning_effort: str,
) -> str:
    return sha256_json([
        candidate_hash,
        source_hash,
        prompt_version,
        review_prompt_sha256(prompt_version),
        review_schema_sha256(),
        model,
        configured_model,
        reasoning_effort,
        f"output-verbosity:{REVIEW_OUTPUT_VERBOSITY}",
        "review-schema-2",
    ])


def _redact_question(question: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(question)
    result.pop("correct_option_id", None)
    return result


def _source_payload(pack: PackData, source_ids: set[str]) -> dict[str, Any]:
    refs = [item.model_dump(mode="json") for item in pack.source_refs if item.source_ref_id in source_ids]
    excerpts = [item.model_dump(mode="json") for item in pack.source_excerpts if item.source_ref_id in source_ids]
    return {"source_refs": refs, "source_excerpts": excerpts}


def _item(item_type: str, item_id: str, revision: int, value: Any, source_hash: str) -> dict[str, Any]:
    raw = value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value
    # AI_APPROVED is a builder-owned materialized status.  It is never part of
    # the candidate that the model reviews, including a resumed re-review.
    if item_type in {"LEARNING_PAIR", "MINI_LAB"}:
        raw = deepcopy(raw)
        raw["ai_review_status"] = "PENDING"
    candidate_hash = sha256_json(raw)
    visible = _redact_question(raw) if item_type == "QUESTION" else raw
    return {
        "item_type": item_type,
        "item_id": item_id,
        "item_revision": revision,
        "candidate_hash": candidate_hash,
        "source_hash": source_hash,
        "candidate_content": visible,
    }


def _pair_batch(pack: PackData, pair: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    lesson = next(item for item in pack.lessons if item.lesson_id == pair.micro_lesson_id)
    questions = [item for item in pack.questions if item.question_id in set(pair.question_ids)]
    lab = next((item for item in pack.mini_labs if item.atom_id == pair.atom_id), None)
    source_ids = set(lesson.source_ref_ids)
    for question in questions:
        source_ids.update(question.source_ref_ids)
    if lab:
        source_ids.update(lab.source_ref_ids)
    sources = _source_payload(pack, source_ids)
    source_hash = sha256_json(sources)
    items = [_item("LESSON", lesson.lesson_id, pair.revision, lesson, source_hash)]
    items.extend(_item("QUESTION", question.question_id, pair.revision, question, source_hash) for question in questions)
    pair_item = _item("LEARNING_PAIR", pair.pair_id, pair.revision, pair, source_hash)
    # LearningPair is a structural bundle: sources live on its lesson,
    # questions and optional lab rather than as a duplicate field on the pair.
    # Expose that ownership contract to the reviewer without changing the hash
    # of the actual persisted LearningPair object.
    pair_item["candidate_content"] = {
        "review_scope": "LEARNING_PAIR_BUNDLE_SOURCE_OWNERSHIP_V2",
        "learning_pair": pair.model_dump(mode="json", by_alias=True),
        "bundle_owned_source_ref_ids": sorted(source_ids),
        "source_ownership_note": (
            "These source IDs are owned transitively by this pair through its lesson, questions, "
            "and optional Mini Lab. The LearningPair schema intentionally does not duplicate a "
            "source_ref_ids field. Use these IDs as this item's own approval evidence."
        ),
    }
    items.append(pair_item)
    if lab:
        items.append(_item("MINI_LAB", lab.mini_lab_id, pair.revision, lab, source_hash))
    correct_answers = {question.question_id: question.correct_option_id for question in questions}
    expected = {
        item["item_id"]: {
            **item,
            "owned_evidence_ref_ids": sorted(_owned_evidence_ref_ids(pack, item["item_type"], item["item_id"])),
            "expected_correct_option_id": correct_answers.get(item["item_id"]),
        }
        for item in items
    }
    payload = {
        "task": (
            "Review every candidate item independently. Echo item metadata exactly. For a "
            "LearningPair, source ownership is bundle-level through its lesson/questions/optional "
            "Mini Lab; bundle_owned_source_ref_ids are the pair's own valid evidence IDs."
        ),
        "source_context": sources,
        "candidate_items": items,
    }
    return pair.pair_id, payload, expected


def _module_mindmap_projection(pack: PackData, module: Any, atoms: list[Any]) -> list[dict[str, Any]]:
    """Project the global map onto one module without implying that other modules are absent."""

    atom_ids = {item.atom_id for item in atoms}
    local_node_ids = {pack.course.course_id, module.module_id, *atom_ids}
    projections: list[dict[str, Any]] = []
    for mindmap in pack.mindmaps:
        projections.append({
            "mind_map_id": mindmap.mind_map_id,
            "root_node_id": mindmap.root_node_id,
            "nodes": [
                node.model_dump(mode="json", by_alias=True)
                for node in mindmap.nodes
                if node.id in local_node_ids
            ],
            "contains_edges": [
                edge.model_dump(mode="json", by_alias=True)
                for edge in mindmap.edges
                if edge.type == "CONTAINS"
                and edge.from_ in local_node_ids
                and edge.to in local_node_ids
            ],
            # Incoming cross-module prerequisites remain visible as ID-only
            # structural facts. Their owner module and source are intentionally
            # outside this module-scoped review batch.
            "prerequisite_edges_into_module": [
                edge.model_dump(mode="json", by_alias=True)
                for edge in mindmap.edges
                if edge.type == "PREREQUISITE" and edge.to in atom_ids
            ],
        })
    return projections


def _module_batch(pack: PackData, module: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    atoms = [item for item in pack.atoms if item.module_id == module.module_id]
    atom_ids = {item.atom_id for item in atoms}
    candidate = {
        "review_scope": "MODULE_STRUCTURE_AND_PROJECTED_MINDMAP_ONLY_V3",
        "module": module.model_dump(mode="json"),
        "atoms": [item.model_dump(mode="json") for item in atoms],
        "mindmap_projection": _module_mindmap_projection(pack, module, atoms),
    }
    source_ids = {
        source_id
        for lesson in pack.lessons
        if lesson.atom_id in atom_ids
        for source_id in lesson.source_ref_ids
    }
    sources = _source_payload(pack, source_ids)
    source_hash = sha256_json(sources)
    item = _item("MODULE", module.module_id, 1, candidate, source_hash)
    return f"module:{module.module_id}", {
        "task": (
            "Review only this target module's atom order, prerequisite-ID graph, and the consistency "
            "of its explicitly module-scoped mind-map projection. The global course map necessarily "
            "contains other modules; they are intentionally omitted from this projection, so never "
            "report omitted/unrelated course nodes or edges as missing, extraneous, or ungrounded. "
            "An incoming prerequisite may name an external atom solely as a structural endpoint; do "
            "not demand its source in this batch. Do not request lesson/source-detail coverage or "
            "changes to builder-owned module and atom titles/objectives; lesson completeness is "
            "reviewed independently in LearningPair batches."
        ),
        "source_context": sources,
        "candidate_items": [item],
    }, {
        module.module_id: {
            **item,
            "owned_evidence_ref_ids": sorted(_owned_evidence_ref_ids(pack, "MODULE", module.module_id)),
            "expected_correct_option_id": None,
        },
    }


def _review_batches(
    pack: PackData,
    *,
    combine_by_module: bool = False,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    batches = [_pair_batch(pack, pair) for pair in pack.learning_pairs]
    batches.extend(_module_batch(pack, module) for module in pack.modules)
    if not combine_by_module:
        return batches

    pair_by_atom = {pair.atom_id: pair for pair in pack.learning_pairs}
    pair_batches = {
        batch_id: (payload, expected)
        for batch_id, payload, expected in batches
        if not batch_id.startswith("module:")
    }
    module_batches = {
        batch_id.removeprefix("module:"): (payload, expected)
        for batch_id, payload, expected in batches
        if batch_id.startswith("module:")
    }
    combined: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    consumed_pairs: set[str] = set()
    for module in pack.modules:
        payloads: list[dict[str, Any]] = []
        expected: dict[str, Any] = {}
        for plan in module.atom_plans:
            pair = pair_by_atom.get(plan.atom_id)
            if pair is None:
                continue
            pair_payload, pair_expected = pair_batches[pair.pair_id]
            payloads.append(pair_payload)
            expected.update(pair_expected)
            consumed_pairs.add(pair.pair_id)
        module_payload, module_expected = module_batches[module.module_id]
        payloads.append(module_payload)
        expected.update(module_expected)
        source_ids = {
            source_ref_id
            for item in expected.values()
            for source_ref_id in item["owned_evidence_ref_ids"]
        }
        combined.append((
            f"module-full:{module.module_id}",
            {
                "task": (
                    "Review every candidate independently. This request batches all LearningPairs and the "
                    "structure projection of one lecture part solely to reduce API overhead. Never borrow "
                    "evidence between items; each item may cite only its own source IDs."
                ),
                "source_context": _source_payload(pack, source_ids),
                "candidate_items": [
                    item
                    for payload in payloads
                    for item in payload["candidate_items"]
                ],
            },
            expected,
        ))
    combined.extend(
        (pair_id, payload, expected)
        for pair_id, (payload, expected) in pair_batches.items()
        if pair_id not in consumed_pairs
    )
    return combined


def _owned_evidence_ref_ids(pack: PackData, item_type: str, item_id: str) -> set[str]:
    if item_type == "LESSON":
        return set(next(item for item in pack.lessons if item.lesson_id == item_id).source_ref_ids)
    if item_type == "QUESTION":
        return set(next(item for item in pack.questions if item.question_id == item_id).source_ref_ids)
    if item_type == "MINI_LAB":
        return set(next(item for item in pack.mini_labs if item.mini_lab_id == item_id).source_ref_ids)
    if item_type == "LEARNING_PAIR":
        pair = next(item for item in pack.learning_pairs if item.pair_id == item_id)
        lesson = next(item for item in pack.lessons if item.lesson_id == pair.micro_lesson_id)
        owned = set(lesson.source_ref_ids)
        question_ids = set(pair.question_ids)
        for question in pack.questions:
            if question.question_id in question_ids:
                owned.update(question.source_ref_ids)
        lab = next((item for item in pack.mini_labs if item.atom_id == pair.atom_id), None)
        if lab:
            owned.update(lab.source_ref_ids)
        return owned
    if item_type == "MODULE":
        atom_ids = {item.atom_id for item in pack.atoms if item.module_id == item_id}
        return {
            source_ref_id
            for lesson in pack.lessons
            if lesson.atom_id in atom_ids
            for source_ref_id in lesson.source_ref_ids
        }
    raise PermissionError(f"AI_REVIEW_REQUIRED: unsupported review item type {item_type}")


def expected_review_items(pack: PackData) -> dict[str, dict[str, Any]]:
    """Rebuild the exact item/hash/source contract used by fresh review calls."""

    expected: dict[str, dict[str, Any]] = {}
    for _, _, batch_items in _review_batches(pack):
        for item_id, item in batch_items.items():
            if item_id in expected:
                raise PermissionError(f"AI_REVIEW_REQUIRED: duplicate global review item ID {item_id}")
            expected[item_id] = {
                **item,
                "owned_evidence_ref_ids": sorted(_owned_evidence_ref_ids(pack, item["item_type"], item_id)),
            }
    return expected


def _review_result_metadata_matches(
    result: Any,
    expected: dict[str, Any],
) -> bool:
    """Match the immutable metadata contract without repairing reviewer echoes."""

    return (
        result.item_type == expected["item_type"]
        and result.item_revision == expected["item_revision"]
        and result.candidate_hash == expected["candidate_hash"]
        and result.source_hash == expected["source_hash"]
    )


def _require_exact_review_result_coverage(
    pack: PackData,
    results: list[Any],
) -> None:
    """Fail closed unless one exact, current result exists for every review item."""

    expected = expected_review_items(pack)
    actual_ids = [result.item_id for result in results]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise PermissionError("AI_REVIEW_REQUIRED: merged reviewer item coverage mismatch")
    for result in results:
        if not _review_result_metadata_matches(result, expected[result.item_id]):
            raise PermissionError(
                f"AI_REVIEW_REQUIRED: stale/tampered metadata for {result.item_id}"
            )


def _validate_review_response(
    *,
    batch_id: str,
    data: dict[str, Any],
    expected: dict[str, dict[str, Any]],
) -> AIReviewBatch:
    try:
        review = AIReviewBatch.model_validate(data)
    except ValidationError as error:
        raise PermissionError(f"AI_REVIEW_REQUIRED: reviewer schema invalid in {batch_id}: {error}") from error
    actual_ids = [item.item_id for item in review.reviews]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise PermissionError(f"AI_REVIEW_REQUIRED: reviewer item coverage mismatch in {batch_id}")
    for result in review.reviews:
        item = expected[result.item_id]
        if not _review_result_metadata_matches(result, item):
            raise PermissionError(f"AI_REVIEW_REQUIRED: stale/tampered metadata for {result.item_id}")
        owned_evidence = set(item["owned_evidence_ref_ids"])
        if (
            result.item_type == "LEARNING_PAIR"
            and result.verdict != "APPROVE"
            and owned_evidence
            and any(
                "SOURCE" in code.upper() and ("MISSING" in code.upper() or "NO_OWN" in code.upper())
                for code in result.issue_codes
            )
        ):
            raise PermissionError(
                f"AI_REVIEW_REQUIRED: LearningPair {result.item_id} has bundle-owned source evidence; "
                "do not require a duplicate source_ref_ids field on the pair"
            )
        if (
            result.item_type == "MODULE"
            and result.verdict == "REPAIR"
            and (
                not result.issue_codes
                or any(
                    not code.upper().startswith("MINDMAP_")
                    for code in result.issue_codes
                )
            )
        ):
            raise PermissionError(
                f"AI_REVIEW_REQUIRED: MODULE {result.item_id} may request repair only for "
                "a projected MINDMAP_* consistency defect; lesson prose, page-label, mixed-objective, "
                "atom-order, and prerequisite findings are outside the mind-map repair schema"
            )
        if result.verdict == "APPROVE":
            evidence = result.evidence_ref_ids
            if result.issue_codes or not result.evidence_sufficient:
                raise PermissionError(f"AI_REVIEW_REQUIRED: invalid approval flags for {result.item_id}")
            if not evidence or len(evidence) != len(set(evidence)) or not set(evidence) <= owned_evidence:
                raise PermissionError(f"AI_REVIEW_REQUIRED: invalid approval evidence for {result.item_id}")
            expected_answer = item["expected_correct_option_id"]
            if result.item_type == "QUESTION":
                if result.independent_correct_option_id != expected_answer:
                    raise PermissionError(f"AI_REVIEW_REQUIRED: ANSWER_DISAGREEMENT for {result.item_id}")
            elif result.independent_correct_option_id is not None:
                raise PermissionError(f"AI_REVIEW_REQUIRED: UNEXPECTED_ANSWER for {result.item_id}")
    return review


def _call_review(
    *,
    batch_id: str,
    payload: dict[str, Any],
    expected: dict[str, dict[str, Any]],
    cache: BuildCache,
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    prompt_version: str,
    resume: bool,
) -> tuple[str, AIReviewBatch, str, str]:
    aggregate_candidate_hash = sha256_json([item["candidate_hash"] for item in expected.values()])
    source_hash = sha256_json(sorted({item["source_hash"] for item in expected.values()}))
    key = _review_cache_key(
        aggregate_candidate_hash,
        source_hash,
        prompt_version,
        model,
        settings.transport_model(model, reviewer=True),
        settings.review_reasoning_effort,
    )
    last_error: PermissionError | None = None
    cached = cache.get("ai_reviews", key) if resume else None
    if cached:
        response = ModelResponse(
            data=cached["output"],
            response_id=cached.get("response_id") or "cache",
            model_snapshot=cached.get("model_snapshot") or model,
        )
        try:
            review = _validate_review_response(batch_id=batch_id, data=response.data, expected=expected)
        except PermissionError as error:
            last_error = error
            cache.record_attempt(
                "ai_reviews",
                {
                    "cache_key": key,
                    "candidate_hash": aggregate_candidate_hash,
                    "source_hash": source_hash,
                    "prompt_version": prompt_version,
                    "logical_model": model,
                    "model_snapshot": response.model_snapshot,
                    "schema_version": "1",
                    "status": "FAILED",
                    "attempt_count": cached.get("attempt_count") or 1,
                    "response_id": response.response_id,
                    "output_json": response.data,
                },
            )
        else:
            return batch_id, review, response.model_snapshot, aggregate_candidate_hash

    semantic_attempts = max(2, min(settings.max_api_attempts, 3))
    for attempt in range(1, semantic_attempts + 1):
        review_payload = payload
        if last_error is not None:
            review_payload = {
                **payload,
                "retry_feedback": (
                    f"The previous response failed the deterministic review gate: {last_error}. "
                    "Return a fresh result. Echo every item metadata field exactly. For APPROVE, "
                    "evidence_ref_ids must be non-empty, unique, and limited to that item's own "
                    "source_ref_ids; do not borrow another item or sibling question's source ID. "
                    "For a MODULE, REPAIR is valid only for a bounded projected MINDMAP_* "
                    "consistency defect; do not apply lesson-level issue codes to a module."
                ),
            }
        response = adapter.structured(
            logical_model=model,
            reviewer=True,
            reasoning_effort=settings.review_reasoning_effort,
            instructions=(PROMPT_ROOT / prompt_version / "system.txt").read_text(encoding="utf-8"),
            input_text=json.dumps(review_payload, ensure_ascii=False, sort_keys=True),
            schema_name="content_review_result",
            schema=responses_strict_schema(load_schema("ai-review-result.schema.json")),
            output_verbosity=REVIEW_OUTPUT_VERBOSITY,
        )
        try:
            review = _validate_review_response(batch_id=batch_id, data=response.data, expected=expected)
        except PermissionError as error:
            last_error = error
            cache.record_attempt(
                "ai_reviews",
                {
                    "cache_key": key,
                    "candidate_hash": aggregate_candidate_hash,
                    "source_hash": source_hash,
                    "prompt_version": prompt_version,
                    "logical_model": model,
                    "model_snapshot": response.model_snapshot,
                    "schema_version": "1",
                    "status": "FAILED",
                    "attempt_count": attempt,
                    "response_id": response.response_id,
                    "output_json": response.data,
                },
            )
            continue
        cache.record_attempt(
            "ai_reviews",
            {
                "cache_key": key,
                "candidate_hash": aggregate_candidate_hash,
                "source_hash": source_hash,
                "prompt_version": prompt_version,
                "logical_model": model,
                "model_snapshot": response.model_snapshot,
                "schema_version": "1",
                "status": "READY",
                "attempt_count": attempt,
                "response_id": response.response_id,
                "output_json": response.data,
            },
        )
        return batch_id, review, response.model_snapshot, aggregate_candidate_hash
    assert last_error is not None
    raise last_error


def _verify_results(pack: PackData, results: list[Any]) -> list[str]:
    _require_exact_review_result_coverage(pack, results)
    errors: list[str] = []
    questions = {item.question_id: item for item in pack.questions}
    expected_items = expected_review_items(pack)
    for result in results:
        if result.verdict != "APPROVE":
            errors.append(f"{result.item_id}:{result.verdict}:{','.join(result.issue_codes)}")
            continue
        if result.issue_codes:
            errors.append(f"{result.item_id}:APPROVE_WITH_ISSUES")
        if not result.evidence_sufficient:
            errors.append(f"{result.item_id}:INSUFFICIENT_EVIDENCE")
        evidence = result.evidence_ref_ids
        owned_evidence = set(expected_items[result.item_id]["owned_evidence_ref_ids"])
        if not evidence:
            errors.append(f"{result.item_id}:EMPTY_EVIDENCE")
        elif len(evidence) != len(set(evidence)) or not set(evidence) <= owned_evidence:
            errors.append(f"{result.item_id}:UNOWNED_EVIDENCE")
        if result.item_type == "QUESTION":
            expected = questions[result.item_id].correct_option_id
            if result.independent_correct_option_id != expected:
                errors.append(f"{result.item_id}:ANSWER_DISAGREEMENT")
        elif result.independent_correct_option_id is not None:
            errors.append(f"{result.item_id}:UNEXPECTED_ANSWER")
    return errors


def _execute_review_batches(
    *,
    batches: list[tuple[str, dict[str, Any], dict[str, dict[str, Any]]]],
    cache: BuildCache,
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    prompt_version: str,
    resume: bool,
) -> list[tuple[str, AIReviewBatch, str, str]]:
    completed: list[tuple[str, AIReviewBatch, str, str]] = []
    with ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix="9router-reviewer") as pool:
        futures = [
            pool.submit(
                _call_review,
                batch_id=batch_id,
                payload=payload,
                expected=expected,
                cache=cache,
                adapter=adapter,
                settings=settings,
                model=model,
                prompt_version=prompt_version,
                resume=resume,
            )
            for batch_id, payload, expected in batches
        ]
        for future in as_completed(futures):
            completed.append(future.result())
    return completed


def _review_round(
    *,
    pack: PackData,
    cache: BuildCache,
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    prompt_version: str,
    resume: bool,
) -> list[tuple[str, AIReviewBatch, str, str]]:
    batches = _review_batches(
        pack,
        combine_by_module=getattr(settings, "review_batch_size", 1) > 1,
    )
    return _execute_review_batches(
        batches=batches,
        cache=cache,
        adapter=adapter,
        settings=settings,
        model=model,
        prompt_version=prompt_version,
        resume=resume,
    )


def _review_changed_items(
    *,
    pack: PackData,
    previous_results: list[Any],
    cache: BuildCache,
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    prompt_version: str,
    resume: bool,
) -> tuple[list[tuple[str, AIReviewBatch, str, str]], list[Any]]:
    """Retain exact clean approvals and review only affected uncombined batches.

    A repair can invalidate one LearningPair inside a large module-combined
    request. Replaying that whole combined request is both expensive and more
    likely to introduce an unrelated metadata echo error. Results are reusable
    only when they are clean approvals whose four immutable metadata fields
    still match the repaired candidate exactly. Every missing item then selects
    its normal, uncombined pair/module batch; the fresh batch replaces all of
    its old items before the merged full-coverage contract is checked.
    """

    prior_ids = [result.item_id for result in previous_results]
    if len(prior_ids) != len(set(prior_ids)):
        raise PermissionError("AI_REVIEW_REQUIRED: duplicate prior reviewer item ID")

    expected = expected_review_items(pack)
    retained = {
        result.item_id: result
        for result in previous_results
        if result.item_id in expected
        and result.verdict == "APPROVE"
        and _review_result_metadata_matches(result, expected[result.item_id])
    }
    missing_ids = set(expected) - set(retained)

    selected_batches: list[tuple[str, dict[str, Any], dict[str, dict[str, Any]]]] = []
    selected_item_ids: set[str] = set()
    for batch in _review_batches(pack, combine_by_module=False):
        batch_item_ids = set(batch[2])
        if batch_item_ids & missing_ids:
            selected_batches.append(batch)
            selected_item_ids.update(batch_item_ids)
    unresolved = missing_ids - selected_item_ids
    if unresolved:
        raise PermissionError(
            "AI_REVIEW_REQUIRED: no review batch owns changed items "
            + ",".join(sorted(unresolved))
        )

    completed = _execute_review_batches(
        batches=selected_batches,
        cache=cache,
        adapter=adapter,
        settings=settings,
        model=model,
        prompt_version=prompt_version,
        resume=resume,
    ) if selected_batches else []
    fresh_results = [
        result
        for _, batch, _, _ in completed
        for result in batch.reviews
    ]
    merged = [
        result
        for item_id, result in retained.items()
        if item_id not in selected_item_ids
    ]
    merged.extend(fresh_results)
    _require_exact_review_result_coverage(pack, merged)
    return completed, merged


def _lesson_field_is_editorially_valid(lesson: Any, field_name: str) -> bool:
    """Return whether one course-v2 lesson field passes its deterministic gates."""

    return not any(
        issue.path.endswith(f".{field_name}")
        for issue in validate_lesson_editorial(lesson, pedagogical=True)
    )


def _normalize_repaired_pair_content(
    repaired: AtomContent,
    *,
    original_lesson: Any,
    original_questions: list[Any],
    original_lab: Any | None,
    pedagogical: bool,
) -> AtomContent:
    """Restore builder-owned identity and source ownership before quality gates.

    Repair is allowed to rewrite instructional prose and quiz semantics. Stable
    IDs, prerequisites, source boundaries, and illustration ownership belong to
    the frozen candidate graph, so accepting model drift in those fields would
    make a valid textual repair fail for the wrong reason (or silently escape
    its assigned evidence). Keep a newly written course-v2 objective only when
    it passes the same deterministic objective gates as normal generation.
    """

    if len(repaired.quiz_bundle.questions) != len(original_questions):
        raise ValueError(
            f"Repair changed question membership for {original_lesson.pair_id}"
        )
    if (original_lab is None) != (repaired.mini_lab is None):
        raise ValueError(
            f"Repair changed Mini Lab membership for {original_lesson.pair_id}"
        )

    repaired.lesson.lesson_id = original_lesson.lesson_id
    repaired.lesson.pair_id = original_lesson.pair_id
    repaired.lesson.atom_id = original_lesson.atom_id
    repaired.lesson.grounding_type = original_lesson.grounding_type
    repaired.lesson.prerequisite_ids = list(original_lesson.prerequisite_ids)
    repaired.lesson.source_ref_ids = list(original_lesson.source_ref_ids)
    repaired.lesson.illustration_source_ref_ids = list(
        original_lesson.illustration_source_ref_ids
    )
    if (
        pedagogical
        and not _lesson_field_is_editorially_valid(repaired.lesson, "learning_objective")
        and _lesson_field_is_editorially_valid(original_lesson, "learning_objective")
    ):
        repaired.lesson.learning_objective = original_lesson.learning_objective
    # A repair sometimes compresses the mental model into a label while fixing
    # a different review issue.  Retain the already-valid grounded intuition in
    # that case; this does not waive any whole-lesson, source, or review gate.
    if (
        pedagogical
        and not _lesson_field_is_editorially_valid(repaired.lesson, "intuition")
        and _lesson_field_is_editorially_valid(original_lesson, "intuition")
    ):
        repaired.lesson.intuition = original_lesson.intuition

    for repaired_question, original_question in zip(
        repaired.quiz_bundle.questions,
        original_questions,
        strict=True,
    ):
        repaired_question.question_id = original_question.question_id
        repaired_question.pair_id = original_question.pair_id
        repaired_question.atom_id = original_question.atom_id
        repaired_question.grounding_type = original_question.grounding_type
        repaired_question.source_ref_ids = list(original_question.source_ref_ids)

    if original_lab is not None and repaired.mini_lab is not None:
        repaired.mini_lab.mini_lab_id = original_lab.mini_lab_id
        repaired.mini_lab.atom_id = original_lab.atom_id
        repaired.mini_lab.source_ref_ids = list(original_lab.source_ref_ids)

    return repaired


def _repair_pair_call(
    pack: PackData,
    pair_id: str,
    issues: list[dict[str, Any]],
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    cache: BuildCache,
    resume: bool,
) -> tuple[str, AtomContent, str]:
    pair = next(item for item in pack.learning_pairs if item.pair_id == pair_id)
    lesson = next(item for item in pack.lessons if item.lesson_id == pair.micro_lesson_id)
    questions = [next(item for item in pack.questions if item.question_id == question_id) for question_id in pair.question_ids]
    lab = next((item for item in pack.mini_labs if item.atom_id == pair.atom_id), None)
    source_ids = set(lesson.source_ref_ids)
    for question in questions:
        source_ids.update(question.source_ref_ids)
    if lab:
        source_ids.update(lab.source_ref_ids)
    candidate = {
        "lesson": lesson.model_dump(mode="json"),
        "quiz_bundle": QuizBundle(questions=questions, insufficient_evidence_items=[]).model_dump(mode="json"),
        "mini_lab": lab.model_dump(mode="json") if lab else None,
    }
    repair_prompt_version = settings.generator_prompt_version
    instructions = (PROMPT_ROOT / repair_prompt_version / "repair-system.txt").read_text(
        encoding="utf-8"
    )
    input_text = json.dumps({
            "task": "Repair this LearningPair bundle. Preserve every stable ID and source boundary.",
            "next_revision": pair.revision + 1,
            "review_issues": issues,
            "source_context": _source_payload(pack, source_ids),
            "candidate": candidate,
        }, ensure_ascii=False, sort_keys=True)
    schema = responses_strict_schema(load_schema("atom-content.schema.json"))
    pedagogical = is_course_v2_prompt_version(pack.generator_prompt_version)

    def validate_repair(data: dict[str, Any]) -> None:
        value = AtomContent.model_validate(data)
        _normalize_repaired_pair_content(
            value,
            original_lesson=lesson,
            original_questions=questions,
            original_lab=lab,
            pedagogical=pedagogical,
        )
        value.lesson.takeaways = _normalize_takeaway_spacing(value.lesson.takeaways)
        _validate_takeaway_editorial(value.lesson.takeaways)
        editorial_issues = validate_lesson_editorial(
            value.lesson,
            pedagogical=pedagogical,
        )
        if editorial_issues:
            codes = ", ".join(sorted({issue.code for issue in editorial_issues}))
            raise ValueError(f"Repair still fails editorial quality for {pair_id}: {codes}")
        rebalance_question_options(value.quiz_bundle.questions, pair.atom_id)
        validate_quiz_editorial(value.quiz_bundle.questions)
        data.clear()
        data.update(value.model_dump(mode="json", by_alias=True))

    response = _cached_call(
        cache=cache,
        adapter=adapter,
        settings=settings,
        step_type="REPAIR_PAIR_CONTENT",
        input_hash=sha256_json({"pair_id": pair_id, "payload": input_text}),
        prompt_version=f"{repair_prompt_version}-repair",
        logical_model=model,
        instructions=instructions,
        input_text=input_text,
        schema_name="repaired_atom_content",
        schema=schema,
        resume=resume,
        output_validator=validate_repair,
    )
    repaired = AtomContent.model_validate(response.data)
    _normalize_repaired_pair_content(
        repaired,
        original_lesson=lesson,
        original_questions=questions,
        original_lab=lab,
        pedagogical=pedagogical,
    )
    editorial_issues = validate_lesson_editorial(
        repaired.lesson,
        pedagogical=pedagogical,
    )
    if editorial_issues:
        codes = ", ".join(sorted({issue.code for issue in editorial_issues}))
        raise ValueError(f"Repaired lesson fails editorial quality for {pair_id}: {codes}")
    rebalance_question_options(repaired.quiz_bundle.questions, pair.atom_id)
    validate_quiz_editorial(repaired.quiz_bundle.questions)
    return pair_id, repaired, response.model_snapshot


def _apply_pair_repair(pack: PackData, pair_id: str, repaired: AtomContent) -> PackData:
    updated = pack.model_copy(deep=True)
    pair = next(item for item in updated.learning_pairs if item.pair_id == pair_id)
    updated.lessons = [repaired.lesson if item.lesson_id == pair.micro_lesson_id else item for item in updated.lessons]
    repaired_questions = {item.question_id: item for item in repaired.quiz_bundle.questions}
    updated.questions = [repaired_questions.get(item.question_id, item) for item in updated.questions]
    if repaired.mini_lab:
        repaired.mini_lab.ai_review_status = "PENDING"
        updated.mini_labs = [repaired.mini_lab if item.atom_id == pair.atom_id else item for item in updated.mini_labs]
    pair.revision += 1
    pair.ai_review_status = "PENDING"
    hash_basis = {
        "pair_id": pair.pair_id,
        "atom_id": pair.atom_id,
        "micro_lesson_id": pair.micro_lesson_id,
        "question_ids": pair.question_ids,
        "revision": pair.revision,
    }
    pair.content_hash = sha256_json(hash_basis)
    return updated


def _repair_mindmap_call(
    pack: PackData,
    issues: list[dict[str, Any]],
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    cache: BuildCache,
    resume: bool,
) -> tuple[MindMap, str]:
    known_source_ids = {item.source_ref_id for item in pack.source_refs}
    issue_source_ids = {
        source_ref_id
        for issue in issues
        for source_ref_id in issue.get("evidence_ref_ids", [])
        if source_ref_id in known_source_ids
    }
    original = pack.mindmaps[0]
    instructions = (PROMPT_ROOT / "course-v1" / "repair-system.txt").read_text(encoding="utf-8")
    input_text = json.dumps({
            "task": "Repair only the mind map consistency. Preserve mind_map_id, root and all course/module/atom IDs.",
            "review_issues": issues,
            "source_context": _source_payload(pack, issue_source_ids),
            "modules": [item.model_dump(mode="json") for item in pack.modules],
            "atoms": [item.model_dump(mode="json") for item in pack.atoms],
            "candidate": original.model_dump(mode="json", by_alias=True),
        }, ensure_ascii=False, sort_keys=True)

    def validate_repair(data: dict[str, Any]) -> None:
        value = MindMap.model_validate(data)
        if value.mind_map_id != original.mind_map_id or value.root_node_id != original.root_node_id:
            raise ValueError("Repair changed stable mind map IDs")
        original_nodes = {
            node.id: (node.type, node.atom_id)
            for node in original.nodes
        }
        repaired_nodes = {
            node.id: (node.type, node.atom_id)
            for node in value.nodes
        }
        if repaired_nodes != original_nodes or len(value.nodes) != len(original.nodes):
            raise ValueError("Repair must preserve every course/module/atom mind map node")
        original_structure = {
            (edge.from_, edge.to, edge.type)
            for edge in original.edges
            if edge.type in {"CONTAINS", "PREREQUISITE"}
        }
        repaired_structure = {
            (edge.from_, edge.to, edge.type)
            for edge in value.edges
            if edge.type in {"CONTAINS", "PREREQUISITE"}
        }
        if repaired_structure != original_structure:
            raise ValueError("Repair must preserve builder-owned contains/prerequisite edges")

    response = _cached_call(
        cache=cache,
        adapter=adapter,
        settings=settings,
        step_type="REPAIR_MIND_MAP",
        input_hash=sha256_json({"mind_map_id": original.mind_map_id, "payload": input_text}),
        prompt_version="course-v1-repair",
        logical_model=model,
        instructions=instructions,
        input_text=input_text,
        schema_name="repaired_mind_map",
        schema=responses_strict_schema(load_schema("mind-map.schema.json")),
        resume=resume,
        output_validator=validate_repair,
    )
    repaired = MindMap.model_validate(response.data)
    return repaired, response.model_snapshot


def _repair_round(
    pack: PackData,
    repair_results: list[Any],
    adapter: ResponsesAdapter,
    settings: Settings,
    model: str,
    cache: BuildCache,
    resume: bool,
    contract: PackContract | None = None,
    require_frozen_outline: bool = False,
) -> tuple[PackData, list[str]]:
    question_to_pair = {item.question_id: item.pair_id for item in pack.questions}
    lesson_to_pair = {item.lesson_id: item.pair_id for item in pack.lessons}
    lab_to_pair = {lab.mini_lab_id: next(pair.pair_id for pair in pack.learning_pairs if pair.atom_id == lab.atom_id) for lab in pack.mini_labs}
    pair_issues: dict[str, list[dict[str, Any]]] = {}
    module_issues: list[dict[str, Any]] = []
    for result in repair_results:
        serialized = result.model_dump(mode="json")
        if result.item_type == "MODULE":
            module_issues.append(serialized)
            continue
        if result.item_type == "LEARNING_PAIR":
            pair_id = result.item_id
        elif result.item_type == "QUESTION":
            pair_id = question_to_pair[result.item_id]
        elif result.item_type == "LESSON":
            pair_id = lesson_to_pair[result.item_id]
        elif result.item_type == "MINI_LAB":
            pair_id = lab_to_pair[result.item_id]
        else:
            raise PermissionError(f"AI_REVIEW_REQUIRED: unsupported repair item type {result.item_type}")
        pair_issues.setdefault(pair_id, []).append(serialized)
    repaired_pack = pack
    snapshots: list[str] = []
    with ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix="9router-repair") as pool:
        futures = {
            pool.submit(
                _repair_pair_call,
                pack,
                pair_id,
                issues,
                adapter,
                settings,
                model,
                cache,
                resume,
            ): pair_id
            for pair_id, issues in pair_issues.items()
        }
        results = [future.result() for future in as_completed(futures)]
    for pair_id, content, snapshot in sorted(results, key=lambda item: item[0]):
        repaired_pack = _apply_pair_repair(repaired_pack, pair_id, content)
        snapshots.append(snapshot)
    if module_issues:
        mindmap, snapshot = _repair_mindmap_call(
            repaired_pack,
            module_issues,
            adapter,
            settings,
            model,
            cache,
            resume,
        )
        repaired_pack.mindmaps = [mindmap]
        snapshots.append(snapshot)
    report = validate_pack(
        repaired_pack,
        contract,
        require_contract=contract is not None,
        require_frozen_outline=require_frozen_outline,
    )
    if not report.valid:
        details = "; ".join(f"{item.code}:{item.path}" for item in report.issues)
        raise PermissionError(
            f"AI_REVIEW_REQUIRED: repaired candidate failed deterministic/contract validation: {details}"
        )
    return repaired_pack, snapshots


def run_ai_review(
    *,
    pack: PackData,
    workdir: Path,
    settings: Settings,
    model: str,
    prompt_version: str,
    max_repair_rounds: int,
    resume: bool,
    adapter: ResponsesAdapter | None = None,
    contract: PackContract | None = None,
    require_contract: bool = False,
    require_frozen_outline: bool = False,
) -> tuple[PackData, dict[str, Any]]:
    _require_supported_model(model, stage="review")
    if contract is None:
        contract = resolve_pack_contract(workdir=workdir, required=require_contract)
    report = validate_pack(
        pack,
        contract,
        require_contract=require_contract,
        require_frozen_outline=require_frozen_outline,
    )
    if not report.valid:
        details = "; ".join(f"{item.code}:{item.path}" for item in report.issues)
        raise ValueError(f"Deterministic/contract validation must pass before AI review: {details}")
    adapter = adapter or build_adapter(settings)
    cache = BuildCache(workdir / "cache.sqlite3")
    try:
        current = pack.model_copy(deep=True)
        for pair in current.learning_pairs:
            pair.ai_review_status = "PENDING"
        for lab in current.mini_labs:
            lab.ai_review_status = "PENDING"
        reviewer_snapshots: list[str] = []
        repair_generator_snapshots: list[str] = []
        repaired_count = 0
        completed: list[tuple[str, AIReviewBatch, str, str]] = []
        results: list[Any] = []
        for round_index in range(max_repair_rounds + 1):
            if round_index == 0:
                completed = _review_round(
                    pack=current,
                    cache=cache,
                    adapter=adapter,
                    settings=settings,
                    model=model,
                    prompt_version=prompt_version,
                    resume=resume,
                )
                results = [
                    item
                    for _, batch, _, _ in completed
                    for item in batch.reviews
                ]
            else:
                completed, results = _review_changed_items(
                    pack=current,
                    previous_results=results,
                    cache=cache,
                    adapter=adapter,
                    settings=settings,
                    model=model,
                    prompt_version=prompt_version,
                    resume=resume,
                )
            reviewer_snapshots.extend(snapshot for _, _, snapshot, _ in completed)
            errors = _verify_results(current, results)
            if not errors:
                break
            repair_results = [item for item in results if item.verdict == "REPAIR"]
            rejected = [item for item in results if item.verdict == "REJECT"]
            non_repair_errors = [error for error in errors if ":REPAIR:" not in error]
            if rejected or non_repair_errors or not repair_results or round_index >= max_repair_rounds:
                raise PermissionError("AI_REVIEW_REQUIRED: " + "; ".join(errors))
            repaired_count += len(repair_results)
            current, repair_snapshots = _repair_round(
                current,
                repair_results,
                adapter,
                settings,
                model,
                cache,
                resume,
                contract,
                require_frozen_outline,
            )
            repair_generator_snapshots.extend(repair_snapshots)
        else:
            raise PermissionError("AI_REVIEW_REQUIRED: repair budget exhausted")

        approved_pairs = {result.item_id for result in results if result.item_type == "LEARNING_PAIR"}
        approved_labs = {result.item_id for result in results if result.item_type == "MINI_LAB"}
        updated = current.model_copy(deep=True)
        for pair in updated.learning_pairs:
            if pair.pair_id not in approved_pairs:
                raise PermissionError(f"AI_REVIEW_REQUIRED: pair missing approval {pair.pair_id}")
            pair.ai_review_status = "AI_APPROVED"
        for lab in updated.mini_labs:
            if lab.mini_lab_id not in approved_labs:
                raise PermissionError(f"AI_REVIEW_REQUIRED: lab missing approval {lab.mini_lab_id}")
            lab.ai_review_status = "AI_APPROVED"

        final_validation = validate_pack(
            updated,
            contract,
            require_contract=require_contract,
            require_frozen_outline=require_frozen_outline,
        )
        if not final_validation.valid:
            details = "; ".join(f"{item.code}:{item.path}" for item in final_validation.issues)
            raise PermissionError(f"AI_REVIEW_REQUIRED: final contract validation failed: {details}")

        compact_results = [item.model_dump(mode="json") for item in sorted(results, key=lambda value: value.item_id)]
        results_hash = sha256_json(compact_results)
        review_report = {
            "status": "AI_APPROVED",
            "candidate_hash": sha256_json(updated.model_dump(mode="json", by_alias=True)),
            "source_hash": updated.source_hash,
            "reviewer_model": model,
            "reviewer_configured_model": settings.transport_model(model, reviewer=True),
            "reviewer_model_snapshots": sorted(set(reviewer_snapshots)),
            "repair_generator_model_snapshots": sorted(set(repair_generator_snapshots)),
            "reviewer_prompt_version": prompt_version,
            "reviewer_prompt_sha256": review_prompt_sha256(prompt_version),
            "reviewer_schema_version": REVIEW_SCHEMA_VERSION,
            "reviewer_schema_sha256": review_schema_sha256(),
            "reviewer_reasoning_effort": settings.review_reasoning_effort,
            "store_responses": False,
            "approved_items": len(results),
            "repaired_count": repaired_count,
            "rejected_count": 0,
            "results_sha256": results_hash,
            "results": compact_results,
        }
        if contract is not None:
            review_report["pack_contract_sha256"] = pack_contract_sha256(contract)
        return updated, review_report
    finally:
        cache.close()
