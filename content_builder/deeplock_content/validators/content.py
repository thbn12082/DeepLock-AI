from __future__ import annotations

import os

import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..contracts import PackContract, check_pack_contract
from ..editorial import is_course_v2_prompt_version, validate_lesson_editorial
from ..models import MiniLab, PackData
from ..quiz_order import (
    duplicate_option_pairs,
    duplicate_question_stem_pairs,
    validate_quiz_editorial,
)
from ..util import ensure_finite, sha256_bytes, sha256_json


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    path: str
    message: str
    severity: str = "ERROR"


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid: bool
    issue_count: int
    issues: list[ValidationIssue]
    stats: dict[str, int]


def _cycle(nodes: set[str], edges: list[tuple[str, str]]) -> bool:
    graph = {node: [] for node in nodes}
    for source, target in edges:
        if source in graph and target in graph:
            graph[source].append(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(target) for target in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in nodes)


def _lab_issues(lab: MiniLab) -> list[str]:
    try:
        ensure_finite(lab.spec)
    except ValueError as error:
        return [str(error)]
    spec = lab.spec
    if lab.lab_type == "LR_STEP_1D":
        required = {"initial_weight", "gradient", "learning_rate_min", "learning_rate_max", "learning_rate_default"}
        if set(spec) != required:
            return ["LR_STEP_1D fields mismatch"]
        if not 0 < float(spec["learning_rate_min"]) <= float(spec["learning_rate_default"]) <= float(spec["learning_rate_max"]) <= 10:
            return ["Learning rate range/default invalid"]
    elif lab.lab_type == "ACTIVATION_CURVE":
        if spec.get("function") not in {"RELU", "SIGMOID", "TANH"} or not 3 <= int(spec.get("samples", 0)) <= 256:
            return ["Activation curve function/samples invalid"]
    elif lab.lab_type == "CONVOLUTION_2D":
        matrix, kernel = spec.get("input", []), spec.get("kernel", [])
        if not matrix or not kernel or len(matrix) > 9 or len(kernel) > 5:
            return ["Convolution matrix size invalid"]
        widths = {len(row) for row in matrix}
        kernel_widths = {len(row) for row in kernel}
        if len(widths) != 1 or len(kernel_widths) != 1 or max(widths) > 9 or max(kernel_widths) > 5:
            return ["Convolution matrices must be bounded rectangles"]
        if len(kernel) > len(matrix) or max(kernel_widths) > max(widths):
            return ["Kernel exceeds input"]
    elif lab.lab_type == "OVERFITTING_CURVE":
        train, validation = spec.get("train_loss", []), spec.get("validation_loss", [])
        if len(train) != len(validation) or not 3 <= len(train) <= 256 or any(float(x) < 0 for x in train + validation):
            return ["Overfitting curves invalid"]
    elif lab.lab_type == "CONFUSION_MATRIX":
        labels, matrix = spec.get("labels", []), spec.get("matrix", [])
        n = len(labels)
        if not 2 <= n <= 10 or len(matrix) != n or any(len(row) != n for row in matrix):
            return ["Confusion matrix must be square and match labels"]
        if any(int(value) < 0 for row in matrix for value in row):
            return ["Confusion counts must be non-negative"]
    payload = str(spec).casefold()
    if any(token in payload for token in ("<script", "javascript:", "import os", "system(")):
        return ["Executable/script payload is forbidden"]
    return []



def learner_polish_is_advisory() -> bool:
    """Whether learner-polish style findings should warn instead of fail.

    Off by default so the strict course-v2 behaviour is unchanged. Turn it on
    with DEEPLOCK_LEARNER_POLISH_ADVISORY=1 when the goal is to keep every
    lecture that a model has genuinely rewritten, accepting that some lessons
    will still read a little formulaic.
    """

    return os.getenv("DEEPLOCK_LEARNER_POLISH_ADVISORY", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def validate_pack(
    pack: PackData,
    contract: PackContract | None = None,
    *,
    require_contract: bool = False,
    require_frozen_outline: bool = False,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    # When learner-polish findings are advisory, a lesson that a model has
    # already rewritten for the learner is accepted even if it still trips a
    # deterministic style rule. The findings are still recorded, as warnings,
    # so nothing is hidden; only the pass/fail verdict changes. Structural and
    # provenance checks stay ERROR either way.
    polish_severity = "WARNING" if learner_polish_is_advisory() else "ERROR"

    def add(code: str, path: str, message: str, severity: str = "ERROR") -> None:
        issues.append(
            ValidationIssue(code=code, path=path, message=message, severity=severity)
        )

    if contract is None:
        if require_contract:
            add(
                "PACK_CONTRACT_REQUIRED",
                "pack-contract.json",
                "Production curriculum validation requires a PackContract sidecar",
            )
    else:
        for check in check_pack_contract(
            pack,
            contract,
            require_frozen_outline=require_frozen_outline,
        ):
            add(check.code, check.path, check.message)

    collections: dict[str, list[str]] = {
        "module": [item.module_id for item in pack.modules],
        "full_lesson": [item.full_lesson_id for item in pack.full_lessons],
        "atom": [item.atom_id for item in pack.atoms],
        "lesson": [item.lesson_id for item in pack.lessons],
        "pair": [item.pair_id for item in pack.learning_pairs],
        "question": [item.question_id for item in pack.questions],
        "mini_lab": [item.mini_lab_id for item in pack.mini_labs],
        "illustration": [item.illustration_id for item in pack.illustrations],
        "mind_map": [item.mind_map_id for item in pack.mindmaps],
        "source_ref": [item.source_ref_id for item in pack.source_refs],
        "excerpt": [item.source_excerpt_id for item in pack.source_excerpts],
    }
    for kind, identifiers in collections.items():
        duplicates = [key for key, count in Counter(identifiers).items() if count > 1]
        for identifier in duplicates:
            add("DUPLICATE_ID", f"{kind}:{identifier}", "ID must be unique")

    modules = {item.module_id: item for item in pack.modules}
    atoms = {item.atom_id: item for item in pack.atoms}
    lessons = {item.lesson_id: item for item in pack.lessons}
    pairs = {item.pair_id: item for item in pack.learning_pairs}
    questions = {item.question_id: item for item in pack.questions}
    refs = {item.source_ref_id: item for item in pack.source_refs}
    illustration_sources = {item.source_ref_id for item in pack.illustrations}
    used_illustration_sources: set[str] = set()

    asset_members = [item.asset_member for item in pack.illustrations]
    for member, count in Counter(asset_members).items():
        if count > 1:
            add("ILLUSTRATION_MEMBER_DUPLICATE", f"illustration_member:{member}", "Asset member must be unique")
    reserved_members = {
        "manifest.json", "ai_review_manifest.json", "course.json", "modules.json",
        "full_lessons.json", "atoms.json", "lessons.json", "learning_pairs.json",
        "questions.json", "mindmaps.json", "mini_labs.json", "illustrations.json",
        "source_refs.json", "source_excerpts.json", "pack-contract.json",
    }
    for illustration in pack.illustrations:
        path = f"illustration:{illustration.illustration_id}"
        if illustration.source_ref_id not in refs:
            add("ILLUSTRATION_SOURCE_UNKNOWN", path, illustration.source_ref_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:png|webp)", illustration.asset_member):
            add("ILLUSTRATION_MEMBER_INVALID", path, "Asset member must be a flat PNG/WebP filename")
        if illustration.asset_member in reserved_members:
            add("ILLUSTRATION_MEMBER_RESERVED", path, illustration.asset_member)
        mime_suffix = {"image/png": ".png", "image/webp": ".webp"}
        expected_suffix = mime_suffix.get(illustration.mime_type)
        if expected_suffix is None:
            add("ILLUSTRATION_MIME_INVALID", path, illustration.mime_type)
        elif not illustration.asset_member.lower().endswith(expected_suffix):
            add("ILLUSTRATION_MIME_MISMATCH", path, "Asset extension does not match mime_type")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", illustration.sha256):
            add("ILLUSTRATION_HASH_INVALID", path, illustration.sha256)
        if not 1 <= illustration.byte_size <= 16 * 1024 * 1024:
            add("ILLUSTRATION_SIZE_INVALID", path, str(illustration.byte_size))
        if not 1 <= illustration.width <= 8192 or not 1 <= illustration.height <= 8192:
            add("ILLUSTRATION_DIMENSIONS_INVALID", path, f"{illustration.width}x{illustration.height}")
        elif illustration.width * illustration.height > 16_777_216:
            add("ILLUSTRATION_PIXELS_INVALID", path, f"{illustration.width}x{illustration.height}")
        if illustration.page_number < 1:
            add("ILLUSTRATION_PAGE_INVALID", path, str(illustration.page_number))
        elif illustration.source_ref_id in refs:
            source_ref = refs[illustration.source_ref_id]
            if not source_ref.page_start <= illustration.page_number <= source_ref.page_end:
                add(
                    "ILLUSTRATION_PAGE_OUTSIDE_SOURCE",
                    path,
                    f"page {illustration.page_number} is outside {source_ref.page_start}-{source_ref.page_end}",
                )
        if not illustration.caption.strip():
            add("ILLUSTRATION_CAPTION_EMPTY", path, "caption must contain visible text")
        if not illustration.alt_text.strip():
            add("ILLUSTRATION_ALT_TEXT_EMPTY", path, "alt_text must contain visible text")
    # Course outline, atom records and FullLesson records must describe the same
    # complete set.  A partial outline is not an acceptable runtime fallback.
    planned_atom_owner: dict[str, str] = {}
    plans_by_atom: dict[str, Any] = {}
    for module in pack.modules:
        local_plan_ids: set[str] = set()
        for plan in module.atom_plans:
            if plan.atom_id in local_plan_ids or plan.atom_id in planned_atom_owner:
                add("ATOM_PLAN_MULTIPLE_MODULES", f"module:{module.module_id}", plan.atom_id)
            local_plan_ids.add(plan.atom_id)
            planned_atom_owner[plan.atom_id] = module.module_id
            plans_by_atom.setdefault(plan.atom_id, plan)
            if not set(plan.source_ref_ids) <= refs.keys():
                add("ATOM_PLAN_SOURCE_UNKNOWN", f"atom_plan:{plan.atom_id}", "Unknown source ref")

    for atom in pack.atoms:
        if atom.module_id not in modules:
            add("ATOM_MODULE_UNKNOWN", f"atom:{atom.atom_id}", atom.module_id)
        plan_module_id = planned_atom_owner.get(atom.atom_id)
        if plan_module_id is None:
            add("ATOM_PLAN_MISSING", f"atom:{atom.atom_id}", "Atom is absent from module outline")
        elif plan_module_id != atom.module_id:
            add("ATOM_MODULE_MISMATCH", f"atom:{atom.atom_id}", "Outline and atom module differ")
        else:
            plan = next(item for item in modules[plan_module_id].atom_plans if item.atom_id == atom.atom_id)
            if (
                plan.order_index != atom.order_index
                or plan.title != atom.title
                or plan.prerequisite_ids != atom.prerequisite_ids
            ):
                add("ATOM_PLAN_MISMATCH", f"atom:{atom.atom_id}", "Outline and atom record differ")
    for atom_id in planned_atom_owner.keys() - atoms.keys():
        add("ATOM_RECORD_MISSING", f"atom_plan:{atom_id}", "Outline atom has no atom record")

    full_lesson_atom_owner: dict[str, str] = {}
    full_lessons_by_module: Counter[str] = Counter()
    for full_lesson in pack.full_lessons:
        full_lessons_by_module[full_lesson.module_id] += 1
        if full_lesson.module_id not in modules:
            add("FULL_LESSON_MODULE_UNKNOWN", f"full_lesson:{full_lesson.full_lesson_id}", full_lesson.module_id)
        if not full_lesson.atom_ids:
            add("FULL_LESSON_EMPTY", f"full_lesson:{full_lesson.full_lesson_id}", "FullLesson needs atoms")
        if len(full_lesson.atom_ids) != len(set(full_lesson.atom_ids)):
            add("FULL_LESSON_DUPLICATE_ATOM", f"full_lesson:{full_lesson.full_lesson_id}", "Atom IDs must be unique")
        for atom_id in full_lesson.atom_ids:
            atom = atoms.get(atom_id)
            if atom is None:
                add("FULL_LESSON_ATOM_UNKNOWN", f"full_lesson:{full_lesson.full_lesson_id}", atom_id)
                continue
            if atom.module_id != full_lesson.module_id:
                add("FULL_LESSON_ATOM_MODULE_MISMATCH", f"full_lesson:{full_lesson.full_lesson_id}", atom_id)
            previous = full_lesson_atom_owner.setdefault(atom_id, full_lesson.full_lesson_id)
            if previous != full_lesson.full_lesson_id:
                add("ATOM_MULTIPLE_FULL_LESSONS", f"atom:{atom_id}", f"{previous}/{full_lesson.full_lesson_id}")
    for module_id in modules:
        if not full_lessons_by_module[module_id]:
            add("MODULE_FULL_LESSON_MISSING", f"module:{module_id}", "Module needs a FullLesson")
    for atom_id in atoms.keys() - full_lesson_atom_owner.keys():
        add("ATOM_FULL_LESSON_MISSING", f"atom:{atom_id}", "Atom is absent from FullLesson coverage")

    question_owner: dict[str, str] = {}
    lesson_owner: dict[str, str] = {}
    pair_atom_counts: Counter[str] = Counter()
    pair_by_atom: dict[str, Any] = {}
    course_v2 = is_course_v2_prompt_version(pack.generator_prompt_version)

    for pair in pack.learning_pairs:
        pair_atom_counts[pair.atom_id] += 1
        pair_by_atom.setdefault(pair.atom_id, pair)
        if pair.atom_id not in atoms:
            add("PAIR_ATOM_UNKNOWN", f"pair:{pair.pair_id}", pair.atom_id)
        lesson = lessons.get(pair.micro_lesson_id)
        if lesson is None:
            add("PAIR_LESSON_MISSING", f"pair:{pair.pair_id}", pair.micro_lesson_id)
        elif lesson.atom_id != pair.atom_id or lesson.pair_id != pair.pair_id:
            add("PAIR_ATOM_MISMATCH", f"pair:{pair.pair_id}", "Lesson atom/pair mismatch")
        if pair.micro_lesson_id in lesson_owner:
            add("LESSON_MULTIPLE_PAIRS", f"lesson:{pair.micro_lesson_id}", "Lesson belongs to two pairs")
        lesson_owner[pair.micro_lesson_id] = pair.pair_id
        if len(pair.question_ids) < 3:
            add("PAIR_TOO_FEW_QUESTIONS", f"pair:{pair.pair_id}", "At least three questions required")
        if len(pair.question_ids) != len(set(pair.question_ids)):
            add("PAIR_DUPLICATE_QUESTION", f"pair:{pair.pair_id}", "Question IDs must be unique")
        hash_basis = {
            "pair_id": pair.pair_id,
            "atom_id": pair.atom_id,
            "micro_lesson_id": pair.micro_lesson_id,
            "question_ids": pair.question_ids,
            "revision": pair.revision,
        }
        if pair.content_hash != sha256_json(hash_basis):
            add("PAIR_CONTENT_HASH_INVALID", f"pair:{pair.pair_id}", "LearningPair content hash is stale")
        for question_id in pair.question_ids:
            if question_id in question_owner:
                add("QUESTION_MULTIPLE_PAIRS", f"question:{question_id}", "Question belongs to two pairs")
            question_owner[question_id] = pair.pair_id
            question = questions.get(question_id)
            if question is None:
                add("PAIR_QUESTION_MISSING", f"pair:{pair.pair_id}", question_id)
            elif question.atom_id != pair.atom_id or question.pair_id != pair.pair_id:
                add("PAIR_ATOM_MISMATCH", f"question:{question_id}", "Question escaped pair atom")

    for atom_id in atoms:
        if pair_atom_counts[atom_id] != 1:
            add("ATOM_PAIR_COVERAGE_INVALID", f"atom:{atom_id}", f"Expected one pair, found {pair_atom_counts[atom_id]}")
    for lesson in pack.lessons:
        owning_pair = pairs.get(lesson.pair_id)
        learner_polish = bool(
            course_v2
            and owning_pair is not None
            and owning_pair.ai_review_status == "AI_APPROVED"
        )
        for editorial_issue in validate_lesson_editorial(
            lesson,
            path=f"lesson:{lesson.lesson_id}",
            pedagogical=course_v2,
            learner_polish=learner_polish,
        ):
            add(
                editorial_issue.code,
                editorial_issue.path,
                editorial_issue.message,
                severity=polish_severity if learner_polish else "ERROR",
            )
        owner = lesson_owner.get(lesson.lesson_id)
        if owner is None:
            add("LESSON_PAIR_MISSING", f"lesson:{lesson.lesson_id}", "Lesson is outside all pairs")
        elif owner != lesson.pair_id:
            add("LESSON_PAIR_MISMATCH", f"lesson:{lesson.lesson_id}", f"Owner is {owner}")
        if lesson.atom_id not in atoms:
            add("LESSON_ATOM_UNKNOWN", f"lesson:{lesson.lesson_id}", lesson.atom_id)
        elif lesson.prerequisite_ids != atoms[lesson.atom_id].prerequisite_ids:
            add("LESSON_PREREQUISITE_MISMATCH", f"lesson:{lesson.lesson_id}", "Lesson and atom prerequisites differ")
        if not set(lesson.source_ref_ids) <= refs.keys():
            add("LESSON_SOURCE_UNKNOWN", f"lesson:{lesson.lesson_id}", "Lesson references unknown source")
        plan = plans_by_atom.get(lesson.atom_id)
        if plan is not None and lesson.source_ref_ids != plan.source_ref_ids:
            add(
                "LESSON_SOURCE_PLAN_MISMATCH",
                f"lesson:{lesson.lesson_id}",
                "Lesson source refs must exactly match AtomPlan source refs in order",
            )
        illustration_ref_ids = lesson.illustration_source_ref_ids
        if len(illustration_ref_ids) != len(set(illustration_ref_ids)):
            add("LESSON_ILLUSTRATION_DUPLICATE", f"lesson:{lesson.lesson_id}", "Illustration source refs must be unique")
        if not set(illustration_ref_ids) <= set(lesson.source_ref_ids):
            add(
                "LESSON_ILLUSTRATION_OUTSIDE_SOURCE",
                f"lesson:{lesson.lesson_id}",
                "Illustrations must come from a source already owned by the lesson",
            )
        for source_ref_id in illustration_ref_ids:
            if source_ref_id not in refs:
                add("LESSON_ILLUSTRATION_SOURCE_UNKNOWN", f"lesson:{lesson.lesson_id}", source_ref_id)
            if source_ref_id not in illustration_sources:
                add("LESSON_ILLUSTRATION_ASSET_MISSING", f"lesson:{lesson.lesson_id}", source_ref_id)
            used_illustration_sources.add(source_ref_id)

    for illustration in pack.illustrations:
        if illustration.source_ref_id not in used_illustration_sources:
            add(
                "ILLUSTRATION_UNUSED",
                f"illustration:{illustration.illustration_id}",
                "Every packaged illustration must be selected by at least one lesson",
            )
    for question in pack.questions:
        owner = question_owner.get(question.question_id)
        if owner is None:
            add("QUESTION_PAIR_MISSING", f"question:{question.question_id}", "Question is outside all pairs")
        elif owner != question.pair_id:
            add("QUESTION_PAIR_MISMATCH", f"question:{question.question_id}", f"Owner is {owner}")
        if question.pair_id not in pairs:
            add("QUESTION_PAIR_UNKNOWN", f"question:{question.question_id}", question.pair_id)

    forbidden_phrases = ("tất cả đáp án trên", "không đáp án nào")
    for index, question in enumerate(pack.questions):
        option_ids = [item.id for item in question.options]
        if option_ids != ["A", "B", "C", "D"]:
            add("OPTION_IDS_INVALID", f"questions[{index}]", "Expected ordered A-D")
        normalized = [" ".join(item.text.casefold().split()) for item in question.options]
        duplicate_pairs = duplicate_option_pairs(question)
        if duplicate_pairs:
            labels = ", ".join(f"{left}/{right}" for left, right in duplicate_pairs)
            add(
                "DUPLICATE_OPTION",
                f"questions[{index}]",
                f"Options must be distinct; duplicate pairs: {labels}",
            )
        if any(phrase in text for text in normalized for phrase in forbidden_phrases):
            add("FORBIDDEN_OPTION", f"questions[{index}]", "All/none-of-the-above is forbidden")
        if question.correct_option_id not in option_ids:
            add("CORRECT_OPTION_MISSING", f"questions[{index}]", question.correct_option_id)
        if not set(question.source_ref_ids) <= refs.keys():
            add("SOURCE_REF_UNKNOWN", f"questions[{index}]", "Question references unknown source")
        plan = plans_by_atom.get(question.atom_id)
        if plan is not None and not set(question.source_ref_ids) <= set(plan.source_ref_ids):
            add(
                "QUESTION_SOURCE_PLAN_MISMATCH",
                f"question:{question.question_id}",
                "Question sources must stay within its owning AtomPlan",
            )
        if question.grounding_type == "SOURCE_GROUNDED" and not question.source_ref_ids:
            add("SOURCE_REQUIRED", f"questions[{index}]", "Grounded quiz requires source")

    by_atom: dict[str, list[Any]] = {}
    for question in pack.questions:
        by_atom.setdefault(question.atom_id, []).append(question)
    for atom_id, atom_questions in by_atom.items():
        owning_pair = pair_by_atom.get(atom_id)
        if (
            course_v2
            and owning_pair is not None
            and owning_pair.ai_review_status == "AI_APPROVED"
        ):
            try:
                validate_quiz_editorial(
                    atom_questions,
                    learner_polish=True,
                )
            except ValueError as error:
                add(
                    "QUIZ_LEARNER_POLISH_FAILED",
                    f"atom:{atom_id}",
                    str(error),
                    severity=polish_severity,
                )
        for left_id, right_id in duplicate_question_stem_pairs(atom_questions):
            add("QUESTION_TOO_SIMILAR", f"atom:{atom_id}", f"{left_id}/{right_id}")

    prerequisite_edges = [(prerequisite, atom.atom_id) for atom in pack.atoms for prerequisite in atom.prerequisite_ids]
    if any(source not in atoms or target not in atoms for source, target in prerequisite_edges):
        add("PREREQUISITE_UNKNOWN", "atoms", "Prerequisite points outside atom set")
    if _cycle(set(atoms), prerequisite_edges):
        add("PREREQUISITE_CYCLE", "atoms", "Prerequisite graph must be acyclic")

    for ref in pack.source_refs:
        if ref.page_start > ref.page_end:
            add("SOURCE_PAGE_INVALID", f"source_ref:{ref.source_ref_id}", "Page range reversed")
        if ref.anchor_start >= ref.anchor_end:
            add("SOURCE_ANCHOR_INVALID", f"source_ref:{ref.source_ref_id}", "Anchor range is empty or reversed")

    excerpt_ref_counts: Counter[str] = Counter()
    for excerpt in pack.source_excerpts:
        excerpt_ref_counts[excerpt.source_ref_id] += 1
        ref = refs.get(excerpt.source_ref_id)
        if ref is None:
            add("EXCERPT_SOURCE_UNKNOWN", f"excerpt:{excerpt.source_excerpt_id}", "Invalid source link")
            continue
        if excerpt.document_id != ref.document_id or excerpt.chunk_id != ref.chunk_id:
            add("EXCERPT_SOURCE_MISMATCH", f"excerpt:{excerpt.source_excerpt_id}", "Excerpt is cross-linked to another source")
        if excerpt.page_start != ref.page_start or excerpt.page_end != ref.page_end:
            add("EXCERPT_PAGE_MISMATCH", f"excerpt:{excerpt.source_excerpt_id}", "Excerpt and source pages differ")
        if not 0 <= excerpt.highlight_start < excerpt.highlight_end <= len(excerpt.context_text):
            add("EXCERPT_OFFSET_INVALID", f"excerpt:{excerpt.source_excerpt_id}", "Highlight outside context")
        expected = sha256_bytes(excerpt.context_text.encode("utf-8"))
        if excerpt.excerpt_hash != expected:
            add("EXCERPT_HASH_INVALID", f"excerpt:{excerpt.source_excerpt_id}", "Excerpt hash mismatch")
        if excerpt.page_start > excerpt.page_end:
            add("EXCERPT_PAGE_INVALID", f"excerpt:{excerpt.source_excerpt_id}", "Page range reversed")
    for source_ref_id in refs:
        if excerpt_ref_counts[source_ref_id] != 1:
            add(
                "SOURCE_EXCERPT_COVERAGE_INVALID",
                f"source_ref:{source_ref_id}",
                f"Expected one excerpt, found {excerpt_ref_counts[source_ref_id]}",
            )

    for lab in pack.mini_labs:
        if lab.atom_id not in atoms:
            add("LAB_ATOM_UNKNOWN", f"lab:{lab.mini_lab_id}", lab.atom_id)
        if not set(lab.source_ref_ids) <= refs.keys():
            add("LAB_SOURCE_UNKNOWN", f"lab:{lab.mini_lab_id}", "Unknown source ref")
        plan = plans_by_atom.get(lab.atom_id)
        if plan is not None and not set(lab.source_ref_ids) <= set(plan.source_ref_ids):
            add(
                "LAB_SOURCE_PLAN_MISMATCH",
                f"lab:{lab.mini_lab_id}",
                "Mini Lab sources must stay within its owning AtomPlan",
            )
        for message in _lab_issues(lab):
            add("LAB_SPEC_INVALID", f"lab:{lab.mini_lab_id}", message)

    expected_node_ids = {pack.course.course_id, *modules.keys(), *atoms.keys()}
    covered_node_ids: set[str] = set()
    all_prerequisite_edges: list[tuple[str, str]] = []
    all_contains_edges: set[tuple[str, str]] = set()
    for mindmap in pack.mindmaps:
        node_ids = [node.id for node in mindmap.nodes]
        local_node_ids = set(node_ids)
        covered_node_ids.update(local_node_ids)
        if len(node_ids) != len(local_node_ids):
            add("MINDMAP_DUPLICATE_NODE", f"map:{mindmap.mind_map_id}", "Node IDs must be unique per map")
        if mindmap.root_node_id not in local_node_ids:
            add("MINDMAP_ROOT_UNKNOWN", f"map:{mindmap.mind_map_id}", mindmap.root_node_id)
        for node in mindmap.nodes:
            if node.type == "COURSE" and node.id != pack.course.course_id:
                add("MINDMAP_COURSE_NODE_INVALID", f"map:{mindmap.mind_map_id}", node.id)
            elif node.type == "MODULE" and node.id not in modules:
                add("MINDMAP_MODULE_NODE_INVALID", f"map:{mindmap.mind_map_id}", node.id)
            elif node.type == "ATOM" and (node.id not in atoms or node.atom_id != node.id):
                add("MINDMAP_ATOM_NODE_INVALID", f"map:{mindmap.mind_map_id}", node.id)
            elif node.type != "ATOM" and node.atom_id is not None:
                add("MINDMAP_NODE_ATOM_INVALID", f"map:{mindmap.mind_map_id}", node.id)
        edges = [(edge.from_, edge.to) for edge in mindmap.edges if edge.type == "PREREQUISITE"]
        for edge in mindmap.edges:
            if edge.from_ not in local_node_ids or edge.to not in local_node_ids:
                add("MINDMAP_EDGE_UNKNOWN", f"map:{mindmap.mind_map_id}", f"{edge.from_}->{edge.to}")
            if edge.type == "CONTAINS":
                all_contains_edges.add((edge.from_, edge.to))
        all_prerequisite_edges.extend(edges)
        if _cycle(node_ids, edges):
            add("MINDMAP_CYCLE", f"map:{mindmap.mind_map_id}", "Prerequisite cycle")

    for node_id in expected_node_ids - covered_node_ids:
        add("MINDMAP_NODE_MISSING", "mindmaps", node_id)
    required_contains = {(pack.course.course_id, module_id) for module_id in modules}
    required_contains.update((atom.module_id, atom.atom_id) for atom in pack.atoms)
    for source, target in required_contains - all_contains_edges:
        add("MINDMAP_CONTAINS_MISSING", "mindmaps", f"{source}->{target}")
    expected_prerequisites = {
        (prerequisite, atom.atom_id)
        for atom in pack.atoms
        for prerequisite in atom.prerequisite_ids
    }
    actual_prerequisites = set(all_prerequisite_edges)
    for source, target in expected_prerequisites - actual_prerequisites:
        add("MINDMAP_PREREQUISITE_MISSING", "mindmaps", f"{source}->{target}")
    for source, target in actual_prerequisites - expected_prerequisites:
        add("MINDMAP_PREREQUISITE_UNDECLARED", "mindmaps", f"{source}->{target}")
    if _cycle(set(atoms), list(actual_prerequisites)):
        add("MINDMAP_CYCLE", "mindmaps", "Combined prerequisite graph contains a cycle")

    return ValidationReport(
        valid=not any(item.severity == "ERROR" for item in issues),
        issue_count=len(issues),
        issues=issues,
        stats={
            "atoms": len(pack.atoms),
            "pairs": len(pack.learning_pairs),
            "questions": len(pack.questions),
            "labs": len(pack.mini_labs),
            "illustrations": len(pack.illustrations),
            "source_excerpts": len(pack.source_excerpts),
        },
    )
