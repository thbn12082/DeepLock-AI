from __future__ import annotations

import json
import re
import sqlite3

import pytest

from deeplock_content import generation
from deeplock_content.generation import ModelResponse
from deeplock_content.generation import builder
from deeplock_content.models import AtomContent, AtomPlan, Course, CourseOutline, ModuleOutline
from deeplock_content.settings import Settings
from deeplock_content.util import stable_id
from deeplock_content.validators import validate_pack


def _settings(
    *,
    max_api_attempts: int = 1,
    max_atoms_per_generation_call: int = 2,
) -> Settings:
    return Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="low",
        review_reasoning_effort="high",
        generator_prompt_version="course-v1",
        review_prompt_version="reviewer-v1",
        max_repair_rounds=2,
        request_timeout_seconds=30,
        max_api_attempts=max_api_attempts,
        max_atoms_per_generation_call=max_atoms_per_generation_call,
    )


def _atom_payload(plan: AtomPlan, content_id_seed: str) -> dict[str, object]:
    pair_id = stable_id("pair", content_id_seed, plan.atom_id)
    lesson_id = stable_id("lesson", pair_id)
    options = [
        {
            "id": option_id,
            "text": f"Nội dung {option_id.lower()} cho {plan.title}",
            "rationale": f"Giải thích {option_id.lower()} có căn cứ.",
            "misconception_tag": None if option_id == "A" else f"DISTRACTOR_{option_id}",
        }
        for option_id in ("A", "B", "C", "D")
    ]
    question_stems = [
        f"Which source definition establishes {plan.title}?",
        f"How should a learner apply the process for {plan.title}?",
        f"What boundary condition limits {plan.title}?",
        f"Which counterexample reveals misuse of {plan.title}?",
        f"When does a practical scenario require {plan.title}?",
    ]
    questions = [
        {
            "question_id": stable_id("q", pair_id, str(index)),
            "pair_id": pair_id,
            "atom_id": plan.atom_id,
            "stem": question_stems[index - 1],
            "options": options,
            "correct_option_id": "A",
            "explanation": f"Nguồn giải thích trực tiếp {plan.title}.",
            "difficulty": 2,
            "bloom_level": "UNDERSTAND",
            "grounding_type": "SOURCE_GROUNDED",
            "source_ref_ids": list(plan.source_ref_ids),
        }
        for index in range(1, 6)
    ]
    return {
        "lesson": {
            "lesson_id": lesson_id,
            "pair_id": pair_id,
            "atom_id": plan.atom_id,
            "title": plan.title,
            "learning_objective": plan.objective,
            "grounding_type": "SOURCE_GROUNDED",
            "prerequisite_ids": list(plan.prerequisite_ids),
            "hook": f"Bắt đầu với {plan.title}.",
            "intuition": f"Trực giác đầy đủ cho {plan.title} từ nguồn.",
            "tiny_example": {
                "input": "Dữ liệu đầu vào",
                "steps": ["Đọc nguồn", "Áp dụng khái niệm"],
                "output": "Kết quả có căn cứ",
            },
            "process_steps": ["Đọc định nghĩa", "Kiểm tra ví dụ"],
            "formula_blocks": [],
            "common_mistake": "Bỏ qua điều kiện trong nguồn.",
            "takeaways": [f"Ghi nhớ đầy đủ {plan.title}."],
            "recall_prompt": f"Hãy nhắc lại {plan.title}.",
            "estimated_seconds": 60,
            "source_ref_ids": list(plan.source_ref_ids),
            "illustration_source_ref_ids": [],
        },
        "quiz_bundle": {
            "questions": questions,
            "insufficient_evidence_items": [],
        },
        "mini_lab": None,
    }


def _requested_atom_ids(input_text: str) -> list[str]:
    group_match = re.search(
        r"REQUIRED_ATOM_BUNDLES_IN_ORDER: (.+)\nReturn atom_contents",
        input_text,
    )
    if group_match:
        return [
            item["atom_plan"]["atom_id"]
            for item in json.loads(group_match.group(1))
        ]
    atom_match = re.search(r"ATOM_PLAN: (.+)\nREQUIRED_PAIR_ID", input_text)
    assert atom_match is not None
    return [json.loads(atom_match.group(1))["atom_id"]]


def test_normalizer_restores_mechanical_ids_and_only_replaces_invalid_objective(
    corpus,
):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plan = AtomPlan(
        atom_id="atom_list_create",
        order_index=0,
        title="Khởi tạo Python List",
        objective=(
            "Người học có thể tạo một List bằng cú pháp [1, 2], giải thích các đặc tính "
            "có thứ tự và cho phép phần tử trùng lặp."
        ),
        prerequisite_ids=[],
        source_ref_ids=[source_ref_id],
    )
    content = AtomContent.model_validate(_atom_payload(plan, corpus.source_hash))
    content.lesson.lesson_id = "model_lesson"
    content.lesson.pair_id = "model_pair"
    content.lesson.atom_id = "model_atom"
    content.lesson.learning_objective = (
        "Hiểu các thuộc tính của mô hình tạo sinh trong thực tế."
    )
    for index, question in enumerate(content.quiz_bundle.questions):
        question.question_id = f"model_question_{index}"
        question.pair_id = "model_pair"
        question.atom_id = "model_atom"

    builder._normalize_generated_atom(
        content,
        plan,
        corpus,
        content_id_seed=corpus.source_hash,
        pedagogical=True,
    )

    pair_id = stable_id("pair", corpus.source_hash, plan.atom_id)
    assert content.lesson.lesson_id == stable_id("lesson", pair_id)
    assert content.lesson.pair_id == pair_id
    assert content.lesson.atom_id == plan.atom_id
    assert content.lesson.learning_objective == (
        "Người học có thể tạo một List bằng cú pháp [1, 2]."
    )
    assert [question.question_id for question in content.quiz_bundle.questions] == [
        stable_id("q", pair_id, str(index)) for index in range(1, 6)
    ]
    assert all(
        question.pair_id == pair_id and question.atom_id == plan.atom_id
        for question in content.quiz_bundle.questions
    )


def test_normalizer_preserves_valid_objective_and_substantive_quiz_gates(corpus):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plan = AtomPlan(
        atom_id="atom_list_index",
        order_index=0,
        title="Index âm trong List",
        objective="Người học có thể giải thích index âm và áp dụng nó trong nhiều tình huống khác nhau.",
        prerequisite_ids=[],
        source_ref_ids=[source_ref_id],
    )
    content = AtomContent.model_validate(_atom_payload(plan, corpus.source_hash))
    generated_objective = "Người học có thể dùng index âm để truy cập phần tử cuối List."
    content.lesson.learning_objective = generated_objective
    content.quiz_bundle.questions[0].options[3].text = (
        content.quiz_bundle.questions[0].options[0].text
    )

    with pytest.raises(ValueError, match="duplicate option text"):
        builder._normalize_generated_atom(
            content,
            plan,
            corpus,
            content_id_seed=corpus.source_hash,
            pedagogical=True,
        )

    assert content.lesson.learning_objective == generated_objective


def test_normalizer_upgrades_cached_title_only_objective_from_specific_plan(corpus):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plan = AtomPlan(
        atom_id="atom_two_sum_trace",
        order_index=0,
        title="Mô phỏng dictionary khi tìm thấy các cặp Two Sum",
        objective=(
            "Người học có thể trace các bước tạo kết quả của thuật toán dictionary: "
            "khi gặp phần bù đã lưu thì thêm cặp chỉ số tương ứng vào ans."
        ),
        prerequisite_ids=[],
        source_ref_ids=[source_ref_id],
    )
    content = AtomContent.model_validate(_atom_payload(plan, corpus.source_hash))
    content.lesson.learning_objective = (
        "Người học có thể giải thích khái niệm "
        "Mô phỏng dictionary khi tìm thấy các cặp Two Sum."
    )

    builder._normalize_generated_atom(
        content,
        plan,
        corpus,
        content_id_seed=corpus.source_hash,
        pedagogical=True,
    )

    assert content.lesson.learning_objective == (
        "Người học có thể trace các bước tạo kết quả của thuật toán dictionary."
    )


def test_normalizer_preserves_specific_vague_plan_instead_of_title_fallback(corpus):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plan = AtomPlan(
        atom_id="atom_dvc_repro",
        order_index=0,
        title="Tái tạo pipeline bằng DVC",
        objective=(
            "Hiểu cách DVC theo dõi dependency và khôi phục đầu ra bằng dvc repro."
        ),
        prerequisite_ids=[],
        source_ref_ids=[source_ref_id],
    )
    content = AtomContent.model_validate(_atom_payload(plan, corpus.source_hash))
    content.lesson.learning_objective = (
        "Người học có thể giải thích khái niệm Tái tạo pipeline bằng DVC."
    )

    builder._normalize_generated_atom(
        content,
        plan,
        corpus,
        content_id_seed=corpus.source_hash,
        pedagogical=True,
    )

    assert content.lesson.learning_objective == (
        "Người học có thể mô tả cách DVC theo dõi dependency và khôi phục đầu ra "
        "bằng dvc repro."
    )
    assert "giải thích khái niệm" not in content.lesson.learning_objective.casefold()


class _GroupAdapter:
    def __init__(
        self,
        plans: dict[str, AtomPlan],
        content_id_seed: str,
        *,
        failing_atom_ids: tuple[str, ...] | None = ("atom_2", "atom_3"),
    ):
        self.plans = plans
        self.content_id_seed = content_id_seed
        self.failing_atom_ids = failing_atom_ids
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.input_texts: list[str] = []

    def structured(self, **kwargs):
        input_text = str(kwargs["input_text"])
        atom_ids = _requested_atom_ids(input_text)
        schema_name = str(kwargs["schema_name"])
        self.calls.append((schema_name, tuple(atom_ids)))
        self.input_texts.append(input_text)
        if self.failing_atom_ids is not None and tuple(atom_ids) == self.failing_atom_ids:
            raise TimeoutError("fixture group timeout")
        payloads = [
            _atom_payload(self.plans[atom_id], self.content_id_seed)
            for atom_id in atom_ids
        ]
        data = {"atom_contents": payloads} if len(atom_ids) > 1 else payloads[0]
        return ModelResponse(
            data=data,
            response_id=f"response_{len(self.calls)}",
            model_snapshot="cx/gpt-5.5",
        )


def _install_outline_fixture(corpus, monkeypatch, *, atom_count: int = 5):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plans = [
        AtomPlan(
            atom_id=f"atom_{index}",
            order_index=index,
            title=f"Atom {index}",
            objective=f"Hiá»ƒu Ä‘áº§y Ä‘á»§ atom {index}.",
            prerequisite_ids=[] if index == 0 else [f"atom_{index - 1}"],
            source_ref_ids=[source_ref_id],
        )
        for index in range(atom_count)
    ]
    module = ModuleOutline(
        module_id="module_fixture",
        order_index=0,
        title="Module fixture",
        objective="Kiá»ƒm tra chia nhÃ³m.",
        atom_plans=plans,
    )
    outline = CourseOutline(
        course=Course(
            course_id="course_fixture",
            title="Course fixture",
            description="Fixture",
            language="vi",
        ),
        modules=[module],
    )
    monkeypatch.setattr(
        builder,
        "_curriculum_outline",
        lambda **_kwargs: (outline, ["outline-snapshot"]),
    )
    curriculum = {
        "stable_content_id_seed": corpus.source_hash,
        "batch_atoms_by_module": True,
        "modules": [{
            "module_id": module.module_id,
            "output": corpus.documents[0].filename,
        }],
    }
    return plans, curriculum


def test_content_canary_starts_one_cache_miss_then_resumes_without_regenerating_it(
    corpus,
    tmp_path,
    monkeypatch,
):
    plans, curriculum = _install_outline_fixture(corpus, monkeypatch)
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
        failing_atom_ids=None,
    )

    with pytest.raises(generation.ContentGenerationStagePaused) as paused:
        generation.generate_candidate(
            corpus=corpus,
            workdir=tmp_path,
            settings=_settings(max_api_attempts=1),
            model="gpt-5.5",
            prompt_version="course-v1",
            resume=True,
            curriculum=curriculum,
            adapter=adapter,
            content_request_limit=1,
        )

    assert paused.value.requests_started == 1
    assert paused.value.atoms_cached == 2
    assert adapter.calls == [
        ("module_atom_contents_2_group_1", ("atom_0", "atom_1"))
    ]

    resumed = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(max_api_attempts=1),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )

    assert len(resumed.atoms) == 5
    assert adapter.calls.count(
        ("module_atom_contents_2_group_1", ("atom_0", "atom_1"))
    ) == 1
    assert adapter.calls.count(
        ("module_atom_contents_2_group_2", ("atom_2", "atom_3"))
    ) == 1
    assert adapter.calls.count(("atom_content", ("atom_4",))) == 1


def test_content_canary_failure_never_enters_retry_wave_or_atom_fallback(
    corpus,
    tmp_path,
    monkeypatch,
):
    plans, curriculum = _install_outline_fixture(corpus, monkeypatch)
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
        failing_atom_ids=("atom_0", "atom_1"),
    )

    with pytest.raises(generation.ContentGenerationStageFailed, match="fallback were skipped"):
        generation.generate_candidate(
            corpus=corpus,
            workdir=tmp_path,
            settings=_settings(max_api_attempts=1),
            model="gpt-5.5",
            prompt_version="course-v1",
            resume=True,
            curriculum=curriculum,
            adapter=adapter,
            content_request_limit=1,
        )

    assert adapter.calls == [
        ("module_atom_contents_2_group_1", ("atom_0", "atom_1"))
    ]


def test_atom_width_one_uses_single_atom_transport_with_exactly_five_quizzes(
    corpus,
    tmp_path,
    monkeypatch,
):
    plans, curriculum = _install_outline_fixture(corpus, monkeypatch, atom_count=1)
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
        failing_atom_ids=None,
    )

    pack = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(max_atoms_per_generation_call=1),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )

    # batch_atoms_by_module still enters run_group, whose one-item branch must
    # delegate to the normal single-atom schema rather than a hidden batch.
    assert adapter.calls == [("atom_content", ("atom_0",))]
    assert len(pack.atoms) == 1
    assert len(pack.learning_pairs) == 1
    assert len(pack.learning_pairs[0].question_ids) == 5
    assert len(pack.questions) == 5


def test_failed_atom_width_one_is_not_retried_as_its_own_fallback(
    corpus,
    tmp_path,
    monkeypatch,
):
    plans, curriculum = _install_outline_fixture(corpus, monkeypatch, atom_count=3)
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
        failing_atom_ids=("atom_1",),
    )

    with pytest.raises(RuntimeError, match="Atom generation failed after retry wave: atom_1"):
        generation.generate_candidate(
            corpus=corpus,
            workdir=tmp_path,
            settings=_settings(
                max_atoms_per_generation_call=1,
                max_api_attempts=1,
            ),
            model="gpt-5.5",
            prompt_version="course-v1",
            resume=True,
            curriculum=curriculum,
            adapter=adapter,
        )

    assert adapter.calls.count(("atom_content", ("atom_0",))) == 1
    assert adapter.calls.count(("atom_content", ("atom_1",))) == 1
    assert adapter.calls.count(("atom_content", ("atom_2",))) == 1


def test_normal_group_retry_wave_respects_one_attempt_setting(
    corpus,
    tmp_path,
    monkeypatch,
):
    plans, curriculum = _install_outline_fixture(corpus, monkeypatch)
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
    )

    pack = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(max_api_attempts=1),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )

    assert len(pack.atoms) == 5
    assert adapter.calls.count(
        ("module_atom_contents_2_group_2", ("atom_2", "atom_3"))
    ) == 1
    assert adapter.calls.count(("atom_content", ("atom_2",))) == 1
    assert adapter.calls.count(("atom_content", ("atom_3",))) == 1


@pytest.mark.parametrize("max_api_attempts", [1, 2])
def test_normal_individual_retry_waves_match_attempt_setting(
    corpus,
    tmp_path,
    monkeypatch,
    max_api_attempts,
):
    plans, curriculum = _install_outline_fixture(corpus, monkeypatch, atom_count=3)
    curriculum["batch_atoms_by_module"] = False
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
        failing_atom_ids=("atom_1",),
    )

    with pytest.raises(RuntimeError, match="Atom generation failed after retry wave"):
        generation.generate_candidate(
            corpus=corpus,
            workdir=tmp_path,
            settings=_settings(max_api_attempts=max_api_attempts),
            model="gpt-5.5",
            prompt_version="course-v1",
            resume=True,
            curriculum=curriculum,
            adapter=adapter,
        )

    assert adapter.calls.count(("atom_content", ("atom_1",))) == max_api_attempts


def test_failed_group_falls_back_only_its_atoms_and_successes_resume_from_cache(
    corpus,
    tmp_path,
    monkeypatch,
):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plans = [
        AtomPlan(
            atom_id=f"atom_{index}",
            order_index=index,
            title=f"Atom {index}",
            objective=f"Hiểu đầy đủ atom {index}.",
            prerequisite_ids=[] if index == 0 else [f"atom_{index - 1}"],
            source_ref_ids=[source_ref_id],
        )
        for index in range(5)
    ]
    module = ModuleOutline(
        module_id="module_fixture",
        order_index=0,
        title="Module fixture",
        objective="Kiểm tra chia nhóm.",
        atom_plans=plans,
    )
    outline = CourseOutline(
        course=Course(
            course_id="course_fixture",
            title="Course fixture",
            description="Fixture",
            language="vi",
        ),
        modules=[module],
    )
    monkeypatch.setattr(
        builder,
        "_curriculum_outline",
        lambda **_kwargs: (outline, ["outline-snapshot"]),
    )
    curriculum = {
        "stable_content_id_seed": corpus.source_hash,
        "batch_atoms_by_module": True,
        "modules": [{
            "module_id": module.module_id,
            "output": corpus.documents[0].filename,
        }],
    }
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
    )

    first = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(max_api_attempts=2),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )
    first_calls = list(adapter.calls)
    second = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(max_api_attempts=2),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )

    assert len(first.atoms) == len(second.atoms) == 5
    assert len(first.questions) == len(second.questions) == 25
    assert first_calls.count(("module_atom_contents_2_group_1", ("atom_0", "atom_1"))) == 1
    assert first_calls.count(("module_atom_contents_2_group_2", ("atom_2", "atom_3"))) == 2
    assert first_calls.count(("atom_content", ("atom_4",))) == 1
    assert first_calls.count(("atom_content", ("atom_2",))) == 1
    assert first_calls.count(("atom_content", ("atom_3",))) == 1
    # On resume, the successful first group, one-atom tail, and individual
    # fallbacks are cache hits. Only the still-failing grouped request retries.
    assert adapter.calls.count(
        ("module_atom_contents_2_group_1", ("atom_0", "atom_1"))
    ) == 1
    assert adapter.calls.count(
        ("module_atom_contents_2_group_2", ("atom_2", "atom_3"))
    ) == 4
    assert adapter.calls.count(("atom_content", ("atom_4",))) == 1
    assert adapter.calls.count(("atom_content", ("atom_2",))) == 1
    assert adapter.calls.count(("atom_content", ("atom_3",))) == 1
    with sqlite3.connect(tmp_path / "cache.sqlite3") as connection:
        group_step_types = {
            row[0]
            for row in connection.execute(
                "SELECT step_type FROM generation_steps "
                "WHERE step_type LIKE 'GENERATE_MODULE_CONTENT_GROUP:%'"
            )
        }
    assert any(
        ":LIMIT:2:INDEX:1-OF-3:SIZE:2:ATOMS:" in step_type
        for step_type in group_step_types
    )
    assert any(
        ":LIMIT:2:INDEX:2-OF-3:SIZE:2:ATOMS:" in step_type
        for step_type in group_step_types
    )


def test_stale_ready_group_duplicate_is_revalidated_repaired_and_passes_pack_gate(
    corpus,
    tmp_path,
    monkeypatch,
):
    source_ref_id = corpus.chunks[0].source_ref.source_ref_id
    plans = [
        AtomPlan(
            atom_id=f"cached_atom_{index}",
            order_index=index,
            title=f"Cached atom {index}",
            objective=f"Understand cached atom {index} from the source.",
            prerequisite_ids=[] if index == 0 else [f"cached_atom_{index - 1}"],
            source_ref_ids=[source_ref_id],
        )
        for index in range(2)
    ]
    module = ModuleOutline(
        module_id="cached_module",
        order_index=0,
        title="Cached module",
        objective="Exercise stale grouped generation cache validation.",
        atom_plans=plans,
    )
    outline = CourseOutline(
        course=Course(
            course_id="cached_course",
            title="Cached course",
            description="Cached group regression fixture",
            language="vi",
        ),
        modules=[module],
    )
    monkeypatch.setattr(
        builder,
        "_curriculum_outline",
        lambda **_kwargs: (outline, ["outline-snapshot"]),
    )
    curriculum = {
        "stable_content_id_seed": corpus.source_hash,
        "batch_atoms_by_module": True,
        "modules": [{
            "module_id": module.module_id,
            "output": corpus.documents[0].filename,
        }],
    }
    adapter = _GroupAdapter(
        {plan.atom_id: plan for plan in plans},
        corpus.source_hash,
    )

    first = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )
    first_question = first.questions[0]
    first_correct_text = next(
        option.text
        for option in first_question.options
        if option.id == first_question.correct_option_id
    )

    # Simulate a READY grouped response written by the older gate: its visible
    # D payload is an exact semantic duplicate of A even though its metadata is
    # different. The cache key and status intentionally remain unchanged.
    with sqlite3.connect(tmp_path / "cache.sqlite3") as connection:
        row = connection.execute(
            "SELECT cache_key, output_json FROM generation_steps "
            "WHERE step_type LIKE 'GENERATE_MODULE_CONTENT_GROUP:%' "
            "AND status='READY'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[1])
        stale_question = payload["atom_contents"][0]["quiz_bundle"]["questions"][0]
        stale_question["options"][3]["text"] = stale_question["options"][0]["text"]
        stale_question["options"][3]["rationale"] = (
            "Different hidden metadata must not disguise duplicate visible text."
        )
        stale_question["options"][3]["misconception_tag"] = "STALE_DUPLICATE"
        connection.execute(
            "UPDATE generation_steps SET output_json=? WHERE cache_key=?",
            (json.dumps(payload, ensure_ascii=False), row[0]),
        )

    adapter.calls.clear()
    adapter.input_texts.clear()
    repaired = generation.generate_candidate(
        corpus=corpus,
        workdir=tmp_path,
        settings=_settings(),
        model="gpt-5.5",
        prompt_version="course-v1",
        resume=True,
        curriculum=curriculum,
        adapter=adapter,
    )

    assert adapter.calls == [
        ("module_atom_contents_2_group_1", ("cached_atom_0", "cached_atom_1")),
    ]
    assert "CORRECTION REQUIRED" in adapter.input_texts[0]
    assert "duplicate option text" in adapter.input_texts[0]
    repaired_question = repaired.questions[0]
    repaired_correct_text = next(
        option.text
        for option in repaired_question.options
        if option.id == repaired_question.correct_option_id
    )
    assert repaired_correct_text == first_correct_text
    report = validate_pack(repaired)
    assert report.valid, report.issues
