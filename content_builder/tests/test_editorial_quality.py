from __future__ import annotations

import pytest

from deeplock_content.editorial import (
    has_observable_learning_objective,
    is_course_v2_prompt_version,
    issue_codes,
    learner_visible_lesson_word_breakdown,
    validate_lesson_editorial,
)
from deeplock_content.models import FormulaBlock, FormulaSymbol, Lesson, TinyExample


def _lesson(**updates: object) -> Lesson:
    values: dict[str, object] = {
        "lesson_id": "lesson_clean",
        "pair_id": "pair_clean",
        "atom_id": "atom_clean",
        "title": "Gradient descent: cập nhật tham số theo hướng giảm loss",
        "learning_objective": "Giải thích vai trò của gradient và áp dụng một bước cập nhật tham số.",
        "grounding_type": "SOURCE_GROUNDED",
        "prerequisite_ids": [],
        "hook": "Một mô hình chỉ học khi ta biết nên điều chỉnh tham số theo hướng nào.",
        "intuition": (
            "Gradient cho biết hướng loss tăng nhanh nhất, vì vậy ta đi theo hướng ngược lại "
            "để giảm loss từng bước."
        ),
        "tiny_example": TinyExample(
            input="w = 2, gradient = 3, learning rate = 0.1",
            steps=["Nhân learning rate với gradient: 0.1 × 3 = 0.3.", "Cập nhật w: 2 - 0.3."],
            output="w mới bằng 1.7",
        ),
        "process_steps": [
            "Tính loss trên dữ liệu hiện tại.",
            "Dùng backpropagation để lấy gradient.",
            "Cập nhật tham số theo hướng ngược gradient.",
        ],
        "formula_blocks": [
            FormulaBlock(
                latex=r"w_{t+1}=w_t-\eta\nabla L(w_t)",
                plain_text="Tham số mới bằng tham số cũ trừ learning rate nhân gradient.",
                symbols=[
                    FormulaSymbol(symbol="η", meaning="learning rate"),
                    FormulaSymbol(symbol="∇L", meaning="gradient của loss"),
                ],
            )
        ],
        "common_mistake": "Cộng gradient khi mục tiêu là minimization sẽ làm loss tăng.",
        "takeaways": ["Dấu trừ đưa tham số đi ngược hướng gradient."],
        "recall_prompt": "Vì sao quy tắc cập nhật dùng dấu trừ?",
        "estimated_seconds": 75,
        "source_ref_ids": ["src_clean"],
        "illustration_source_ref_ids": [],
    }
    values.update(updates)
    return Lesson.model_validate(values)


def test_clean_pedagogical_lesson_passes_and_technical_identifiers_are_preserved() -> None:
    lesson = _lesson(
        title="PyTorch DataLoader, ConvNeXt và List<T> trong training loop",
        intuition="DataLoader tạo batch; ConvNeXt xử lý tensor; page_size và slide_window là identifiers.",
    )

    assert validate_lesson_editorial(lesson) == []


def test_real_multilevel_paging_prose_is_not_misread_as_a_source_page_reference() -> None:
    lesson = _lesson(
        title="Phân trang đa cấp trong bộ nhớ ảo",
        intuition="Phân trang 2 cấp chia bảng trang thành các cấu trúc nhỏ hơn.",
    )

    assert validate_lesson_editorial(lesson) == []


def test_course_v2_coherent_micro_lesson_passes_strict_pedagogical_gate() -> None:
    lesson = _lesson(
        intuition=(
            "Gradient là độ dốc cục bộ của hàm loss: dấu của nó chỉ hướng tăng, còn độ lớn cho biết "
            "loss nhạy đến mức nào khi tham số thay đổi. Vì mục tiêu là giảm loss, thuật toán dịch tham số "
            "theo hướng đối diện. Learning rate kiểm soát độ dài bước để việc học không nhảy quá xa."
        ),
    )

    assert validate_lesson_editorial(lesson, pedagogical=True) == []


def test_course_v2_rejects_fragmentary_fact_card_but_course_v1_stays_compatible() -> None:
    lesson = _lesson(
        learning_objective="Biết gradient.",
        hook="Gradient là độ dốc.",
        intuition="Gradient cho biết hướng.",
        tiny_example=TinyExample(input="w=2", steps=["Trừ gradient."], output="w=1.7"),
        process_steps=["Cập nhật tham số."],
        common_mistake="Nhầm dấu.",
        recall_prompt="Gradient là gì?",
    )

    assert validate_lesson_editorial(lesson) == []
    strict_codes = issue_codes(validate_lesson_editorial(lesson, pedagogical=True))
    assert {
        "LESSON_OBJECTIVE_TOO_THIN",
        "LESSON_OBJECTIVE_NOT_OBSERVABLE",
        "LESSON_HOOK_TOO_THIN",
        "LESSON_INTUITION_TOO_THIN",
        "LESSON_EXAMPLE_STEPS_TOO_FEW",
        "LESSON_EXAMPLE_TOO_THIN",
        "LESSON_PROCESS_STEPS_TOO_FEW",
        "LESSON_COMMON_MISTAKE_TOO_THIN",
        "LESSON_TEXT_TOO_THIN",
    } <= strict_codes


def test_course_v2_allows_complete_lesson_spread_across_game_screens() -> None:
    lesson = _lesson(
        intuition=" ".join(["gradient"] * 285),
    )

    assert "LESSON_TEXT_EXCESSIVE" not in issue_codes(validate_lesson_editorial(lesson))
    assert "LESSON_TEXT_EXCESSIVE" not in issue_codes(
        validate_lesson_editorial(lesson, pedagogical=True)
    )


def test_course_v2_still_rejects_a_text_dump_beyond_the_whole_lesson_limit() -> None:
    lesson = _lesson(
        intuition=" ".join(["gradient"] * 610),
    )

    issues = validate_lesson_editorial(lesson, pedagogical=True)

    assert "LESSON_TEXT_EXCESSIVE" in issue_codes(issues)
    issue = next(item for item in issues if item.code == "LESSON_TEXT_EXCESSIVE")
    assert "450" in issue.message
    breakdown = learner_visible_lesson_word_breakdown(lesson)
    assert breakdown["intuition"] == 610
    assert sum(breakdown.values()) > 450


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("course-v2", True),
        ("course-v2.1", True),
        ("course-v2-pilot", True),
        ("course-v20", False),
        ("course-v2broken", False),
        ("course-v1", False),
    ],
)
def test_course_v2_prompt_version_matching_is_namespace_safe(
    version: str,
    expected: bool,
) -> None:
    assert is_course_v2_prompt_version(version) is expected


@pytest.mark.parametrize(
    "objective",
    [
        "Sau bài này, bạn có thể giải thích vì sao list cho phép thay đổi phần tử.",
        "Sau bài này, bạn có thể tính toán chỉ số âm tương ứng của một phần tử.",
        "Sau bài học này, bạn sẽ phân biệt list và tuple khi chọn cấu trúc dữ liệu.",
        "Sau khi học xong, người học có thể áp dụng slicing để lấy một dãy con.",
        "Bạn sẽ xác định chỉ số phù hợp để truy cập một phần tử trong list.",
        "Người học có thể tạo một list từ dữ liệu đầu vào.",
        "Người học có thể thay đổi phần tử bằng chỉ số.",
        "Người học có thể dùng index âm để lấy phần tử cuối.",
        "Người học có thể khởi tạo biến tích lũy trước vòng lặp.",
        "Người học có thể phát biểu bài toán Two Sum chính xác.",
        "Người học có thể cài đặt lời giải bằng dictionary.",
        "Người học có thể mô phỏng vòng lặp while trên một list.",
        "Người học có thể diễn đạt bài toán Two Sum bằng chỉ số.",
        "Người học có thể lần theo các cặp phần tử không trùng lặp.",
        "Người học có thể truy vết dictionary khi tìm thấy phần bù.",
        "Người học có thể trace thuật toán trên dữ liệu mẫu.",
        "Người học có thể theo dõi phiên bản dữ liệu bằng DVC.",
        "Người học có thể khôi phục một phiên bản dữ liệu đã ghi nhận.",
        "Người học có thể tái tạo pipeline từ các dependency đã khai báo.",
        "Người học có thể chạy dvc repro và quan sát stage được thực thi.",
        "Người học có thể chẩn đoán nguyên nhân một stage không chạy lại.",
        "Nêu được business requirement theo input-task-output.",
        "Sắp xếp đúng các giai đoạn của vòng đời Machine Learning.",
        "Chỉ ra vì sao data và code tách rời làm thí nghiệm khó tái lập.",
        "Sau bài này, bạn có thể thiết kế route nhận book_id từ URL.",
        "Sau bài này, bạn có thể mở đúng /docs để kiểm tra endpoint.",
        "Sau bài này, bạn có thể biến rubric thành checklist nộp bài.",
    ],
)
def test_course_v2_accepts_common_observable_objective_forms(objective: str) -> None:
    issues = validate_lesson_editorial(
        _lesson(learning_objective=objective),
        pedagogical=True,
    )

    assert "LESSON_OBJECTIVE_NOT_OBSERVABLE" not in issue_codes(issues)


def test_course_v2_objective_does_not_accept_verb_hidden_inside_a_noun_phrase() -> None:
    lesson = _lesson(
        learning_objective="Hiểu các thuộc tính của mô hình tạo sinh trong thực tế.",
        intuition=(
            "Mô hình tạo sinh học phân phối của dữ liệu để tạo mẫu mới có cấu trúc tương tự dữ liệu huấn luyện. "
            "Điểm quan trọng là đầu ra không phải bản sao máy móc: mô hình kết hợp các quy luật đã học để sinh "
            "một khả năng mới, nhưng chất lượng vẫn phụ thuộc dữ liệu và mục tiêu tối ưu."
        ),
    )

    assert "LESSON_OBJECTIVE_NOT_OBSERVABLE" in issue_codes(
        validate_lesson_editorial(lesson, pedagogical=True)
    )


def test_course_v2_rejects_relative_source_narration_only_in_strict_mode() -> None:
    lesson = _lesson(
        intuition=(
            "Slide tiếp theo trình bày cách gradient thay đổi tham số. Gradient là độ dốc cục bộ của hàm loss, "
            "vì vậy dấu của nó cho biết hướng tăng còn độ lớn cho biết mức độ nhạy. Muốn giảm loss, thuật toán "
            "dịch tham số theo hướng đối diện và dùng learning rate để giới hạn độ dài bước cập nhật."
        ),
    )

    assert "LESSON_PROSE_SOURCE_NARRATION" not in issue_codes(validate_lesson_editorial(lesson))
    assert "LESSON_PROSE_SOURCE_NARRATION" in issue_codes(
        validate_lesson_editorial(lesson, pedagogical=True)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hook", "Giảng viên: Nguyễn Văn A — hôm nay ta học cách theo dõi dữ liệu."),
        ("hook", "Năm học: 2025 — hôm nay ta học cách theo dõi dữ liệu."),
        ("hook", "Agenda: theo dõi dữ liệu, pipeline và thí nghiệm."),
        ("hook", "Đọc thêm tại https://example.com/dvc trước khi bắt đầu."),
        ("hook", "Mở Lecture_DVC.pdf trước khi bắt đầu thực hành."),
        ("common_mistake", "Nguồn nêu rằng nên chạy lại toàn bộ pipeline bằng tay."),
        ("common_mistake", "Free trial: đăng ký ngay trước khi chạy pipeline."),
    ],
)
def test_course_v2_rejects_import_and_commercial_metadata_anywhere(
    field: str,
    value: str,
) -> None:
    intermediate_issues = validate_lesson_editorial(
        _lesson(**{field: value}),
        pedagogical=True,
        learner_polish=False,
    )
    issues = validate_lesson_editorial(
        _lesson(**{field: value}),
        pedagogical=True,
        learner_polish=True,
    )

    assert "LESSON_TEXT_IMPORT_ARTIFACT" not in issue_codes(intermediate_issues)
    assert "LESSON_TEXT_IMPORT_ARTIFACT" in issue_codes(issues)
    assert any(
        issue.code == "LESSON_TEXT_IMPORT_ARTIFACT"
        and issue.path == f"lesson.{field}"
        for issue in issues
    )


def test_course_v2_allows_aws_pricing_calculator_as_technical_content() -> None:
    lesson = _lesson(
        intuition=(
            "AWS Pricing Calculator estimates the expected monthly cost before deployment, "
            "while Cost Explorer analyzes charges that have already occurred. This distinction "
            "helps select the correct financial tool for a planned architecture."
        ),
    )

    assert "LESSON_TEXT_IMPORT_ARTIFACT" not in issue_codes(
        validate_lesson_editorial(
            lesson,
            pedagogical=True,
            learner_polish=True,
        )
    )


def test_course_v2_still_rejects_generic_pricing_promotion() -> None:
    lesson = _lesson(
        intuition=(
            "Check pricing and upgrade plan now before continuing with this technical lesson."
        ),
    )

    assert "LESSON_TEXT_IMPORT_ARTIFACT" in issue_codes(
        validate_lesson_editorial(
            lesson,
            pedagogical=True,
            learner_polish=True,
        )
    )


def test_course_v2_rejects_mixed_windows_and_posix_shell_procedure() -> None:
    lesson = _lesson(
        process_steps=[
            "Trên terminal, chạy `source .venv/bin/activate` để bật môi trường.",
            r"Sau đó chạy `.\.venv\Scripts\Activate.ps1` trong cùng quy trình.",
        ],
    )

    assert "LESSON_SHELL_PLATFORM_MIXED" in issue_codes(
        validate_lesson_editorial(
            lesson,
            pedagogical=True,
            learner_polish=True,
        )
    )


def test_course_v2_allows_dockerfile_cmd_in_a_linux_procedure() -> None:
    lesson = _lesson(
        process_steps=[
            "Mount /var/run/docker.sock so Jenkins can call the Linux host.",
            "Dockerfile uses CMD uvicorn app:app on port 8000.",
        ],
    )

    assert "LESSON_SHELL_PLATFORM_MIXED" not in issue_codes(
        validate_lesson_editorial(
            lesson,
            pedagogical=True,
            learner_polish=True,
        )
    )


def test_bare_tinh_before_a_formula_is_an_observable_objective() -> None:
    assert has_observable_learning_objective(
        "Tính Δv=v(S∪{i})−v(S) cho một liên minh S cụ thể."
    )


def test_course_v2_still_rejects_cmd_slash_c_mixed_with_posix() -> None:
    lesson = _lesson(
        process_steps=[
            "Run `source .venv/bin/activate` in the POSIX shell.",
            "Then run `cmd /c build.bat` in the same procedure.",
        ],
    )

    assert "LESSON_SHELL_PLATFORM_MIXED" in issue_codes(
        validate_lesson_editorial(
            lesson,
            pedagogical=True,
            learner_polish=True,
        )
    )

def test_course_v2_rejects_duplicate_teaching_steps_and_takeaways() -> None:
    lesson = _lesson(
        intuition=(
            "Gradient là độ dốc cục bộ của hàm loss: dấu của nó chỉ hướng tăng, còn độ lớn cho biết "
            "loss nhạy đến mức nào khi tham số thay đổi. Vì mục tiêu là giảm loss, thuật toán dịch tham số "
            "theo hướng đối diện. Learning rate kiểm soát độ dài bước để việc học không nhảy quá xa."
        ),
        process_steps=["Tính gradient trên batch.", "Tính gradient trên batch."],
        takeaways=["Đi ngược gradient để giảm loss.", "Đi ngược gradient để giảm loss!"],
    )

    codes = issue_codes(validate_lesson_editorial(lesson, pedagogical=True))
    assert "LESSON_PROCESS_STEPS_DUPLICATE" in codes
    assert "LESSON_TAKEAWAYS_DUPLICATE" in codes


@pytest.mark.parametrize(
    ("title", "expected_code"),
    [
        ("Activation Functions · trang 25–30", "LESSON_TITLE_PAGE_LABEL"),
        ("Page 75 visual supplement (audited)", "LESSON_TITLE_INTERNAL_SUPPLEMENT"),
        ("Trang ảnh 47 · bản chép trực quan đã kiểm tra", "LESSON_TITLE_VISUAL_TRANSCRIPTION"),
        ("<Case Study>", "LESSON_TITLE_ANGLE_PLACEHOLDER"),
        ("<TODO: replace this heading>", "LESSON_TITLE_ANGLE_PLACEHOLDER"),
        ("Agenda", "LESSON_TITLE_GENERIC"),
        ("Learning Objectives", "LESSON_TITLE_GENERIC"),
    ],
)
def test_import_artifact_titles_fail(title: str, expected_code: str) -> None:
    issues = validate_lesson_editorial(_lesson(title=title))

    assert expected_code in issue_codes(issues)
    assert any(issue.code == expected_code and issue.path == "lesson.title" for issue in issues)


def test_generic_import_objective_fails() -> None:
    lesson = _lesson(
        learning_objective=(
            "Học đầy đủ nội dung Activation Functions · trang 25–30 từ bài giảng gốc."
        )
    )

    issues = validate_lesson_editorial(lesson)

    assert "LESSON_OBJECTIVE_SOURCE_TEMPLATE" in issue_codes(issues)
    assert any(issue.path == "lesson.learning_objective" for issue in issues)


@pytest.mark.parametrize(
    "objective",
    [
        (
            "Người học có thể mô tả vai trò của chuyển phiên bản dữ liệu "
            "trong nội dung đang học."
        ),
        "Người học có thể giải thích khái niệm Chuyển về Version 1.",
        "Sau bài này, bạn có thể giải thích khái niệm Data Versioning.",
    ],
)
def test_learner_polish_rejects_generic_role_objective_but_pass_one_accepts_it(
    objective: str,
) -> None:
    lesson = _lesson(learning_objective=objective)

    assert "LESSON_OBJECTIVE_GENERIC_ROLE_TEMPLATE" not in issue_codes(
        validate_lesson_editorial(lesson, learner_polish=False)
    )
    assert "LESSON_OBJECTIVE_GENERIC_ROLE_TEMPLATE" in issue_codes(
        validate_lesson_editorial(lesson, learner_polish=True)
    )


@pytest.mark.parametrize(
    "raw_filler",
    [
        "Data file không connected với source code nên khó tái lập thí nghiệm.",
        "Only model serving không đủ để quản lý vòng đời mô hình.",
        "Initialize Problem là bước đầu tiên của quy trình này.",
        "Thông điệp Dataset V2 only không mô tả đủ mốc huấn luyện.",
    ],
)
def test_learner_polish_rejects_clear_raw_english_filler(raw_filler: str) -> None:
    lesson = _lesson(intuition=raw_filler)

    assert "LESSON_TEXT_RAW_FILLER_ENGLISH" not in issue_codes(
        validate_lesson_editorial(lesson, learner_polish=False)
    )
    assert "LESSON_TEXT_RAW_FILLER_ENGLISH" in issue_codes(
        validate_lesson_editorial(lesson, learner_polish=True)
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_code", "expected_path"),
    [
        (
            "intuition",
            "Trang 12 mô tả cách gradient lan truyền qua mạng.",
            "LESSON_PROSE_PAGE_NARRATION",
            "lesson.intuition",
        ),
        (
            "hook",
            "Slide số 8 cho thấy một lỗi huấn luyện thường gặp.",
            "LESSON_PROSE_PAGE_NARRATION",
            "lesson.hook",
        ),
        (
            "common_mistake",
            "Bài giảng nêu rằng không nên cập nhật tham số trước backward.",
            "LESSON_PROSE_LECTURE_NARRATION",
            "lesson.common_mistake",
        ),
        (
            "recall_prompt",
            "Bài giảng gốc trình bày vai trò nào của learning rate?",
            "LESSON_PROSE_LECTURE_NARRATION",
            "lesson.recall_prompt",
        ),
    ],
)
def test_source_narration_in_learner_prose_fails(
    field: str,
    value: str,
    expected_code: str,
    expected_path: str,
) -> None:
    issues = validate_lesson_editorial(_lesson(**{field: value}))

    assert expected_code in issue_codes(issues)
    assert any(issue.code == expected_code and issue.path == expected_path for issue in issues)


def test_more_than_eight_process_steps_fails() -> None:
    issues = validate_lesson_editorial(
        _lesson(process_steps=[f"Thao tác học tập {index}." for index in range(1, 10)])
    )

    assert "LESSON_PROCESS_STEPS_EXCESSIVE" in issue_codes(issues)
    issue = next(item for item in issues if item.code == "LESSON_PROCESS_STEPS_EXCESSIVE")
    assert issue.path == "lesson.process_steps"
    assert "9" in issue.message


def test_excessively_long_lesson_fails() -> None:
    lesson = _lesson(
        intuition=" ".join(["gradient"] * 610),
    )

    issues = validate_lesson_editorial(lesson)

    assert "LESSON_TEXT_EXCESSIVE" in issue_codes(issues)


@pytest.mark.parametrize(
    "title",
    [
        "Sẽgây lỗi AttributeError",
        "Lỗi khi dùng từkhóa class",
        "Tìm vịtrí của phần tử",
        "Hàm trảvềsố phần tử nhỏ nhất",
    ],
)
def test_obvious_vietnamese_ocr_joined_tokens_fail(title: str) -> None:
    issues = validate_lesson_editorial(_lesson(title=title))

    assert "LESSON_TITLE_OCR_JOINED_TOKEN" in issue_codes(issues)
    assert any(issue.path == "lesson.title" for issue in issues)


def test_custom_root_path_is_reflected_in_issues() -> None:
    issues = validate_lesson_editorial(
        _lesson(title="Summary"),
        path="lessons[4]",
    )

    assert issues[0].path == "lessons[4].title"
