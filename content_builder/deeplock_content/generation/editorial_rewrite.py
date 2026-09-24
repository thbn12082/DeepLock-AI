from __future__ import annotations

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..cache import BuildCache
from ..contracts import PackContract, pack_contract_sha256
from ..editorial import (
    has_observable_learning_objective,
    learner_visible_lesson_word_breakdown,
    validate_lesson_editorial,
)
from ..models import AtomContent, PackData
from ..models import _require_supported_model
from ..quiz_order import rebalance_question_options, validate_quiz_editorial
from ..settings import Settings
from ..util import (
    PROMPT_ROOT,
    load_schema,
    responses_strict_schema,
    sha256_bytes,
    sha256_json,
    write_json,
)
from ..validators import validate_pack
from ..validators.content import learner_polish_is_advisory
from .api import ModelResponse, ResponsesAdapter
from .adapters import build_adapter


EDITORIAL_REWRITE_SCOPE = "LEARNER_REWRITE_ONLY_V1"
EDITORIAL_REWRITE_SCHEMA_VERSION = "1"
EDITORIAL_REWRITE_REPORT_FILENAME = "editorial-rewrite-report.json"
EDITORIAL_QUALITY_FEEDBACK_FILENAME = "editorial-quality-feedback.json"
EDITORIAL_QUALITY_FEEDBACK_SCHEMA_VERSION = "1"


CachedCall = Callable[..., ModelResponse]
QualityFeedbackByAtom = dict[str, tuple[str, ...]]


_LEARNER_SOURCE_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:·|Â·)?\s*(?:trang|slide|page)\s*\d+(?:\s*[-–—]\s*\d+)?\s*$",
    re.IGNORECASE,
)
_LEARNER_SOURCE_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_LEARNER_SOURCE_FILE_RE = re.compile(
    r"(?<![\w.])[^\s/\\<>:\"|?*]{1,80}\.(?:pdf|pptx?|docx?)(?!\w)",
    re.IGNORECASE,
)
_LEARNER_SOURCE_NARRATION_SENTENCE_RE = re.compile(
    r"(^|[.!?]\s+)(?:"
    r"(?:trang|slide|page)\s*(?:số\s*)?\d+[^.!?]{0,180}"
    r"|(?:bài giảng|tài liệu|nguồn)(?:\s+gốc)?\s+"
    r"(?:cho biết|mô tả|đề cập|nêu|trình bày|giới thiệu)[^.!?]{0,180}"
    r")([.!?]|$)",
    re.IGNORECASE,
)


def _clean_learner_text(value: str, *, title: bool = False) -> str:
    """Remove imported source furniture without changing substantive prose."""

    text = unicodedata.normalize("NFKC", value).strip()
    if title:
        text = _LEARNER_SOURCE_TITLE_SUFFIX_RE.sub("", text).strip()
    text = _LEARNER_SOURCE_URL_RE.sub("", text)
    text = _LEARNER_SOURCE_FILE_RE.sub("", text)
    text = _LEARNER_SOURCE_NARRATION_SENTENCE_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _normalize_learner_visible_fields(content: AtomContent) -> None:
    lesson = content.lesson
    lesson.title = _clean_learner_text(lesson.title, title=True)
    lesson.learning_objective = _clean_learner_text(lesson.learning_objective)
    lesson.hook = _clean_learner_text(lesson.hook)
    lesson.intuition = _clean_learner_text(lesson.intuition)
    lesson.tiny_example.input = _clean_learner_text(lesson.tiny_example.input)
    lesson.tiny_example.steps = [
        cleaned
        for item in lesson.tiny_example.steps
        if (cleaned := _clean_learner_text(item))
    ]
    lesson.tiny_example.output = _clean_learner_text(lesson.tiny_example.output)
    lesson.process_steps = [
        cleaned
        for item in lesson.process_steps
        if (cleaned := _clean_learner_text(item))
    ][:8]
    for block in lesson.formula_blocks:
        block.plain_text = _clean_learner_text(block.plain_text)
        for symbol in block.symbols:
            symbol.meaning = _clean_learner_text(symbol.meaning)
    lesson.common_mistake = _clean_learner_text(lesson.common_mistake)
    lesson.takeaways = [
        cleaned
        for item in lesson.takeaways
        if (cleaned := _clean_learner_text(item))
    ][:3]
    lesson.recall_prompt = _clean_learner_text(lesson.recall_prompt)
    for question in content.quiz_bundle.questions:
        question.stem = _clean_learner_text(question.stem)
        question.explanation = _clean_learner_text(question.explanation)
        for option in question.options:
            option.text = _clean_learner_text(option.text)
            option.rationale = _clean_learner_text(option.rationale)


def _load_editorial_quality_feedback(
    workdir: Path,
    pack: PackData,
) -> tuple[dict[str, Any] | None, QualityFeedbackByAtom, str | None]:
    """Load optional human learner-quality feedback with a strict local schema.

    Feedback is deliberately atom-addressed and contains no source material. It
    is normalized before hashing so formatting or atom order in the local JSON
    file cannot create a misleading audit identity.
    """

    path = workdir / EDITORIAL_QUALITY_FEEDBACK_FILENAME
    if not path.exists():
        return None, {}, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Invalid {EDITORIAL_QUALITY_FEEDBACK_FILENAME}: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise ValueError("Editorial quality feedback must be a JSON object")
    expected_top_fields = {
        "schema_version",
        "source_correctness_reviewed",
        "atoms",
    }
    if set(raw) != expected_top_fields:
        raise ValueError(
            "Editorial quality feedback fields must be exactly: "
            + ", ".join(sorted(expected_top_fields))
        )
    if raw.get("schema_version") != EDITORIAL_QUALITY_FEEDBACK_SCHEMA_VERSION:
        raise ValueError("Unsupported editorial quality feedback schema_version")
    if raw.get("source_correctness_reviewed") is not False:
        raise ValueError(
            "Editorial quality feedback must not claim source correctness review"
        )
    raw_atoms = raw.get("atoms")
    if not isinstance(raw_atoms, list) or not raw_atoms:
        raise ValueError("Editorial quality feedback atoms must be a nonempty array")

    atom_order = {item.atom_id: index for index, item in enumerate(pack.atoms)}
    feedback_by_atom: QualityFeedbackByAtom = {}
    for index, entry in enumerate(raw_atoms):
        if not isinstance(entry, dict) or set(entry) != {"atom_id", "feedback"}:
            raise ValueError(
                f"Editorial quality feedback atoms[{index}] must contain only "
                "atom_id and feedback"
            )
        atom_id = entry.get("atom_id")
        if not isinstance(atom_id, str) or not atom_id or atom_id != atom_id.strip():
            raise ValueError(
                f"Editorial quality feedback atoms[{index}].atom_id is invalid"
            )
        if atom_id not in atom_order:
            raise ValueError(
                f"Editorial quality feedback references unknown atom_id {atom_id!r}"
            )
        if atom_id in feedback_by_atom:
            raise ValueError(
                f"Editorial quality feedback duplicates atom_id {atom_id!r}"
            )
        raw_items = entry.get("feedback")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError(
                f"Editorial quality feedback for {atom_id} must be a nonempty array"
            )
        normalized_items: list[str] = []
        for feedback_index, item in enumerate(raw_items):
            if not isinstance(item, str):
                raise ValueError(
                    f"Editorial quality feedback {atom_id}[{feedback_index}] "
                    "must be a string"
                )
            normalized = " ".join(item.split())
            if not normalized:
                raise ValueError(
                    f"Editorial quality feedback {atom_id}[{feedback_index}] is empty"
                )
            if len(normalized) > 320 or len(normalized.split()) > 55:
                raise ValueError(
                    f"Editorial quality feedback {atom_id}[{feedback_index}] "
                    "must be concise (at most 320 characters and 55 words)"
                )
            if normalized in normalized_items:
                raise ValueError(
                    f"Editorial quality feedback {atom_id} contains a duplicate item"
                )
            normalized_items.append(normalized)
        feedback_by_atom[atom_id] = tuple(normalized_items)

    normalized_atoms = [
        {
            "atom_id": atom_id,
            "feedback": list(feedback_by_atom[atom_id]),
        }
        for atom_id in sorted(feedback_by_atom, key=atom_order.__getitem__)
    ]
    normalized_document = {
        "schema_version": EDITORIAL_QUALITY_FEEDBACK_SCHEMA_VERSION,
        "source_correctness_reviewed": False,
        "atoms": normalized_atoms,
    }
    return (
        normalized_document,
        feedback_by_atom,
        sha256_json(normalized_document),
    )


def editorial_prompt_sha256(prompt_version: str) -> str:
    return sha256_bytes(
        (PROMPT_ROOT / prompt_version / "system.txt").read_bytes()
    )


def editorial_schema_sha256() -> str:
    return sha256_json(load_schema("module-editorial-rewrite.schema.json"))


def _module_units(
    pack: PackData,
    module_id: str,
    atom_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    module = next(item for item in pack.modules if item.module_id == module_id)
    atoms = {item.atom_id: item for item in pack.atoms}
    pairs = {item.atom_id: item for item in pack.learning_pairs}
    lessons = {item.lesson_id: item for item in pack.lessons}
    questions = {item.question_id: item for item in pack.questions}
    labs = {item.atom_id: item for item in pack.mini_labs}
    requested_ids = (
        [item.atom_id for item in module.atom_plans]
        if atom_ids is None
        else list(atom_ids)
    )
    selected = set(requested_ids)
    plans = [item for item in module.atom_plans if item.atom_id in selected]
    if [item.atom_id for item in plans] != requested_ids:
        raise ValueError(
            f"Editorial rewrite target is not an ordered subset of {module_id}"
        )
    units: list[dict[str, Any]] = []
    for plan in plans:
        atom = atoms[plan.atom_id]
        pair = pairs[plan.atom_id]
        lesson = lessons[pair.micro_lesson_id]
        ordered_questions = [questions[item_id] for item_id in pair.question_ids]
        if len(ordered_questions) != 5:
            raise ValueError(
                f"Editorial rewrite requires exactly five quizzes for {pair.pair_id}"
            )
        content = AtomContent(
            lesson=lesson,
            quiz_bundle={
                "questions": ordered_questions,
                "insufficient_evidence_items": [],
            },
            mini_lab=labs.get(plan.atom_id),
        )
        units.append({
            "atom_plan": plan.model_dump(mode="json"),
            "atom": atom.model_dump(mode="json"),
            "learning_pair": pair.model_dump(mode="json"),
            "content": content.model_dump(mode="json", by_alias=True),
        })
    return units


def _contiguous_editorial_groups(
    atom_ids: list[str],
    max_atoms_per_call: int,
) -> list[list[str]]:
    limit = max(1, min(5, int(max_atoms_per_call)))
    return [
        atom_ids[index:index + limit]
        for index in range(0, len(atom_ids), limit)
    ]


def _boundary_lesson(pack: PackData, atom_id: str | None) -> dict[str, Any] | None:
    if atom_id is None:
        return None
    lesson = next(item for item in pack.lessons if item.atom_id == atom_id)
    return {
        "atom_id": atom_id,
        "title": lesson.title,
        "learning_objective": lesson.learning_objective,
        "takeaways": list(lesson.takeaways),
    }


def _module_input(
    pack: PackData,
    module_id: str,
    target_atom_ids: list[str],
    group_index: int,
    group_count: int,
    quality_feedback_by_atom: QualityFeedbackByAtom | None = None,
) -> dict[str, Any]:
    module = next(item for item in pack.modules if item.module_id == module_id)
    lesson_by_atom = {item.atom_id: item for item in pack.lessons}
    global_atom_ids = [
        plan.atom_id
        for item in pack.modules
        for plan in item.atom_plans
    ]
    first_index = global_atom_ids.index(target_atom_ids[0])
    last_index = global_atom_ids.index(target_atom_ids[-1])
    previous_atom_id = global_atom_ids[first_index - 1] if first_index else None
    next_atom_id = (
        global_atom_ids[last_index + 1]
        if last_index + 1 < len(global_atom_ids)
        else None
    )
    payload = {
        "scope": EDITORIAL_REWRITE_SCOPE,
        "course": pack.course.model_dump(mode="json"),
        "module_sequence_context": [
            {
                "module_id": item.module_id,
                "order_index": item.order_index,
                "title": item.title,
                "objective": item.objective,
                "is_current": item.module_id == module_id,
                "first_lesson": {
                    "title": lesson_by_atom[item.atom_plans[0].atom_id].title,
                    "learning_objective": lesson_by_atom[
                        item.atom_plans[0].atom_id
                    ].learning_objective,
                },
                "last_lesson": {
                    "title": lesson_by_atom[item.atom_plans[-1].atom_id].title,
                    "learning_objective": lesson_by_atom[
                        item.atom_plans[-1].atom_id
                    ].learning_objective,
                },
            }
            for item in pack.modules
        ],
        "module": {
            "module_id": module.module_id,
            "order_index": module.order_index,
            "title": module.title,
            "objective": module.objective,
        },
        "full_module_roadmap": [
            {
                "atom_id": plan.atom_id,
                "order_index": plan.order_index,
                "title": plan.title,
                "objective": plan.objective,
                "prerequisite_ids": list(plan.prerequisite_ids),
            }
            for plan in module.atom_plans
        ],
        "target_group": {
            "group_index": group_index,
            "group_count": group_count,
            "ordered_atom_ids": list(target_atom_ids),
            "previous_boundary": _boundary_lesson(pack, previous_atom_id),
            "next_boundary": _boundary_lesson(pack, next_atom_id),
        },
        "ordered_atom_units": _module_units(
            pack,
            module_id,
            target_atom_ids,
        ),
        "immutable_contract": {
            "atom_order": list(target_atom_ids),
            "module_atom_order": [item.atom_id for item in module.atom_plans],
            "rewrite_surface": "learner-facing lesson prose and quiz semantics only",
            "source_correctness_reviewed": False,
        },
    }
    targeted_feedback = [
        {
            "atom_id": atom_id,
            "feedback": list((quality_feedback_by_atom or {})[atom_id]),
        }
        for atom_id in target_atom_ids
        if atom_id in (quality_feedback_by_atom or {})
    ]
    if targeted_feedback:
        # Never include the full feedback document or its global hash in a
        # request. This keeps unaffected groups' input hashes/cache keys stable.
        payload["learner_quality_feedback"] = targeted_feedback
    return payload


def _wire_schema(atom_count: int) -> dict[str, Any]:
    schema = load_schema("module-editorial-rewrite.schema.json")
    contents = schema["properties"]["atom_contents"]
    contents["minItems"] = atom_count
    contents["maxItems"] = atom_count
    return responses_strict_schema(schema)


def _lesson_builder_projection(content: AtomContent) -> dict[str, Any]:
    lesson = content.lesson
    return {
        "lesson_id": lesson.lesson_id,
        "pair_id": lesson.pair_id,
        "atom_id": lesson.atom_id,
        "grounding_type": lesson.grounding_type,
        "prerequisite_ids": list(lesson.prerequisite_ids),
        "estimated_seconds": lesson.estimated_seconds,
        "source_ref_ids": list(lesson.source_ref_ids),
        "illustration_source_ref_ids": list(lesson.illustration_source_ref_ids),
    }


def _question_builder_projection(content: AtomContent) -> list[dict[str, Any]]:
    return [
        {
            "question_id": question.question_id,
            "pair_id": question.pair_id,
            "atom_id": question.atom_id,
            "difficulty": question.difficulty,
            "bloom_level": question.bloom_level,
            "grounding_type": question.grounding_type,
            "source_ref_ids": list(question.source_ref_ids),
        }
        for question in content.quiz_bundle.questions
    ]


def _restore_builder_owned_fields(candidate: AtomContent, original: AtomContent) -> None:
    """Re-impose builder metadata that carries no grounding meaning.

    Callers must have already checked that the two describe the same atom.

    Deliberately narrow. Anything that says *where the content came from* -
    `source_ref_ids`, `illustration_source_ref_ids`, `grounding_type`, the mini
    lab and the insufficient-evidence list - is left alone so the strict
    comparison downstream still fails closed on source-ownership drift; a
    rewrite must never be able to claim a source the builder did not grant, and
    a mistyped reference is indistinguishable from an invented one.

    What is restored here is ordering and scoring metadata that the editor is
    given no basis to set: it sees one bounded group at a time, so it cannot
    know the module's dependency graph, and difficulty and Bloom level are the
    builder's own calibration. Models drift on these routinely - one run
    returned every `prerequisite_ids` empty, another nudged a question from
    difficulty 1 to 2 - and losing a whole lecture's rewrite over that is pure
    waste.
    """

    lesson, source = candidate.lesson, original.lesson
    lesson.lesson_id = source.lesson_id
    lesson.pair_id = source.pair_id
    lesson.atom_id = source.atom_id
    lesson.prerequisite_ids = list(source.prerequisite_ids)
    lesson.estimated_seconds = source.estimated_seconds
    # `grounding_type` is the builder's classification of how the atom is
    # grounded. The editor is forbidden from adding a fact, so grounding cannot
    # legitimately change under a rewrite, and a relabel is drift rather than a
    # claim on a new source. `source_ref_ids` stays out of this list on purpose.
    lesson.grounding_type = source.grounding_type

    for rewritten, origin in zip(
        candidate.quiz_bundle.questions, original.quiz_bundle.questions, strict=True
    ):
        rewritten.question_id = origin.question_id
        rewritten.pair_id = origin.pair_id
        rewritten.atom_id = origin.atom_id
        rewritten.difficulty = origin.difficulty
        rewritten.bloom_level = origin.bloom_level
        rewritten.grounding_type = origin.grounding_type


def _projection_diff(rewritten: Any, expected: Any, path: str = "") -> str:
    """Name the builder-owned fields a rewrite changed.

    Without this the failure only says a field drifted, which is not enough to
    tell an editor rewording something it owns from one quietly reassigning a
    source reference - and those two need opposite responses.
    """

    if isinstance(expected, dict) and isinstance(rewritten, dict):
        parts: list[str] = []
        for key in sorted(set(expected) | set(rewritten)):
            if expected.get(key) != rewritten.get(key):
                parts.append(_projection_diff(rewritten.get(key), expected.get(key), f"{path}.{key}" if path else key))
        return ", ".join(parts[:6])
    if isinstance(expected, list) and isinstance(rewritten, list):
        if len(expected) != len(rewritten):
            return f"{path} length {len(rewritten)} != {len(expected)}"
        parts = [
            _projection_diff(r, e, f"{path}[{index}]")
            for index, (r, e) in enumerate(zip(rewritten, expected))
            if r != e
        ]
        return ", ".join(parts[:6])
    return f"{path} {expected!r} -> {rewritten!r}"


def _builder_projection(content: AtomContent) -> dict[str, Any]:
    return {
        "lesson": _lesson_builder_projection(content),
        "questions": _question_builder_projection(content),
        "insufficient_evidence_items": list(
            content.quiz_bundle.insufficient_evidence_items
        ),
        "mini_lab": (
            content.mini_lab.model_dump(mode="json", by_alias=True)
            if content.mini_lab is not None
            else None
        ),
    }


def _parse_and_validate_module_output(
    data: dict[str, Any],
    *,
    pack: PackData,
    module_id: str,
    target_atom_ids: list[str] | None = None,
) -> list[AtomContent]:
    if data.get("module_id") != module_id:
        raise ValueError(f"Editorial rewrite changed module ID {module_id}")
    raw_contents = data.get("atom_contents")
    if not isinstance(raw_contents, list):
        raise ValueError(f"Editorial rewrite omitted atom_contents for {module_id}")
    original_units = _module_units(pack, module_id, target_atom_ids)
    if len(raw_contents) != len(original_units):
        raise ValueError(f"Editorial rewrite changed atom membership for {module_id}")

    rewritten = [AtomContent.model_validate(item) for item in raw_contents]
    quality_failures: list[str] = []
    for original_unit, candidate in zip(original_units, rewritten, strict=True):
        original = AtomContent.model_validate(original_unit["content"])
        atom_id = original.lesson.atom_id
        # Restoring metadata onto the wrong atom would silently mis-attach
        # content, so alignment is checked before anything is copied across.
        if candidate.lesson.atom_id != atom_id:
            raise ValueError(
                "Editorial rewrite returned atom "
                f"{candidate.lesson.atom_id!r} where {atom_id!r} was expected"
            )
        if len(candidate.quiz_bundle.questions) != len(original.quiz_bundle.questions):
            raise ValueError(
                f"Editorial rewrite changed the question count for {atom_id}"
            )
        # Re-impose ordering and scoring metadata the editor cannot know, then
        # still compare the full projection: source ownership, grounding and
        # the mini lab are untouched above, so drift there fails closed.
        _restore_builder_owned_fields(candidate, original)
        candidate_projection = _builder_projection(candidate)
        original_projection = _builder_projection(original)
        if candidate_projection != original_projection:
            raise ValueError(
                f"Editorial rewrite changed builder-owned fields for {atom_id}: "
                f"{_projection_diff(candidate_projection, original_projection)}"
            )
        if len(candidate.quiz_bundle.questions) != 5:
            raise ValueError(
                f"Editorial rewrite must preserve exactly five quizzes for {atom_id}"
            )
        _normalize_learner_visible_fields(candidate)
        # A rewrite can be pedagogically sound while opening its objective with
        # a verb outside the deliberately narrow deterministic allow-list. The
        # input candidate already passed pack validation, so retain its exact
        # objective instead of discarding an otherwise valid group response.
        # This mutation is serialized back into the durable cache below.
        if not has_observable_learning_objective(
            candidate.lesson.learning_objective
        ):
            candidate.lesson.learning_objective = (
                original.lesson.learning_objective
            )
        rebalance_question_options(candidate.quiz_bundle.questions, atom_id)
        # Structural quiz failures must trigger correction even when learner
        # polish is advisory; otherwise a READY group poisons every resume and
        # the final pack validator rejects the same duplicate indefinitely.
        validate_quiz_editorial(candidate.quiz_bundle.questions, learner_polish=False)
        try:
            validate_quiz_editorial(
                candidate.quiz_bundle.questions,
                learner_polish=True,
            )
        except ValueError as error:
            quality_failures.append(
                f"{atom_id}[QUIZ:{error}. ACTION: replace every flagged distractor "
                "with a credible technical misconception on the same axis as the stem]"
            )
        issues = validate_lesson_editorial(
            candidate.lesson,
            pedagogical=True,
            learner_polish=True,
        )
        if issues:
            issue_details: list[str] = []
            for item in issues:
                action = ""
                if item.code == "LESSON_TEXT_EXCESSIVE":
                    current_counts = learner_visible_lesson_word_breakdown(
                        candidate.lesson
                    )
                    counts_text = ", ".join(
                        f"{name}={count}"
                        for name, count in current_counts.items()
                    )
                    action = (
                        " ACTION: rewrite this atom to 360-460 learner-visible words; "
                        "preserve every substantive fact but state each fact once, remove "
                        "repetition, and compress transitions and sentence wording. Treat the "
                        "following as hard maxima, not targets: title<=10 words, objective<=22, "
                        "hook<=18, intuition<=55, tiny_example total<=65, process_steps "
                        "total<=80, formula_blocks total<=50, common_mistake<=20, takeaways "
                        "total<=35, recall_prompt<=15. Count whitespace-separated words across "
                        "all fields before returning and do not exceed any field budget. "
                        f"Current exact field counts are: {counts_text}"
                    )
                elif item.code in {
                    "LESSON_OBJECTIVE_NOT_OBSERVABLE",
                    "LESSON_OBJECTIVE_GENERIC_ROLE_TEMPLATE",
                }:
                    action = (
                        " ACTION: state a concrete observable learner action and the exact "
                        "result the learner can check"
                    )
                issue_details.append(
                    f"{item.code}@{item.path} ({item.message}){action}"
                )
            details = ", ".join(issue_details)
            quality_failures.append(f"{atom_id}[LESSON:{details}]")

    if quality_failures:
        # These are style findings on content a model has already rewritten for
        # the learner. Failing here throws away the whole lecture, including
        # every group that came back clean, so in advisory mode the findings are
        # reported and the rewrite is kept. Identity, source ownership and
        # structural checks above still raise.
        if learner_polish_is_advisory():
            print(
                {
                    "phase": "editorial-quality-advisory",
                    "module_id": module_id,
                    "findings": len(quality_failures),
                    "detail": "; ".join(quality_failures)[:400],
                },
                flush=True,
            )
        else:
            raise ValueError(
                "Editorial rewrite failed group quality: "
                + "; ".join(quality_failures)
            )

    # Persist builder-normalized quiz ordering in READY cache entries as well as
    # fresh responses, so resume cannot change the final candidate.
    data.clear()
    data.update({
        "module_id": module_id,
        "atom_contents": [
            item.model_dump(mode="json", by_alias=True) for item in rewritten
        ],
    })
    return rewritten


def _apply_module_contents(
    pack: PackData,
    module_id: str,
    contents: list[AtomContent],
) -> None:
    module = next(item for item in pack.modules if item.module_id == module_id)
    if [item.lesson.atom_id for item in contents] != [
        item.atom_id for item in module.atom_plans
    ]:
        raise ValueError(f"Editorial rewrite changed atom order for {module_id}")
    lessons = {item.lesson.lesson_id: item.lesson for item in contents}
    questions = {
        question.question_id: question
        for item in contents
        for question in item.quiz_bundle.questions
    }
    pack.lessons = [lessons.get(item.lesson_id, item) for item in pack.lessons]
    pack.questions = [questions.get(item.question_id, item) for item in pack.questions]


def learner_rewrite_module_hash(pack: PackData, module_id: str) -> str:
    """Hash every final learner-facing output owned by one module."""

    modules = [item for item in pack.modules if item.module_id == module_id]
    if len(modules) != 1:
        raise ValueError(f"Expected exactly one module for {module_id!r}")
    atom_ids = {
        item.atom_id for item in pack.atoms if item.module_id == module_id
    }
    return sha256_json({
        "module": modules[0].model_dump(mode="json"),
        "full_lessons": [
            item.model_dump(mode="json")
            for item in pack.full_lessons
            if item.module_id == module_id
        ],
        "atoms": [
            item.model_dump(mode="json")
            for item in pack.atoms
            if item.atom_id in atom_ids
        ],
        "lessons": [
            item.model_dump(mode="json")
            for item in pack.lessons
            if item.atom_id in atom_ids
        ],
        "learning_pairs": [
            item.model_dump(mode="json")
            for item in pack.learning_pairs
            if item.atom_id in atom_ids
        ],
        "questions": [
            item.model_dump(mode="json")
            for item in pack.questions
            if item.atom_id in atom_ids
        ],
        "mini_labs": [
            item.model_dump(mode="json")
            for item in pack.mini_labs
            if item.atom_id in atom_ids
        ],
    })


def run_editorial_rewrite(
    *,
    pack: PackData,
    workdir: Path,
    settings: Settings,
    resume: bool,
    adapter: ResponsesAdapter | None = None,
    cached_call: CachedCall | None = None,
    contract: PackContract | None = None,
    require_contract: bool = False,
    require_frozen_outline: bool = False,
) -> tuple[PackData, dict[str, Any]]:
    """Rewrite modules in bounded groups while preserving the frozen graph."""

    _require_supported_model(settings.editorial_model, stage="learner editorial rewrite")
    before = validate_pack(
        pack,
        contract,
        require_contract=require_contract,
        require_frozen_outline=require_frozen_outline,
    )
    if not before.valid:
        details = "; ".join(f"{item.code}:{item.path}" for item in before.issues)
        raise ValueError(
            f"Candidate must pass deterministic validation before editorial rewrite: {details}"
        )
    if cached_call is None:
        # Late import avoids making builder/editorial_rewrite a module cycle and
        # keeps the existing durable generation cache contract in one place.
        from .builder import _cached_call as cached_call

    editorial_settings = replace(
        settings,
        generator_reasoning_effort=settings.editorial_reasoning_effort,
    )
    adapter = adapter or build_adapter(editorial_settings)
    instructions = (
        PROMPT_ROOT / settings.editorial_prompt_version / "system.txt"
    ).read_text(encoding="utf-8")
    (
        quality_feedback_document,
        quality_feedback_by_atom,
        quality_feedback_sha256,
    ) = _load_editorial_quality_feedback(workdir, pack)
    cache = BuildCache(workdir / "cache.sqlite3")
    try:
        group_tasks: list[tuple[str, list[str], int, int]] = []
        for module in pack.modules:
            module_atom_ids = [item.atom_id for item in module.atom_plans]
            # Validate the full module before starting any upstream request, so
            # a malformed later group cannot leave a partial editorial pass.
            _module_units(pack, module.module_id, module_atom_ids)
            groups = _contiguous_editorial_groups(
                module_atom_ids,
                settings.max_atoms_per_editorial_call,
            )
            group_tasks.extend(
                (module.module_id, atom_ids, group_index, len(groups))
                for group_index, atom_ids in enumerate(groups)
            )

        def rewrite_one(
            task: tuple[str, list[str], int, int],
        ) -> tuple[str, int, list[AtomContent], str]:
            module_id, target_atom_ids, group_index, group_count = task
            payload = _module_input(
                pack,
                module_id,
                target_atom_ids,
                group_index,
                group_count,
                quality_feedback_by_atom,
            )
            atom_count = len(payload["ordered_atom_units"])

            def validate_output(data: dict[str, Any]) -> None:
                _parse_and_validate_module_output(
                    data,
                    pack=pack,
                    module_id=module_id,
                    target_atom_ids=target_atom_ids,
                )

            atom_identity = sha256_json(target_atom_ids).removeprefix("sha256:")[:16]
            response = cached_call(
                cache=cache,
                adapter=adapter,
                settings=editorial_settings,
                step_type=(
                    f"EDITORIAL_REWRITE_MODULE:{module_id}:"
                    f"GROUP:{group_index + 1}-OF-{group_count}:"
                    f"ATOMS:{atom_identity}"
                ),
                input_hash=sha256_json(payload),
                prompt_version=settings.editorial_prompt_version,
                logical_model=settings.editorial_model,
                instructions=instructions,
                input_text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                schema_name=(
                    f"module_editorial_rewrite_{atom_count}_"
                    f"group_{group_index + 1}"
                ),
                schema=_wire_schema(atom_count),
                resume=resume,
                output_validator=validate_output,
                validation_feedback_max_chars=4_000,
                include_invalid_output_in_correction=True,
                correction_output_verbosity="low",
                correction_context={
                    "module_id": module_id,
                    "ordered_atom_ids": target_atom_ids,
                    "builder_owned_fields": [
                        _builder_projection(AtomContent.model_validate(unit["content"]))
                        for unit in _module_units(pack, module_id, target_atom_ids)
                    ],
                },
            )
            contents = _parse_and_validate_module_output(
                response.data,
                pack=pack,
                module_id=module_id,
                target_atom_ids=target_atom_ids,
            )
            return module_id, group_index, contents, response.model_snapshot

        completed: dict[tuple[str, int], tuple[list[AtomContent], str]] = {}
        with ThreadPoolExecutor(
            max_workers=min(settings.concurrency, len(group_tasks)),
            thread_name_prefix="9router-editorial-rewrite",
        ) as pool:
            futures = {
                pool.submit(rewrite_one, task): (task[0], task[2])
                for task in group_tasks
            }
            for future in as_completed(futures):
                module_id, group_index, contents, snapshot = future.result()
                completed[(module_id, group_index)] = (contents, snapshot)

        updated = pack.model_copy(deep=True)
        for module in updated.modules:
            group_count = sum(
                1 for task in group_tasks if task[0] == module.module_id
            )
            contents = [
                content
                for group_index in range(group_count)
                for content in completed[(module.module_id, group_index)][0]
            ]
            expected_atom_ids = [item.atom_id for item in module.atom_plans]
            if [item.lesson.atom_id for item in contents] != expected_atom_ids:
                raise ValueError(
                    f"Editorial rewrite group coverage mismatch for {module.module_id}"
                )
            _apply_module_contents(updated, module.module_id, contents)

        snapshots = {
            value.strip()
            for value in updated.generator_model_snapshot.split(",")
            if value.strip()
        }
        snapshots.update(snapshot for _, snapshot in completed.values())
        updated.generator_model_snapshot = ",".join(sorted(snapshots))

        # AI_APPROVED is a builder-owned materialized quality status. The model
        # was required to echo PENDING unchanged; only complete module coverage
        # plus deterministic validation may promote it here.
        for pair in updated.learning_pairs:
            pair.revision += 1
            hash_basis = {
                "pair_id": pair.pair_id,
                "atom_id": pair.atom_id,
                "micro_lesson_id": pair.micro_lesson_id,
                "question_ids": pair.question_ids,
                "revision": pair.revision,
            }
            pair.content_hash = sha256_json(hash_basis)
            pair.ai_review_status = "AI_APPROVED"
        for lab in updated.mini_labs:
            lab.ai_review_status = "AI_APPROVED"

        final_validation = validate_pack(
            updated,
            contract,
            require_contract=require_contract,
            require_frozen_outline=require_frozen_outline,
        )
        if not final_validation.valid:
            details = "; ".join(
                f"{item.code}:{item.path}" for item in final_validation.issues
            )
            raise ValueError(
                f"Editorial rewrite produced an invalid candidate: {details}"
            )

        results = [
            {
                "module_id": module.module_id,
                "verdict": "APPROVE",
                "issue_codes": [],
                "output_hash": learner_rewrite_module_hash(
                    updated,
                    module.module_id,
                ),
            }
            for module in updated.modules
        ]
        report = {
            "status": "EDITORIAL_REWRITE_APPROVED",
            "review_scope": EDITORIAL_REWRITE_SCOPE,
            "source_correctness_reviewed": False,
            "candidate_hash": sha256_json(
                updated.model_dump(mode="json", by_alias=True)
            ),
            "source_hash": updated.source_hash,
            "editorial_model": settings.editorial_model,
            "editorial_configured_model": settings.transport_model(
                settings.editorial_model
            ),
            "editorial_model_snapshots": sorted(
                {snapshot for _, snapshot in completed.values()}
            ),
            "editorial_prompt_version": settings.editorial_prompt_version,
            "editorial_prompt_sha256": editorial_prompt_sha256(
                settings.editorial_prompt_version
            ),
            "editorial_schema_version": EDITORIAL_REWRITE_SCHEMA_VERSION,
            "editorial_schema_sha256": editorial_schema_sha256(),
            "editorial_reasoning_effort": settings.editorial_reasoning_effort,
            "store_responses": False,
            "approved_items": len(results),
            "repaired_count": len(results),
            "rejected_count": 0,
            "results_sha256": sha256_json(results),
            "results": results,
        }
        report["editorial_quality_feedback_present"] = (
            quality_feedback_document is not None
        )
        if quality_feedback_document is not None:
            feedback_group_count = sum(
                any(atom_id in quality_feedback_by_atom for atom_id in target_atom_ids)
                for _, target_atom_ids, _, _ in group_tasks
            )
            report.update({
                "editorial_quality_feedback_sha256": quality_feedback_sha256,
                "editorial_quality_feedback_atom_count": len(
                    quality_feedback_by_atom
                ),
                "editorial_quality_feedback_group_count": feedback_group_count,
            })
        if contract is not None:
            report["pack_contract_sha256"] = pack_contract_sha256(contract)
        write_json(workdir / EDITORIAL_REWRITE_REPORT_FILENAME, report)
        return updated, report
    finally:
        cache.close()
