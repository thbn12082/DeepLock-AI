from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import Question
from .editorial import learner_visible_import_artifact, learner_visible_raw_filler


OPTION_IDS = ("A", "B", "C", "D")
_OPTION_REFERENCE_RE = re.compile(
    "(?:\\b(?:\u0111\u00e1p\\s*\u00e1n|ph\u01b0\u01a1ng\\s*\u00e1n|l\u1ef1a\\s*ch\u1ecdn|option)\\s*[A-D]\\b)",
    re.IGNORECASE,
)
_PYTHON_CODE_RE = re.compile(
    r"(?:"
    r"\bfor\s+[A-Za-z_]\w*\s+in\s+[A-Za-z_]\w*\s*:"
    r"|\bclass\s+[A-Za-z_]\w*\s*(?:\([^\n;]*\))?\s*:"
    r"|\bdef\s+[A-Za-z_]\w*\s*\([^\n;]*\)\s*:"
    r"|`[^`\n]+`"
    r"|\b[A-Za-z_]\w*\s*=\s*[A-Za-z_]\w*(?:\(\))?"
    r"|\b[A-Za-z_]\w*\.[A-Za-z_]\w*(?:\(\))?"
    r"|\b[A-Za-z_]\w*\(\)"
    r")",
    re.IGNORECASE,
)
_IMPLAUSIBLE_DISTRACTOR_RE = re.compile(
    r"(?:\b(?:đáp\s+án\s+nhiễu|câu\s+trả\s+lời\s+sai|phương\s+án\s+sai|"
    r"off[- ]?topic|distractor|placeholder|lorem\s+ipsum|"
    r"để\s+cho\s+vui|just\s+for\s+fun|banana|pizza)\b|"
    r"\b(?:all\s+of\s+the\s+above|none\s+of\s+the\s+above|"
    r"tất\s+cả\s+(?:các\s+)?(?:đáp\s+án|phương\s+án)(?:\s+trên)?)\b|"
    r"\b(?:vì\s+)?(?:máy\s+tính|ai|mô\s+hình)\s+(?:thích|buồn|vui)\b|"
    r"\b(?:bấm|nhấn)\s+(?:nút\s+)?(?:tiếp\s+tục|next|submit)\b|"
    r"\bchọn\s+(?:đáp\s+án|nút)\s*[A-D]?\b)",
    re.IGNORECASE,
)
_SUPERFICIAL_DISTRACTOR_AXES: tuple[
    tuple[re.Pattern[str], re.Pattern[str]], ...
] = (
    (
        re.compile(
            r"\b(?:giao\s+diện(?:\s+(?:người\s+dùng|trang\s+chủ))?|"
            r"ui\s+design|logo(?:\s+và\s+màu)?|màu\s+giao\s+diện)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:giao\s+diện|ui|ux|front[- ]?end|back[- ]?end|api|trải\s+nghiệm\s+người\s+dùng|"
            r"logo|thiết\s+kế|streamlit|gradio|grafana|panel|slider|chat|biểu\s+mẫu|form|tiêu\s+chí|rubric|ứng\s+dụng|app|widget|"
            r"hiển\s+thị|màn\s+hình|bố\s+cục|layout|cột|columns?|metric|"
            r"media|video|audio|hình\s+ảnh|image)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"\b(?:tên\s+tác\s+giả|url\s+tài\s+liệu)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:tác\s+giả|trích\s+dẫn|thư\s+mục|tài\s+liệu|url|liên\s+kết)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"\b(?:cloud\s+billing|dịch\s+vụ\s+cloud\s+bắt\s+buộc\s+trả\s+phí|"
            r"ide\s+thương\s+mại)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:billing|chi\s+phí|giá|thanh\s+toán|ide|trình\s+soạn\s+thảo)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"\bđăng\s+nhập\s+và\s+phân\s+quyền\b", re.IGNORECASE),
        re.compile(
            r"\b(?:đăng\s+nhập|phân\s+quyền|xác\s+thực|bảo\s+mật|tài\s+khoản)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"\bnén\s+(?:cơ\s+sở\s+dữ\s+liệu|database)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:cơ\s+sở\s+dữ\s+liệu|database|lưu\s+trữ|nén)\b",
            re.IGNORECASE,
        ),
    ),
)
_ABSOLUTE_SUPERFICIAL_DISTRACTOR_RE = re.compile(
    r"\bs3\s+luôn\s+miễn\s+phí\s+và\s+mở\s+sẵn\b",
    re.IGNORECASE,
)
_TECHNICAL_URL_QUIZ_RE = re.compile(
    r"\b(?:url|đường\s+dẫn|endpoint|localhost|swagger|redoc|cổng|port)\b",
    re.IGNORECASE,
)
_DOCUMENT_STRUCTURE_QUIZ_RE = re.compile(
    r"\b(?:pdf|tài\s+liệu|document|mục\s+lục|table\s+of\s+contents)\b",
    re.IGNORECASE,
)


def _collapsed_option_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _case_sensitive_code_fragments(value: str) -> tuple[str, ...]:
    """Return explicit Python/inline-code fragments whose casing carries meaning."""

    return tuple(
        _collapsed_option_text(match.group(0))
        for match in _PYTHON_CODE_RE.finditer(value)
    )


def option_texts_are_duplicates(left: str, right: str, *, naming_context: bool = False) -> bool:
    """Compare option text without erasing case-sensitive code distinctions.

    Prose remains case-insensitive, so cosmetic casing cannot disguise a
    duplicate. Exact code duplicates remain duplicates too. Only an explicit
    Python or backtick-delimited code fragment may use casing as substantive
    quiz content (for example, a PEP 8 naming question).
    """

    left_collapsed = _collapsed_option_text(left)
    right_collapsed = _collapsed_option_text(right)
    if left_collapsed == right_collapsed:
        return True
    if left_collapsed.casefold() != right_collapsed.casefold():
        return False
    # Bare identifiers are meaningful case-sensitive answers only when the
    # question explicitly asks about naming. Ordinary prose still casefolds.
    if naming_context and all(
        re.fullmatch(r"[A-Za-z_]\w*(?:\(\))?(?:\s+(?:và|and)\s+[A-Za-z_]\w*(?:\(\))?)*", text)
        for text in (left_collapsed, right_collapsed)
    ):
        return False
    left_code = _case_sensitive_code_fragments(left_collapsed)
    right_code = _case_sensitive_code_fragments(right_collapsed)
    if left_code and right_code and left_code != right_code:
        return False
    return True


def duplicate_option_pairs(question: Question) -> list[tuple[str, str]]:
    """Return option-ID pairs whose visible payloads are duplicates."""

    duplicates: list[tuple[str, str]] = []
    naming_context = bool(re.search(
        r"\b(?:tên|naming|identifier|pascalcase|capwords|snake_case|camelcase)\b",
        question.stem, re.IGNORECASE,
    ))
    for left_index, left in enumerate(question.options):
        for right in question.options[left_index + 1 :]:
            if option_texts_are_duplicates(left.text, right.text, naming_context=naming_context):
                duplicates.append((left.id, right.id))
    return duplicates


def _stem_tokens(value: str) -> set[str]:
    return set(re.findall(r"[\wÀ-ỹ]+", value.casefold()))


def question_stem_similarity(left: str, right: str) -> float:
    """Return the token Jaccard score used by the pack-level quiz gate."""

    left_tokens = _stem_tokens(left)
    right_tokens = _stem_tokens(right)
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def duplicate_question_stem_pairs(
    questions: list[Question],
    *,
    threshold: float = 0.90,
) -> list[tuple[str, str]]:
    """Return question-ID pairs that test near-identical visible prompts."""

    duplicates: list[tuple[str, str]] = []
    for left_index, left in enumerate(questions):
        for right in questions[left_index + 1 :]:
            if question_stem_similarity(left.stem, right.stem) > threshold:
                duplicates.append((left.question_id, right.question_id))
    return duplicates


def _is_superficial_off_axis(question: Question, value: str) -> bool:
    """Reject clear topic-axis escapes while allowing genuine UI/cloud quizzes."""

    if _ABSOLUTE_SUPERFICIAL_DISTRACTOR_RE.search(value):
        return True
    correct_option = next(
        option for option in question.options
        if option.id == question.correct_option_id
    )
    # The stem can be deliberately compact (for example, "Cách ghép nào
    # đúng?").  Use the correct answer and explanation to establish the real
    # technical axis, but never use another distractor to justify itself.
    axis_context = " ".join((
        question.stem,
        correct_option.text,
        correct_option.rationale,
        question.explanation,
    ))
    for distractor_pattern, on_axis_stem_pattern in _SUPERFICIAL_DISTRACTOR_AXES:
        if (
            distractor_pattern.search(value)
            and not on_axis_stem_pattern.search(axis_context)
        ):
            return True
    return False


def validate_quiz_editorial(
    questions: list[Question],
    *,
    learner_polish: bool = False,
) -> None:
    """Reject quiz text that is unsafe after deterministic option rotation."""

    for question in questions:
        named_fields = [
            ("stem", question.stem),
            ("explanation", question.explanation),
        ]
        named_fields.extend(
            (f"option {option.id}", option.text) for option in question.options
        )
        named_fields.extend(
            (f"option {option.id} rationale", option.rationale)
            for option in question.options
        )
        fields = [value for _, value in named_fields]
        if any(_OPTION_REFERENCE_RE.search(value) for value in fields):
            raise ValueError(
                f"Question {question.question_id} refers to a literal option letter in user-visible text"
            )
        if learner_polish:
            for field_name, value in named_fields:
                artifact = learner_visible_import_artifact(value)
                if (
                    artifact == "source URL"
                    and _TECHNICAL_URL_QUIZ_RE.search(question.stem)
                ):
                    artifact = None
                if (
                    artifact == "agenda metadata"
                    and _DOCUMENT_STRUCTURE_QUIZ_RE.search(question.stem)
                ):
                    artifact = None
                if artifact is not None:
                    raise ValueError(
                        f"Question {question.question_id} contains {artifact} in {field_name}"
                    )
                raw_filler = learner_visible_raw_filler(value)
                if raw_filler is not None:
                    raise ValueError(
                        f"Question {question.question_id} contains {raw_filler} "
                        f"in {field_name}"
                    )
            for option in question.options:
                if option.id == question.correct_option_id:
                    continue
                if _IMPLAUSIBLE_DISTRACTOR_RE.search(option.text) or (
                    _IMPLAUSIBLE_DISTRACTOR_RE.search(option.rationale)
                ) or _is_superficial_off_axis(question, option.text) or (
                    _is_superficial_off_axis(question, option.rationale)
                ):
                    raise ValueError(
                        f"Question {question.question_id} has an implausible/off-concept "
                        f"distractor {option.id}"
                    )
        duplicates = duplicate_option_pairs(question)
        if duplicates:
            labels = ", ".join(f"{left}/{right}" for left, right in duplicates)
            raise ValueError(
                f"Question {question.question_id} has duplicate option text: {labels}"
            )
    duplicate_stems = duplicate_question_stem_pairs(questions)
    if duplicate_stems:
        labels = ", ".join(f"{left}/{right}" for left, right in duplicate_stems)
        raise ValueError(f"Quiz bundle has questions that are too similar: {labels}")


def balanced_target_ids(atom_id: str, count: int) -> list[str]:
    """Return a stable, per-atom answer-key order without bundle-level bias."""

    if count < 0:
        raise ValueError("Question count cannot be negative")
    order = sorted(
        range(4),
        key=lambda index: hashlib.sha256(f"{atom_id}:answer-order:{index}".encode()).digest(),
    )
    extra_offset = hashlib.sha256(f"{atom_id}:answer-extra".encode()).digest()[0] % 4
    targets: list[str] = []
    for question_index in range(count):
        if question_index < 4:
            target_index = order[question_index]
        else:
            target_index = order[(extra_offset + question_index - 4) % 4]
        targets.append(OPTION_IDS[target_index])
    return targets


def rebalance_question_options(questions: list[Question], atom_id: str) -> None:
    """Rotate complete option payloads so correct answers follow a stable balanced key."""

    targets = balanced_target_ids(atom_id, len(questions))
    for question, target_id in zip(questions, targets, strict=True):
        old_options = list(question.options)
        old_ids = [option.id for option in old_options]
        if old_ids != list(OPTION_IDS):
            raise ValueError(f"Question {question.question_id} options must be ordered A-D")
        correct_index = old_ids.index(question.correct_option_id)
        target_index = OPTION_IDS.index(target_id)
        rotation = (correct_index - target_index) % 4
        question.options = [
            old_options[(new_index + rotation) % 4].model_copy(update={"id": OPTION_IDS[new_index]})
            for new_index in range(4)
        ]
        question.correct_option_id = target_id
