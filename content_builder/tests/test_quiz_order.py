from __future__ import annotations

import pytest

from deeplock_content.models import Question, QuizOption
from deeplock_content.quiz_order import (
    duplicate_option_pairs,
    option_texts_are_duplicates,
    rebalance_question_options,
    validate_quiz_editorial,
)


def _question(index: int) -> Question:
    return Question(
        question_id=f"q_{index}",
        pair_id="pair_1",
        atom_id="atom_1",
        stem="Stem",
        options=[
            QuizOption(id=option_id, text=f"payload-{option_id}", rationale=f"why-{option_id}")
            for option_id in ("A", "B", "C", "D")
        ],
        correct_option_id="A",
        explanation="Explanation",
        difficulty=1,
        bloom_level="RECALL",
        grounding_type="SOURCE_GROUNDED",
        source_ref_ids=["src_1"],
    )


def test_rebalance_question_options_is_balanced_semantic_and_idempotent():
    questions = [_question(index) for index in range(5)]

    rebalance_question_options(questions, "atom_1")

    assert set(question.correct_option_id for question in questions[:4]) == {"A", "B", "C", "D"}
    for question in questions:
        correct = next(option for option in question.options if option.id == question.correct_option_id)
        assert correct.text == "payload-A"
        assert correct.rationale == "why-A"
        assert [option.id for option in question.options] == ["A", "B", "C", "D"]
    once = [question.model_dump(mode="json") for question in questions]

    rebalance_question_options(questions, "atom_1")

    assert [question.model_dump(mode="json") for question in questions] == once


def test_literal_option_reference_fails_before_rebalance():
    questions = [_question(1)]
    questions[0].explanation = "K\u1ebft qu\u1ea3 kh\u1edbp v\u1edbi ph\u01b0\u01a1ng \u00e1n A."

    with pytest.raises(ValueError, match="literal option letter"):
        validate_quiz_editorial(questions)


def test_pep8_code_casing_is_not_collapsed_into_a_duplicate():
    question = _question(1)
    question.options[0].text = (
        "for user in users: ...; class InvalidUserError(Exception): ..."
    )
    question.options[3].text = (
        "for USER in users: ...; class invalidUserError(Exception): ..."
    )

    assert not option_texts_are_duplicates(
        question.options[0].text,
        question.options[3].text,
    )
    assert duplicate_option_pairs(question) == []
    validate_quiz_editorial([question])


def test_real_duplicate_cannot_hide_behind_ids_rationales_or_prose_casing():
    question = _question(1)
    question.options[3].text = question.options[0].text
    question.options[3].rationale = "A different rationale does not repair duplicate visible text."
    question.options[3].misconception_tag = "DISTRACTOR_D"

    with pytest.raises(ValueError, match=r"duplicate option text: A/D"):
        validate_quiz_editorial([question])

    assert option_texts_are_duplicates("Gradient descent", "GRADIENT   DESCENT")


@pytest.mark.parametrize("left,right", [
    ("cat = Cat()", "Cat = cat()"),
    ("cat.name()", "Cat.name()"),
    ("cat_color và get_name()", "Cat_color và Get_name()"),
])
def test_bare_python_expressions_preserve_case(left, right):
    assert not option_texts_are_duplicates(left, right)
    assert option_texts_are_duplicates(left, left)


@pytest.mark.parametrize("left,right", [
    ("userAccount", "UserAccount"),
    ("user_account", "USER_ACCOUNT"),
    ("InvalidUserError", "InvaliduserError"),
])
def test_naming_question_preserves_bare_identifier_case(left, right):
    question = _question(1)
    question.stem = "Tên nào đúng quy ước cho lớp Python?"
    question.options[0].text = left
    question.options[3].text = right
    assert duplicate_option_pairs(question) == []
    question.stem = "Which concept is correct?"
    assert duplicate_option_pairs(question) == [("A", "D")]
    question.stem = "Tên nào đúng quy ước cho lớp Python?"
    question.options[3].text = left
    assert duplicate_option_pairs(question) == [("A", "D")]


def test_near_duplicate_stems_fail_during_atom_generation_gate():
    questions = [_question(index) for index in range(1, 6)]
    questions[0].stem = "Với data = [4,5,6,7,8,9], data[1:-3] trả về gì?"
    questions[1].stem = "Trong slicing list[start:end:step], end có nghĩa gì?"
    questions[2].stem = "Với data = [4,5,6,7,8,9], data[:-3] trả về gì?"
    questions[3].stem = "Vì sao hướng slicing có thể tạo list rỗng?"
    questions[4].stem = "Backward index -4 trỏ tới phần tử nào?"

    with pytest.raises(ValueError, match=r"too similar: q_1/q_3"):
        validate_quiz_editorial(questions)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("stem", "Theo slide 12, lệnh nào tái tạo pipeline?"),
        ("stem", "According to page 12, which command reproduces the pipeline?"),
        ("explanation", "Nguồn nêu rằng dvc repro tái tạo pipeline."),
        ("explanation", "Source: Lecture_DVC.pdf"),
        ("option", "Giảng viên: Nguyễn Văn A"),
        ("option", "Teacher Nguyễn Văn A"),
        ("option", "Agenda của khóa học 2025"),
        ("rationale", "Xem https://example.com/lecture để biết thêm."),
        ("rationale", "Nội dung được chép từ Lecture_DVC.pdf."),
    ],
)
def test_quiz_rejects_source_or_slide_metadata_in_every_visible_surface(
    target: str,
    value: str,
) -> None:
    question = _question(1)
    if target == "stem":
        question.stem = value
    elif target == "explanation":
        question.explanation = value
    elif target == "option":
        question.options[1].text = value
    else:
        question.options[1].rationale = value

    # Pass one may contain rough slide/source residue; structural quiz checks
    # must accept and cache it for the dedicated learner rewrite.
    validate_quiz_editorial([question], learner_polish=False)
    with pytest.raises(ValueError, match=r"Question q_1 contains"):
        validate_quiz_editorial([question], learner_polish=True)


@pytest.mark.parametrize(
    ("text", "rationale"),
    [
        ("Vì máy tính thích thế", "Đây là một lựa chọn vui."),
        ("Pizza và banana", "Nội dung không liên quan đến DVC."),
        ("Bấm nút Tiếp tục", "Chỉ là đáp án nhiễu."),
        ("All of the above", "Chọn mọi phương án phía trên."),
    ],
)
def test_quiz_rejects_joke_off_domain_or_ui_distractors(
    text: str,
    rationale: str,
) -> None:
    question = _question(1)
    question.options[1].text = text
    question.options[1].rationale = rationale

    validate_quiz_editorial([question], learner_polish=False)
    with pytest.raises(ValueError, match=r"implausible/off-concept distractor B"):
        validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_plausible_misconception_using_khong_lien_quan() -> None:
    question = _question(1)
    question.stem = "Phát biểu nào đúng về quan hệ giữa data versioning và pipeline?"
    question.options[1].text = "Data versioning không liên quan pipeline"
    question.options[1].rationale = (
        "Sai: workflow ML nối dữ liệu, mã nguồn và pipeline."
    )

    validate_quiz_editorial([question], learner_polish=True)


@pytest.mark.parametrize(
    "value",
    [
        "Data file không connected với code",
        "Only Eval result",
        "Initialize Problem rồi mới version dữ liệu",
        "Dataset V2 only",
    ],
)
def test_quiz_rejects_clear_raw_english_filler(value: str) -> None:
    question = _question(1)
    question.options[1].text = value

    validate_quiz_editorial([question], learner_polish=False)
    with pytest.raises(ValueError, match=r"raw .+ filler"):
        validate_quiz_editorial([question], learner_polish=True)


@pytest.mark.parametrize(
    "value",
    [
        "Thay đổi giao diện trang chủ",
        "Tên tác giả",
        "URL tài liệu",
        "Cloud billing",
        "IDE thương mại",
        "Đăng nhập và phân quyền",
        "Nén database",
        "S3 luôn miễn phí và mở sẵn",
    ],
)
def test_quiz_rejects_superficial_distractor_off_the_stem_axis(value: str) -> None:
    question = _question(1)
    question.stem = "Cơ chế nào giúp DVC truy vết một phiên bản dữ liệu?"
    question.options[1].text = value

    with pytest.raises(ValueError, match=r"implausible/off-concept distractor B"):
        validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_ui_or_cloud_terms_when_the_stem_really_teaches_that_axis() -> None:
    ui_question = _question(1)
    ui_question.stem = "Yếu tố nào giữ giao diện người dùng nhất quán?"
    ui_question.options[1].text = "Thay đổi màu giao diện không theo design token"
    cloud_question = _question(2)
    cloud_question.stem = "Chi phí billing của dịch vụ được tính theo yếu tố nào?"
    cloud_question.options[1].text = "Cloud billing chỉ dựa trên tên tài khoản"

    validate_quiz_editorial([ui_question], learner_polish=True)
    validate_quiz_editorial([cloud_question], learner_polish=True)


def test_quiz_uses_correct_answer_to_resolve_a_compact_streamlit_stem_axis() -> None:
    question = _question(1)
    question.stem = "Cách ghép nào đúng giữa các thành phần trong ví dụ?"
    question.options[0].text = "Streamlit nhận input và hiển thị kết quả trong app"
    question.options[0].rationale = "Đúng; widget nối dữ liệu vào với màn hình kết quả."
    question.options[1].text = "Chỉ thêm một đường phân cách giao diện"
    question.options[1].rationale = (
        "Sai; đường phân cách chỉ tổ chức giao diện, không xử lý input."
    )
    question.explanation = (
        "Ứng dụng Streamlit cần widget, xử lý dữ liệu và phần hiển thị."
    )

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_same_family_streamlit_media_and_layout_misconceptions() -> None:
    media = _question(1)
    media.stem = "Muốn nhúng video, lệnh nào phù hợp?"
    media.options[0].text = "st.video('./clip.mp4')"
    media.options[0].rationale = "Đúng; hàm video tạo trình phát video."
    media.options[1].text = "st.logo('./clip.mp4')"
    media.options[1].rationale = "Sai; logo là loại media khác, không phải video."
    media.explanation = "Video cần thành phần media tương ứng."

    layout = _question(2)
    layout.stem = "Vì sao ví dụ gọi `st.columns(2)` hai lần?"
    layout.options[0].text = "Tạo hai hàng, mỗi hàng có hai metric"
    layout.options[0].rationale = "Đúng; bốn vị trí cột chứa bốn metric."
    layout.options[1].text = "Tạo một bảng dữ liệu"
    layout.options[1].rationale = "Sai; bảng không được tạo bằng cột giao diện."
    layout.explanation = "Hai lượt tạo cột xây bố cục cho bốn metric."

    validate_quiz_editorial([media], learner_polish=True)
    validate_quiz_editorial([layout], learner_polish=True)


def test_quiz_allows_frontend_contrast_in_a_backend_question() -> None:
    question = _question(1)
    question.stem = "Backend được mô tả tốt nhất bằng cụm nào?"
    question.options[0].text = "Logic code xử lý yêu cầu"
    question.options[0].rationale = "Đúng; backend nhận request và tạo response."
    question.options[1].text = "Nút bấm người dùng thao tác"
    question.options[1].rationale = "Nút bấm thuộc giao diện frontend, không phải backend."
    question.explanation = "Backend chứa logic xử lý request; frontend chứa giao diện."

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_endpoint_urls_when_the_question_teaches_a_url() -> None:
    question = _question(1)
    question.stem = "Server chạy ở localhost:8080. URL nào mở Swagger UI?"
    question.options[0].text = "http://localhost:8080/docs"
    question.options[0].rationale = "Đúng; /docs mở Swagger UI."
    question.options[1].text = "http://localhost:8080/redoc"
    question.options[1].rationale = "Đây là ReDoc, không phải Swagger UI."
    question.explanation = "Dùng URL /docs để mở tài liệu Swagger."

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_ui_output_misconception_for_a_gradio_slider() -> None:
    question = _question(1)
    question.stem = "Vì sao `slider.release()` tốt hơn `change()` cho hàm AI nặng?"
    question.options[0].text = "Nó chỉ chạy sau khi chọn xong"
    question.options[0].rationale = "Đúng; release tránh gọi hàm liên tục khi kéo."
    question.options[1].text = "Nó không cần outputs"
    question.options[1].rationale = "Sai; cập nhật giao diện vẫn cần outputs phù hợp."
    question.explanation = "release chạy khi người dùng thả Slider."

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_builder_mode_as_a_grafana_panel_misconception() -> None:
    question = _question(1)
    question.stem = "Which Grafana panel mode allows direct PromQL input?"
    question.options[0].text = "Code"
    question.options[0].rationale = "Code mode accepts a direct PromQL expression."
    question.options[1].text = "Builder mode"
    question.options[1].rationale = (
        "Builder dựng truy vấn bằng giao diện, không nhập PromQL trực tiếp."
    )
    question.explanation = "Grafana panel uses Code mode for direct PromQL."

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_a_table_of_contents_as_a_pdf_output_misconception() -> None:
    question = _question(1)
    question.stem = "Output trực tiếp của hệ thống RAG hỏi đáp PDF là gì?"
    question.options[0].text = "Một câu trả lời văn bản"
    question.options[0].rationale = "Đúng; hệ thống trả lời câu hỏi."
    question.options[1].text = "Một mục lục mới"
    question.options[1].rationale = "Tạo mục lục không phải output của pipeline hỏi đáp."
    question.explanation = "RAG trả về câu trả lời dựa trên tài liệu."

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_ui_misconception_when_comparing_chat_and_a_form() -> None:
    question = _question(1)
    question.stem = "Điểm nào phân biệt khung chat với biểu mẫu một lượt?"
    question.options[0].text = "Hỗ trợ nhiều lượt trao đổi"
    question.options[0].rationale = "Đúng; chat giữ luồng tương tác liên tục."
    question.options[1].text = "Không liên quan giao diện"
    question.options[1].rationale = "Sai; trọng tâm là mở rộng giao diện chat."
    question.explanation = "Khung chat hỗ trợ hỏi đáp nhiều lượt."

    validate_quiz_editorial([question], learner_polish=True)


def test_quiz_allows_streamlit_as_a_neighboring_rubric_criterion() -> None:
    question = _question(1)
    question.stem = "Tiêu chí nào thuộc phần kiến thức nền trước khi triển khai RAG?"
    question.options[0].text = "Sử dụng Google Colab"
    question.options[0].rationale = "Đúng; rubric liệt kê kiến thức dùng Colab."
    question.options[1].text = "Chỉ thiết kế giao diện Streamlit"
    question.options[1].rationale = (
        "Sai; Streamlit thuộc phần demo giao diện, không thay kiến thức nền."
    )
    question.explanation = "Rubric tách kiến thức nền khỏi phần demo Streamlit."

    validate_quiz_editorial([question], learner_polish=True)
