from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from deeplock_content.generation import ModelResponse
from deeplock_content.generation.editorial_rewrite import (
    EDITORIAL_QUALITY_FEEDBACK_FILENAME,
    EDITORIAL_REWRITE_REPORT_FILENAME,
    EDITORIAL_REWRITE_SCHEMA_VERSION,
    EDITORIAL_REWRITE_SCOPE,
    _module_units,
    _parse_and_validate_module_output,
    learner_rewrite_module_hash,
    run_editorial_rewrite,
)
from deeplock_content.packaging.pack import (
    LearnerRewriteGateConfig,
    verify_learner_rewrite_gate,
)
from deeplock_content.settings import Settings
from deeplock_content.util import PROMPT_ROOT, read_json, sha256_json, write_json
from deeplock_content.validators import validate_pack


def _settings() -> Settings:
    return Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=4,
        generator_reasoning_effort="medium",
        review_reasoning_effort="high",
        generator_prompt_version="course-v1",
        review_prompt_version="reviewer-v1",
        max_repair_rounds=0,
        request_timeout_seconds=30,
        max_api_attempts=1,
        editorial_rewrite_enabled=True,
        editorial_model="gpt-5.5",
        editorial_reasoning_effort="high",
        editorial_prompt_version="learner-rewrite-v1",
    )


def test_learner_rewrite_prompt_bans_metadata_and_requires_teachable_distractors() -> None:
    prompt = (
        PROMPT_ROOT / "learner-rewrite-v1" / "system.txt"
    ).read_text(encoding="utf-8").casefold()

    for required in (
        "every learner-visible string",
        "option rationale",
        "teacher/presenter",
        "school year",
        "agenda/course outline",
        "url",
        "source filename",
        '"nguồn nêu"',
        "define each specialist term",
        "free-trial",
        "powershell/cmd",
        "bash/linux/macos",
        "credible, on-concept",
        "jokes, absurd/off-domain choices",
        "ui instructions",
        '"connected"',
        '"only ..."',
        '"initialize problem"',
        "same shell and path convention across atoms",
        "same technical axis",
        "learner_quality_feedback",
    ):
        assert required in prompt


def test_pack_defers_polish_for_pending_pass_one_but_enforces_it_after_approval(
    valid_pack,
) -> None:
    rough = _with_five_quizzes(valid_pack)
    rough.generator_prompt_version = "course-v2"
    for lesson in rough.lessons:
        payload = lesson.model_dump(mode="json")
        _rewrite_lesson(payload)
        replacement = type(lesson).model_validate(payload)
        lesson_index = rough.lessons.index(lesson)
        rough.lessons[lesson_index] = replacement
    rough.lessons[0].process_steps[0] = (
        "Kiểm tra free trial của nhà cung cấp trước khi chạy bước kỹ thuật này."
    )
    first_pair = rough.learning_pairs[0]
    first_question = next(
        item for item in rough.questions
        if item.question_id == first_pair.question_ids[0]
    )
    distractor = next(
        option for option in first_question.options
        if option.id != first_question.correct_option_id
    )
    distractor.text = "Tên experiment trong agenda"
    distractor.rationale = "Đây là metadata trình chiếu, không phải cơ chế kỹ thuật."
    for pair in rough.learning_pairs:
        pair.ai_review_status = "PENDING"

    intermediate = validate_pack(rough)

    assert intermediate.valid, [
        (item.code, item.path, item.message) for item in intermediate.issues
    ]

    approved = rough.model_copy(deep=True)
    for pair in approved.learning_pairs:
        pair.ai_review_status = "AI_APPROVED"
    strict = validate_pack(approved)
    strict_codes = {item.code for item in strict.issues}

    assert not strict.valid
    assert "LESSON_TEXT_IMPORT_ARTIFACT" in strict_codes
    assert "QUIZ_LEARNER_POLISH_FAILED" in strict_codes


def _with_five_quizzes(pack):
    updated = pack.model_copy(deep=True)
    for pair in updated.learning_pairs:
        owned = [
            item for item in updated.questions if item.pair_id == pair.pair_id
        ]
        additions = [
            (
                "direction",
                "Dấu của gradient giúp chọn hướng thay đổi tham số như thế nào?",
            ),
            (
                "step_size",
                "Learning rate kiểm soát đặc điểm nào của một bước tối ưu?",
            ),
        ]
        for suffix, stem in additions:
            question = owned[0].model_copy(deep=True)
            question.question_id = f"q_{pair.atom_id}_{suffix}"
            question.stem = stem
            updated.questions.append(question)
            pair.question_ids.append(question.question_id)
        basis = {
            "pair_id": pair.pair_id,
            "atom_id": pair.atom_id,
            "micro_lesson_id": pair.micro_lesson_id,
            "question_ids": pair.question_ids,
            "revision": pair.revision,
        }
        pair.content_hash = sha256_json(basis)
    return updated


def _with_ten_atoms(pack):
    updated = _with_five_quizzes(pack)
    module = updated.modules[0]
    full_lesson = next(
        item for item in updated.full_lessons
        if item.module_id == module.module_id
    )
    mindmap = updated.mindmaps[0]
    base_plan = module.atom_plans[0]
    base_atom = next(
        item for item in updated.atoms if item.atom_id == base_plan.atom_id
    )
    base_lesson = next(
        item for item in updated.lessons if item.atom_id == base_plan.atom_id
    )
    base_pair = next(
        item for item in updated.learning_pairs
        if item.atom_id == base_plan.atom_id
    )
    base_questions = [
        next(
            item for item in updated.questions
            if item.question_id == question_id
        )
        for question_id in base_pair.question_ids
    ]
    base_node = next(
        item for item in mindmap.nodes if item.id == base_plan.atom_id
    )
    base_contains = next(
        item for item in mindmap.edges
        if item.from_ == module.module_id and item.to == base_plan.atom_id
    )

    for order_index in range(len(module.atom_plans), 10):
        atom_id = f"atom_extra_{order_index:02d}"
        pair_id = f"pair_{atom_id}"
        lesson_id = f"lesson_{atom_id}"
        title = f"Optimization concept {order_index + 1}"

        plan = base_plan.model_copy(deep=True)
        plan.atom_id = atom_id
        plan.order_index = order_index
        plan.title = title
        plan.objective = f"Explain the role of {title} in one update."
        plan.prerequisite_ids = []
        module.atom_plans.append(plan)

        atom = base_atom.model_copy(deep=True)
        atom.atom_id = atom_id
        atom.order_index = order_index
        atom.title = title
        atom.prerequisite_ids = []
        updated.atoms.append(atom)

        lesson = base_lesson.model_copy(deep=True)
        lesson.lesson_id = lesson_id
        lesson.pair_id = pair_id
        lesson.atom_id = atom_id
        lesson.title = title
        lesson.learning_objective = (
            f"Explain how {title} influences a concrete model update."
        )
        lesson.prerequisite_ids = []
        updated.lessons.append(lesson)

        cloned_questions = []
        for question_index, original_question in enumerate(
            base_questions,
            start=1,
        ):
            question = original_question.model_copy(deep=True)
            question.question_id = f"q_{atom_id}_{question_index}"
            question.pair_id = pair_id
            question.atom_id = atom_id
            question.stem = f"{title}: {original_question.stem}"
            cloned_questions.append(question)
        updated.questions.extend(cloned_questions)

        pair = base_pair.model_copy(deep=True)
        pair.pair_id = pair_id
        pair.atom_id = atom_id
        pair.micro_lesson_id = lesson_id
        pair.question_ids = [item.question_id for item in cloned_questions]
        pair.revision = 1
        pair.ai_review_status = "PENDING"
        pair.content_hash = sha256_json({
            "pair_id": pair.pair_id,
            "atom_id": pair.atom_id,
            "micro_lesson_id": pair.micro_lesson_id,
            "question_ids": pair.question_ids,
            "revision": pair.revision,
        })
        updated.learning_pairs.append(pair)

        node = base_node.model_copy(deep=True)
        node.id = atom_id
        node.label = title
        node.atom_id = atom_id
        mindmap.nodes.append(node)
        contains = base_contains.model_copy(deep=True)
        contains.to = atom_id
        mindmap.edges.append(contains)
        full_lesson.atom_ids.append(atom_id)
    return updated


def _rewrite_lesson(lesson: dict[str, object]) -> None:
    topic = str(lesson["title"])
    lesson.update({
        "title": f"Cách {topic} dẫn đường cho cập nhật mô hình",
        "learning_objective": (
            f"Giải thích cách {topic} tham gia vào một bước cập nhật và áp dụng "
            "đúng quy trình cho tình huống đơn giản."
        ),
        "hook": (
            "Một mô hình chỉ cải thiện khi ta biết nên đổi tham số theo hướng nào "
            "và kiểm soát độ lớn của bước thay đổi."
        ),
        "intuition": (
            f"Hãy xem {topic} như tín hiệu dẫn đường cho quá trình tối ưu. Tín hiệu "
            "này liên kết trạng thái hiện tại của tham số với hướng làm loss thay đổi. "
            "Khi mục tiêu là giảm loss, ta đi theo hướng đối diện và dùng learning rate "
            "để điều chỉnh độ dài bước. Nhờ vậy, mỗi phép cập nhật có nguyên nhân rõ "
            "ràng thay vì chỉ là một công thức phải ghi nhớ."
        ),
        "tiny_example": {
            "input": "w = 2, gradient = 3 và learning rate = 0.1",
            "steps": [
                "Tính độ dịch chuyển: 0.1 nhân 3 bằng 0.3.",
                "Đi ngược gradient nên lấy 2 trừ 0.3.",
            ],
            "output": "Tham số mới là 1.7, tức đã dịch 0.3 theo hướng giảm loss.",
        },
        "process_steps": [
            "Xác định tham số và loss tại trạng thái hiện tại.",
            "Tính gradient để nhận hướng thay đổi nhanh nhất của loss.",
            "Chọn learning rate phù hợp để kiểm soát độ dài bước.",
            "Cập nhật theo hướng ngược gradient rồi đánh giá lại loss.",
        ],
        "common_mistake": (
            "Cộng gradient khi đang minimization sẽ đưa tham số về hướng làm loss "
            "tăng thay vì giảm."
        ),
        "takeaways": [
            "Gradient xác định hướng, còn learning rate xác định độ dài bước.",
            "Minimization cập nhật tham số theo hướng ngược gradient.",
        ],
        "recall_prompt": (
            "Hãy giải thích vai trò riêng của gradient và learning rate trong một bước cập nhật."
        ),
    })


class CoherentEditor:
    def __init__(
        self,
        *,
        drift_source: bool = False,
        unobservable_objective: bool = False,
        delay_seconds: float = 0,
    ):
        self.calls = 0
        self.payloads: list[dict[str, object]] = []
        self.drift_source = drift_source
        self.unobservable_objective = unobservable_objective
        self.delay_seconds = delay_seconds
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def structured(self, **kwargs):
        with self._lock:
            self.calls += 1
            call_number = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            payload = json.loads(kwargs["input_text"])
            with self._lock:
                self.payloads.append(payload)
            contents = [item["content"] for item in payload["ordered_atom_units"]]
            for content in contents:
                _rewrite_lesson(content["lesson"])
                if self.unobservable_objective:
                    content["lesson"]["learning_objective"] = (
                        "Understand the concept and its place in a model update."
                    )
            if self.drift_source:
                contents[0]["lesson"]["source_ref_ids"] = ["src_escaped"]
            return ModelResponse(
                {
                    "module_id": payload["module"]["module_id"],
                    "atom_contents": contents,
                },
                f"editorial_{call_number}",
                "cx/gpt-5.5",
            )
        finally:
            with self._lock:
                self.active -= 1


def test_grouped_cached_calls_rewrite_full_sequence_and_emit_bound_report(
    valid_pack,
    tmp_path: Path,
) -> None:
    source = _with_five_quizzes(valid_pack)
    before_lessons = {item.atom_id: item for item in source.lessons}
    before_pairs = {item.atom_id: item for item in source.learning_pairs}
    editor = CoherentEditor()

    updated, report = run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=_settings(),
        resume=True,
        adapter=editor,
    )

    assert editor.calls == 2
    payload = editor.payloads[0]
    assert payload["course"]["course_id"] == source.course.course_id
    assert payload["module_sequence_context"][0]["is_current"] is True
    assert "source_context" not in payload
    assert "context_text" not in json.dumps(payload, ensure_ascii=False)
    for lesson in updated.lessons:
        original = before_lessons[lesson.atom_id]
        assert lesson.title != original.title
        assert lesson.learning_objective != original.learning_objective
        assert lesson.lesson_id == original.lesson_id
        assert lesson.pair_id == original.pair_id
        assert lesson.source_ref_ids == original.source_ref_ids
        assert lesson.illustration_source_ref_ids == original.illustration_source_ref_ids
        assert lesson.prerequisite_ids == original.prerequisite_ids
    for pair in updated.learning_pairs:
        original = before_pairs[pair.atom_id]
        assert pair.question_ids == original.question_ids
        assert pair.revision == original.revision + 1
        assert pair.content_hash != original.content_hash
        assert pair.ai_review_status == "AI_APPROVED"
        assert len(pair.question_ids) == 5

    assert report["status"] == "EDITORIAL_REWRITE_APPROVED"
    assert report["review_scope"] == EDITORIAL_REWRITE_SCOPE
    assert report["source_correctness_reviewed"] is False
    assert report["editorial_schema_version"] == EDITORIAL_REWRITE_SCHEMA_VERSION
    assert report["approved_items"] == len(updated.modules)
    assert report["repaired_count"] == len(updated.modules)
    assert report["rejected_count"] == 0
    assert report["editorial_quality_feedback_present"] is False
    assert report["results"][0] == {
        "module_id": updated.modules[0].module_id,
        "verdict": "APPROVE",
        "issue_codes": [],
        "output_hash": learner_rewrite_module_hash(
            updated,
            updated.modules[0].module_id,
        ),
    }
    assert report["candidate_hash"] == sha256_json(
        updated.model_dump(mode="json", by_alias=True)
    )
    assert "reviewer_model" not in report
    verify_learner_rewrite_gate(
        updated,
        report,
        LearnerRewriteGateConfig(
            editorial_model=report["editorial_model"],
            editorial_configured_model=report["editorial_configured_model"],
            editorial_model_snapshot="cx/gpt-5.5",
            editorial_prompt_version=report["editorial_prompt_version"],
            editorial_prompt_sha256=report["editorial_prompt_sha256"],
            editorial_schema_version=report["editorial_schema_version"],
            editorial_schema_sha256=report["editorial_schema_sha256"],
            editorial_reasoning_effort=report["editorial_reasoning_effort"],
        ),
    )
    assert read_json(tmp_path / EDITORIAL_REWRITE_REPORT_FILENAME) == report

    replay = CoherentEditor()
    replayed, replay_report = run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=_settings(),
        resume=True,
        adapter=replay,
    )
    assert replay.calls == 0
    assert replayed == updated
    assert replay_report == report


def test_module_validation_reports_quality_failures_for_every_atom_in_group(
    valid_pack,
) -> None:
    source = _with_five_quizzes(valid_pack)
    module = source.modules[0]
    atom_ids = [item.atom_id for item in module.atom_plans[:2]]
    assert len(atom_ids) == 2
    contents = json.loads(json.dumps([
        item["content"]
        for item in _module_units(source, module.module_id, atom_ids)
    ]))
    for content in contents:
        _rewrite_lesson(content["lesson"])
    contents[0]["lesson"]["intuition"] = ("mô hình " * 500).strip()
    contents[1]["lesson"]["tiny_example"]["input"] = (
        "example " * 1000
    )

    with pytest.raises(ValueError) as raised:
        _parse_and_validate_module_output(
            {
                "module_id": module.module_id,
                "atom_contents": contents,
            },
            pack=source,
            module_id=module.module_id,
            target_atom_ids=atom_ids,
        )

    message = str(raised.value)
    assert "LESSON_TEXT_EXCESSIVE" in message
    assert "360-460 learner-visible words" in message
    assert "formula_blocks total<=50" in message
    assert "Current exact field counts are:" in message
    assert message.count("LESSON_TEXT_EXCESSIVE") >= 2
    assert message.index(atom_ids[0]) < message.index(atom_ids[1])


def test_advisory_polish_still_rejects_duplicate_quiz_options(valid_pack, monkeypatch):
    monkeypatch.setenv("DEEPLOCK_LEARNER_POLISH_ADVISORY", "1")
    source = _with_five_quizzes(valid_pack)
    module = source.modules[0]
    atom_ids = [module.atom_plans[0].atom_id]
    contents = json.loads(json.dumps([
        item["content"] for item in _module_units(source, module.module_id, atom_ids)
    ]))
    question = contents[0]["quiz_bundle"]["questions"][0]
    question["options"][3]["text"] = question["options"][0]["text"]
    with pytest.raises(ValueError, match="duplicate option text"):
        _parse_and_validate_module_output(
            {"module_id": module.module_id, "atom_contents": contents},
            pack=source, module_id=module.module_id, target_atom_ids=atom_ids,
        )


def test_ten_atoms_use_five_ordered_groups_and_one_global_worker_budget(
    valid_pack,
    tmp_path: Path,
) -> None:
    source = _with_ten_atoms(valid_pack)
    settings = replace(
        _settings(),
        concurrency=2,
        max_atoms_per_editorial_call=2,
    )
    editor = CoherentEditor(delay_seconds=0.03)

    updated, report = run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=settings,
        resume=True,
        adapter=editor,
    )

    expected_atom_ids = [
        item.atom_id for item in source.modules[0].atom_plans
    ]
    ordered_payloads = sorted(
        editor.payloads,
        key=lambda item: int(item["target_group"]["group_index"]),
    )
    assert editor.calls == 5
    assert editor.max_active == settings.concurrency
    assert editor.max_active <= settings.concurrency
    assert len(ordered_payloads) == 5
    assert {
        int(item["target_group"]["group_count"])
        for item in ordered_payloads
    } == {5}
    assert [
        atom_id
        for payload in ordered_payloads
        for atom_id in payload["target_group"]["ordered_atom_ids"]
    ] == expected_atom_ids
    for payload in ordered_payloads:
        target_atom_ids = payload["target_group"]["ordered_atom_ids"]
        assert len(target_atom_ids) == 2
        assert len(payload["full_module_roadmap"]) == 10
        assert [
            item["atom_plan"]["atom_id"]
            for item in payload["ordered_atom_units"]
        ] == target_atom_ids
        assert all("content" not in item for item in payload["full_module_roadmap"])

    assert ordered_payloads[0]["target_group"]["previous_boundary"] is None
    assert (
        ordered_payloads[0]["target_group"]["next_boundary"]["atom_id"]
        == expected_atom_ids[2]
    )
    assert (
        ordered_payloads[1]["target_group"]["previous_boundary"]["atom_id"]
        == expected_atom_ids[1]
    )
    assert [item.atom_id for item in updated.lessons] == expected_atom_ids
    assert all(
        item.ai_review_status == "AI_APPROVED"
        for item in updated.learning_pairs
    )
    assert report["approved_items"] == 1
    assert len(report["results"]) == 1

    with sqlite3.connect(tmp_path / "cache.sqlite3") as connection:
        step_types = [
            row[0]
            for row in connection.execute(
                "SELECT step_type FROM generation_steps ORDER BY step_type"
            )
        ]
    assert len(step_types) == 5
    assert len(set(step_types)) == 5
    assert all("GROUP:" in item and "ATOMS:" in item for item in step_types)


def test_quality_feedback_invalidates_only_its_target_group_cache(
    valid_pack,
    tmp_path: Path,
) -> None:
    source = _with_ten_atoms(valid_pack)
    settings = replace(
        _settings(),
        max_atoms_per_editorial_call=2,
    )
    baseline_editor = CoherentEditor()
    run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=settings,
        resume=True,
        adapter=baseline_editor,
    )
    assert baseline_editor.calls == 5
    assert all(
        "learner_quality_feedback" not in payload
        for payload in baseline_editor.payloads
    )

    target_atom_id = source.modules[0].atom_plans[0].atom_id
    feedback = {
        "schema_version": "1",
        "source_correctness_reviewed": False,
        "atoms": [
            {
                "atom_id": target_atom_id,
                "feedback": [
                    "Define the local term before its first use in this atom."
                ],
            }
        ],
    }
    write_json(tmp_path / EDITORIAL_QUALITY_FEEDBACK_FILENAME, feedback)

    repair_editor = CoherentEditor()
    _, report = run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=settings,
        resume=True,
        adapter=repair_editor,
    )

    assert repair_editor.calls == 1
    assert repair_editor.payloads[0]["target_group"]["ordered_atom_ids"][:1] == [
        target_atom_id
    ]
    assert repair_editor.payloads[0]["learner_quality_feedback"] == feedback["atoms"]
    assert report["editorial_quality_feedback_present"] is True
    assert report["editorial_quality_feedback_atom_count"] == 1
    assert report["editorial_quality_feedback_group_count"] == 1
    assert report["editorial_quality_feedback_sha256"] == sha256_json(feedback)


@pytest.mark.parametrize(
    "atoms,error_match",
    [
        (
            [{"atom_id": "atom_not_in_pack", "feedback": ["Fix it."]}],
            "unknown atom_id",
        ),
        (
            [{"atom_id": "__KNOWN__", "feedback": []}],
            "nonempty array",
        ),
        (
            [{"atom_id": "__KNOWN__", "feedback": [" "]}],
            "is empty",
        ),
    ],
)
def test_quality_feedback_schema_rejects_unknown_or_empty_items_before_api(
    valid_pack,
    tmp_path: Path,
    atoms: list[dict[str, object]],
    error_match: str,
) -> None:
    source = _with_five_quizzes(valid_pack)
    known_atom_id = source.atoms[0].atom_id
    normalized_atoms = [
        {
            **item,
            "atom_id": (
                known_atom_id if item["atom_id"] == "__KNOWN__" else item["atom_id"]
            ),
        }
        for item in atoms
    ]
    write_json(
        tmp_path / EDITORIAL_QUALITY_FEEDBACK_FILENAME,
        {
            "schema_version": "1",
            "source_correctness_reviewed": False,
            "atoms": normalized_atoms,
        },
    )
    editor = CoherentEditor()

    with pytest.raises(ValueError, match=error_match):
        run_editorial_rewrite(
            pack=source,
            workdir=tmp_path,
            settings=_settings(),
            resume=True,
            adapter=editor,
        )

    assert editor.calls == 0


def test_nonobservable_rewritten_objective_falls_back_to_validated_original(
    valid_pack,
    tmp_path: Path,
) -> None:
    source = _with_five_quizzes(valid_pack)
    expected: dict[str, str] = {}
    for lesson in source.lessons:
        lesson.learning_objective = (
            f"Explain how {lesson.title} influences one concrete model update."
        )
        expected[lesson.atom_id] = lesson.learning_objective
    editor = CoherentEditor(unobservable_objective=True)

    updated, _ = run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=_settings(),
        resume=True,
        adapter=editor,
    )

    assert editor.calls == 2
    assert {
        item.atom_id: item.learning_objective for item in updated.lessons
    } == expected

    replay = CoherentEditor(unobservable_objective=True)
    replayed, _ = run_editorial_rewrite(
        pack=source,
        workdir=tmp_path,
        settings=_settings(),
        resume=True,
        adapter=replay,
    )
    assert replay.calls == 0
    assert replayed == updated


def test_editorial_rewrite_rejects_source_ownership_drift_before_report(
    valid_pack,
    tmp_path: Path,
) -> None:
    source = _with_five_quizzes(valid_pack)
    editor = CoherentEditor(drift_source=True)

    with pytest.raises(ValueError, match="builder-owned fields"):
        run_editorial_rewrite(
            pack=source,
            workdir=tmp_path,
            settings=_settings(),
            resume=False,
            adapter=editor,
        )

    assert editor.calls == 2
    assert not (tmp_path / EDITORIAL_REWRITE_REPORT_FILENAME).exists()


def test_editorial_rewrite_requires_exactly_five_quizzes_before_api(
    valid_pack,
    tmp_path: Path,
) -> None:
    editor = CoherentEditor()

    with pytest.raises(ValueError, match="exactly five quizzes"):
        run_editorial_rewrite(
            pack=valid_pack,
            workdir=tmp_path,
            settings=_settings(),
            resume=False,
            adapter=editor,
        )

    assert editor.calls == 0
