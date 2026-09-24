from __future__ import annotations

from deeplock_content.models import IllustrationAsset, MiniLab, MiniLabFallback
from deeplock_content.util import sha256_bytes
from deeplock_content.validators import validate_pack


def test_valid_pack_passes(valid_pack):
    report = validate_pack(valid_pack)
    assert report.valid, report.issues


def test_course_v2_pack_enables_strict_pedagogical_validation(valid_pack):
    strict = valid_pack.model_copy(deep=True)
    strict.generator_prompt_version = "course-v2"

    codes = {item.code for item in validate_pack(strict).issues}

    assert "LESSON_TEXT_TOO_THIN" in codes
    assert "LESSON_INTUITION_TOO_THIN" in codes


def test_duplicate_validator_preserves_case_sensitive_pep8_distinction(valid_pack):
    question = valid_pack.questions[0]
    question.options[0].text = (
        "for user in users: ...; class InvalidUserError(Exception): ..."
    )
    question.options[3].text = (
        "for USER in users: ...; class invalidUserError(Exception): ..."
    )

    assert "DUPLICATE_OPTION" not in issue_codes(valid_pack)

    question.options[3].text = question.options[0].text
    question.options[3].rationale = "Different rationale, same visible payload."
    assert "DUPLICATE_OPTION" in issue_codes(valid_pack)


def test_pair_atom_mismatch_is_rejected(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.questions[0].atom_id = "atom_other"
    report = validate_pack(broken)
    assert not report.valid
    assert "PAIR_ATOM_MISMATCH" in {item.code for item in report.issues}


def test_question_cannot_belong_to_two_pairs(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    second = broken.learning_pairs[0].model_copy(deep=True)
    second.pair_id = "pair_second"
    broken.learning_pairs.append(second)
    report = validate_pack(broken)
    assert "QUESTION_MULTIPLE_PAIRS" in {item.code for item in report.issues}


def test_excerpt_hash_and_offset_are_checked(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.source_excerpts[0].excerpt_hash = "sha256:" + "0" * 64
    broken.source_excerpts[0].highlight_end = 999999
    codes = {item.code for item in validate_pack(broken).issues}
    assert {"EXCERPT_HASH_INVALID", "EXCERPT_OFFSET_INVALID"} <= codes


def issue_codes(pack):
    return {item.code for item in validate_pack(pack).issues}


def test_lesson_source_must_exist(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.lessons[0].source_ref_ids = ["src_fake"]
    assert "LESSON_SOURCE_UNKNOWN" in issue_codes(broken)


def test_lesson_sources_must_exactly_match_atom_plan_in_order(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.lessons[0].source_ref_ids = []
    assert "LESSON_SOURCE_PLAN_MISMATCH" in issue_codes(broken)

    broken = valid_pack.model_copy(deep=True)
    original_ref = broken.source_refs[0]
    original_excerpt = broken.source_excerpts[0]
    second_ref = original_ref.model_copy(update={
        "source_ref_id": "src_second",
        "chunk_id": "chunk_second",
    })
    second_excerpt = original_excerpt.model_copy(update={
        "source_excerpt_id": "excerpt_second",
        "source_ref_id": second_ref.source_ref_id,
        "chunk_id": second_ref.chunk_id,
    })
    broken.source_refs.append(second_ref)
    broken.source_excerpts.append(second_excerpt)
    plan = broken.modules[0].atom_plans[0]
    plan.source_ref_ids = [original_ref.source_ref_id, second_ref.source_ref_id]
    broken.lessons[0].source_ref_ids = [second_ref.source_ref_id, original_ref.source_ref_id]
    assert "LESSON_SOURCE_PLAN_MISMATCH" in issue_codes(broken)


def test_question_and_lab_sources_cannot_escape_owning_atom_plan(curriculum_pack):
    first_plan, second_plan = curriculum_pack.modules[0].atom_plans[:2]
    foreign_source = second_plan.source_ref_ids[0]
    question = next(item for item in curriculum_pack.questions if item.atom_id == first_plan.atom_id)
    question.source_ref_ids = [foreign_source]
    assert "QUESTION_SOURCE_PLAN_MISMATCH" in issue_codes(curriculum_pack)

    question.source_ref_ids = list(first_plan.source_ref_ids)
    curriculum_pack.mini_labs = [MiniLab(
        mini_lab_id="lab_contract",
        atom_id=first_plan.atom_id,
        lab_type="LR_STEP_1D",
        spec={
            "initial_weight": 1.0,
            "gradient": 0.5,
            "learning_rate_min": 0.01,
            "learning_rate_max": 1.0,
            "learning_rate_default": 0.1,
        },
        fallback=MiniLabFallback(
            columns=["w"],
            rows=[["1.0"]],
            takeaways=["Đi ngược gradient."],
        ),
        source_ref_ids=[foreign_source],
    )]
    assert "LAB_SOURCE_PLAN_MISMATCH" in issue_codes(curriculum_pack)

def test_every_question_must_be_owned_by_exactly_one_pair(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    orphan_id = broken.learning_pairs[0].question_ids.pop()
    pair = broken.learning_pairs[0]
    basis = {
        "pair_id": pair.pair_id, "atom_id": pair.atom_id,
        "micro_lesson_id": pair.micro_lesson_id,
        "question_ids": pair.question_ids, "revision": pair.revision,
    }
    from deeplock_content.util import sha256_json
    pair.content_hash = sha256_json(basis)
    assert "QUESTION_PAIR_MISSING" in issue_codes(broken)
    assert orphan_id in {item.question_id for item in broken.questions}


def test_every_atom_needs_pair_outline_and_full_lesson_coverage(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    removed = broken.learning_pairs.pop()
    codes = issue_codes(broken)
    assert "ATOM_PAIR_COVERAGE_INVALID" in codes
    assert removed.atom_id in {item.atom_id for item in broken.atoms}

    broken = valid_pack.model_copy(deep=True)
    broken.full_lessons[0].atom_ids.pop()
    assert "ATOM_FULL_LESSON_MISSING" in issue_codes(broken)


def test_full_lesson_rejects_unknown_atom(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.full_lessons[0].atom_ids.append("atom_missing")
    assert "FULL_LESSON_ATOM_UNKNOWN" in issue_codes(broken)


def test_mindmap_requires_complete_nodes_and_valid_all_edge_types(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.mindmaps[0].nodes = [broken.mindmaps[0].nodes[0]]
    broken.mindmaps[0].edges = []
    codes = issue_codes(broken)
    assert {"MINDMAP_NODE_MISSING", "MINDMAP_CONTAINS_MISSING"} <= codes

    broken = valid_pack.model_copy(deep=True)
    broken.mindmaps[0].edges[0].to = "node_missing"
    assert "MINDMAP_EDGE_UNKNOWN" in issue_codes(broken)


def test_excerpt_must_match_its_exact_source_ref(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.source_excerpts[0].chunk_id = "chunk_from_another_ref"
    assert "EXCERPT_SOURCE_MISMATCH" in issue_codes(broken)


def test_pair_content_hash_is_recomputed(valid_pack):
    broken = valid_pack.model_copy(deep=True)
    broken.learning_pairs[0].content_hash = "sha256:" + "0" * 64
    assert "PAIR_CONTENT_HASH_INVALID" in issue_codes(broken)


def test_illustration_must_be_grounded_and_used_by_a_lesson(valid_pack):
    source_ref = valid_pack.source_refs[0]
    valid_pack.illustrations = [IllustrationAsset(
        illustration_id="illustration_orphan",
        source_ref_id=source_ref.source_ref_id,
        asset_member="illustration_orphan.png",
        mime_type="image/png",
        sha256=sha256_bytes(b"not packaged in validator"),
        byte_size=25,
        width=1,
        height=1,
        page_number=source_ref.page_start,
        caption="Minh họa.",
        alt_text="Một hình minh họa.",
    )]
    assert "ILLUSTRATION_UNUSED" in issue_codes(valid_pack)

    valid_pack.lessons[0].illustration_source_ref_ids = [source_ref.source_ref_id]
    assert validate_pack(valid_pack).valid


def test_lesson_cannot_reference_missing_or_unowned_illustration(valid_pack):
    valid_pack.lessons[0].illustration_source_ref_ids = ["src_missing"]
    codes = issue_codes(valid_pack)
    assert "LESSON_ILLUSTRATION_OUTSIDE_SOURCE" in codes
    assert "LESSON_ILLUSTRATION_SOURCE_UNKNOWN" in codes
    assert "LESSON_ILLUSTRATION_ASSET_MISSING" in codes
