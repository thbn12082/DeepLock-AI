from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator

from pydantic import BaseModel, ConfigDict

from .models import Lesson


class EditorialIssue(BaseModel):
    """One deterministic, user-visible lesson quality problem."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    path: str
    message: str


_TITLE_PAGE_LABEL_RE = re.compile(r"\s*·\s*trang\b", re.IGNORECASE)
_TITLE_PAGE_SUFFIX_RE = re.compile(r"\s*·\s*trang\b.*$", re.IGNORECASE)
_INTERNAL_VISUAL_SUPPLEMENT_RE = re.compile(
    r"\bpage\s+\d+\s+visual\s+supplement\s*\(\s*audited\s*\)",
    re.IGNORECASE,
)
_VISUAL_TRANSCRIPTION_LABEL_RE = re.compile(r"\bbản\s+chép\s+trực\s+quan\b", re.IGNORECASE)
_ANGLE_WRAPPED_TITLE_RE = re.compile(
    r"^\s*<\s*(?P<label>[^<>]{1,80}?)\s*>\s*(?:·.*)?$",
    re.IGNORECASE,
)
_GENERIC_OBJECTIVE_RE = re.compile(
    r"^\s*học\s+đầy\s+đủ\s+nội\s+dung\b.*\btừ\s+bài\s+giảng\s+gốc\b",
    re.IGNORECASE | re.DOTALL,
)
_GENERIC_ROLE_OBJECTIVE_RE = re.compile(
    r"(?:"
    r"\b(?:mô\s+tả|giải\s+thích)\s+vai\s+trò\s+của\b.+"
    r"\btrong\s+nội\s+dung\s+đang\s+học\b"
    r"|^\s*(?:(?:sau\s+bài(?:\s+học)?\s+này)[,:]?\s*)?"
    r"(?:(?:người\s+học|bạn)\s+)?(?:sẽ\s+|có\s+thể\s+)?"
    r"(?:mô\s+tả|giải\s+thích)\s+khái\s+niệm\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_RAW_FILLER_ENGLISH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "raw 'connected' filler",
        re.compile(
            r"\b(?:data\s+files?|code|dataset|dữ\s+liệu)\s+"
            r"(?:không\s+|chưa\s+|được\s+)?connected\b",
            re.IGNORECASE,
        ),
    ),
    (
        "raw 'Only ...' filler",
        re.compile(
            r"\bonly\s+(?:model\s+serving|eval(?:uation)?\s+result|model\s+name)\b"
            r"|\bdataset\s+v\d+\s+only\b",
            re.IGNORECASE,
        ),
    ),
    (
        "raw 'Initialize Problem' filler",
        re.compile(r"\binitialize\s+problem\b", re.IGNORECASE),
    ),
)
_SOURCE_PAGE_NARRATION_RE = re.compile(
    # "phân trang 2 cấp" is a real operating-systems concept, not source
    # narration. Keep that common technical construction out of this gate.
    r"(?<!phân\s)\b(?:trang|slide|page)\s*(?:số\s*)?\d+\b",
    re.IGNORECASE,
)
_LECTURE_NARRATION_RE = re.compile(
    r"\bbài\s+giảng(?:\s+gốc)?\s+(?:nêu|trình\s+bày)\b",
    re.IGNORECASE,
)
_COURSE_V2_SOURCE_NARRATION_RE = re.compile(
    r"(?:"
    r"\b(?:trang|slide)\s+(?:này|tiếp\s+theo|trước\s+đó|đầu\s+tiên|cuối\s+cùng)\s+"
    r"(?:cho\s+thấy|trình\s+bày|mô\s+tả|nêu|giới\s+thiệu|đề\s+cập)\b"
    r"|\b(?:bài\s+giảng|tài\s+liệu|nguồn)(?:\s+gốc)?\s+"
    r"(?:cho\s+biết|mô\s+tả|đề\s+cập|nêu|trình\s+bày|giới\s+thiệu)\b"
    r")",
    re.IGNORECASE,
)
_LEARNER_IMPORT_ARTIFACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "source narration",
        re.compile(
            r"(?:\b(?:nguồn|source)\s*[:：]|\b(?:nguồn|bài\s+giảng|tài\s+liệu)"
            r"(?:\s+gốc)?\s+(?:cho\s+biết|mô\s+tả|đề\s+cập|nêu|trình\s+bày|"
            r"giới\s+thiệu)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "presenter metadata",
        re.compile(
            r"\b(?:giảng\s+viên|người\s+trình\s+bày|presenter|instructor|teacher)"
            r"\s*(?:[:：-]|(?:là\s+)?[A-ZÀ-ỸĐ][\wÀ-ỹĐđ]+(?:\s+"
            r"[A-ZÀ-ỸĐ][\wÀ-ỹĐđ]+)+)",
            re.IGNORECASE,
        ),
    ),
    (
        "course-year metadata",
        re.compile(
            r"(?:\b(?:năm\s+học|niên\s+khóa|khóa\s+học|course)\s*[:：-]?\s*"
            r"(?:19|20)\d{2}\b|\b(?:ai\s+vi(?:e|ệ)t\s+nam|all\s+in\s+one)\s+"
            r"(?:course\s+)?(?:19|20)\d{2}\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "agenda metadata",
        re.compile(
            r"\b(?:agenda|table\s+of\s+contents|course\s+outline|mục\s+lục|"
            r"nội\s+dung\s+buổi\s+học|learning\s+objectives?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "source URL",
        re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE),
    ),
    (
        "source filename",
        re.compile(
            r"(?<![\w.])[^\s/\\<>:\"|?*]{1,80}\.(?:pdf|pptx?|docx?)(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "slide/template furniture",
        re.compile(
            r"\b(?:click\s+to\s+add\s+(?:title|text)|lorem\s+ipsum|thank\s+you|"
            r"quiz\s+time|free\s+trial|dùng\s+thử\s+miễn\s+phí|buy\s+now|mua\s+ngay|"
            r"sign\s+up|đăng\s+ký\s+ngay|upgrade\s+(?:now|plan)|pricing(?!\s+calculator\b)|"
            r"sponsored\s+by)\b",
            re.IGNORECASE,
        ),
    ),
)
_POSIX_SHELL_RE = re.compile(
    r"(?:^|[\s;`])(?:source|sudo|export)\s+|(?:^|[\s=`])/(?:home|tmp|usr|var)/|"
    r"\.(?:/)[\w.-]+",
    re.IGNORECASE | re.MULTILINE,
)
_WINDOWS_SHELL_RE = re.compile(
    r"(?:\bpowershell\b|\bcmd(?:\.exe|\s+/[ck])\b|\$env:[A-Za-z_]|"
    r"(?:^|[\s=`])[A-Za-z]:\\|\\Scripts\\|Activate\.ps1\b)",
    re.IGNORECASE | re.MULTILINE,
)
_LETTER_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_VISIBLE_WORD_RE = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)
_MAX_VISIBLE_LESSON_WORDS = 520
_COURSE_V2_MIN_VISIBLE_LESSON_WORDS = 140
# This is a whole-lesson budget, not a one-screen budget. Android presents a
# lesson as several focused pages (and losslessly paginates long intuition
# prose), so 360 here rejected otherwise coherent lessons merely because their
# structured fields add up across pages. Keep a 520-word safety
# ceiling while course-v2's field-shape gates and prompt enforce concise pages.
_COURSE_V2_MAX_VISIBLE_LESSON_WORDS = 520
_COURSE_V2_MIN_OBJECTIVE_WORDS = 6
_COURSE_V2_MIN_HOOK_WORDS = 10
_COURSE_V2_MIN_INTUITION_WORDS = 30
_COURSE_V2_MIN_EXAMPLE_WORDS = 20
_COURSE_V2_MIN_COMMON_MISTAKE_WORDS = 8
_COURSE_V2_MIN_RECALL_PROMPT_WORDS = 5
_OBSERVABLE_OBJECTIVE_RE = re.compile(
    r"^\s*(?:"
    r"(?:sau\s+(?:bài(?:\s+học)?\s+này|khi\s+học\s+xong)[,:]?\s*)?"
    r"(?:(?:người\s+học|bạn)\s+)?(?:(?:sẽ|có\s+thể)\s+)?(?:"
    r"giải\s+thích|phân\s+biệt|áp\s+dụng|vận\s+dụng|tính(?:\s+(?:được|toán))?|mô\s+tả|"
    r"mô\s+phỏng|diễn\s+đạt|lần\s+theo|truy\s+vết|trace|"
    r"nhận\s+diện|xác\s+định|triển\s+khai|cấu\s+hình|so\s+sánh|"
    r"đánh\s+giá|phân\s+tích|thực\s+hiện|xây\s+dựng|kiểm\s+tra|"
    r"lựa\s+chọn|chọn|sử\s+dụng|dùng|dự\s+đoán|chứng\s+minh|"
    r"khởi\s+tạo|tạo(?:\s+(?:ra|được))?|thay\s+đổi|phát\s+biểu|cài\s+đặt|viết|"
    r"liệt\s+kê|nêu|chỉ\s+ra|sắp\s+xếp|trình\s+bày|diễn\s+giải|đọc|ghi|lưu|thêm|xóa|sửa|chạy|"
    r"tái\s+tạo|khôi\s+phục|phục\s+hồi|theo\s+dõi|quan\s+sát|đối\s+chiếu|"
    r"liên\s+kết|tổ\s+chức|quản\s+lý|thiết\s+kế|thiết\s+lập|mở|biến|chẩn\s+đoán|khắc\s+phục|"
    r"thử\s+nghiệm|đo|ước\s+lượng|tối\s+ưu|phân\s+rã|tổng\s+hợp|"
    r"chuẩn\s+hóa|chuyển\s+đổi|giải\s+quyết|suy\s+luận"
    r")|"
    r"(?:(?:learner|you)\s+(?:can\s+)?)?(?:"
    r"explain|distinguish|apply|calculate|describe|identify|deploy|"
    r"configure|compare|evaluate|analyze|implement|build|verify|use|list|"
    r"read|write|run|track|restore|reproduce|monitor|inspect|troubleshoot|"
    r"diagnose|solve"
    r"))\b",
    re.IGNORECASE,
)
_COURSE_V2_PROMPT_VERSION_RE = re.compile(r"^course-v2(?:$|[-.])")


def _normalized_heading(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\wÀ-ỹ]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def is_course_v2_prompt_version(value: str) -> bool:
    """Return whether a prompt version belongs to the course-v2 contract."""

    return bool(_COURSE_V2_PROMPT_VERSION_RE.match(value.strip()))


def has_observable_learning_objective(value: str) -> bool:
    """Return whether an objective begins with an explicit learner action."""

    return bool(_OBSERVABLE_OBJECTIVE_RE.search(value))


def learner_visible_import_artifact(value: str) -> str | None:
    """Describe imported metadata/template copy that must not reach a learner."""

    normalized = unicodedata.normalize("NFKC", value)
    if _SOURCE_PAGE_NARRATION_RE.search(normalized):
        return "source page/slide number"
    for label, pattern in _LEARNER_IMPORT_ARTIFACT_PATTERNS:
        if pattern.search(normalized):
            return label
    return None


def learner_visible_raw_filler(value: str) -> str | None:
    """Describe clear untranslated filler embedded in learner-facing prose."""

    normalized = unicodedata.normalize("NFKC", value)
    for label, pattern in _RAW_FILLER_ENGLISH_PATTERNS:
        if pattern.search(normalized):
            return label
    return None


def mixes_shell_platforms(values: Iterable[str]) -> bool:
    """Return whether one learner unit mixes clear POSIX and Windows shell idioms."""

    combined = "\n".join(unicodedata.normalize("NFKC", value) for value in values)
    return bool(_POSIX_SHELL_RE.search(combined) and _WINDOWS_SHELL_RE.search(combined))


_GENERIC_HEADINGS = {
    "agenda",
    "bài tập",
    "case study",
    "checklist",
    "code",
    "conclusion",
    "contents",
    "course outline",
    "cảm ơn",
    "example",
    "exercise",
    "giới thiệu",
    "introduction",
    "kết luận",
    "learning objective",
    "learning objectives",
    "lesson",
    "lecture",
    "model",
    "models",
    "mục tiêu bài học",
    "mục tiêu học tập",
    "nội dung",
    "overview",
    "practice",
    "quiz",
    "quiz time",
    "quizz",
    "reference",
    "references",
    "scenario",
    "section",
    "summary",
    "table of contents",
    "thank you",
    "tham khảo",
    "topic",
    "tài liệu tham khảo",
    "tóm tắt",
    "tổng quan",
    "ví dụ",
    # Course-cover text observed in the imported lecture corpus.
    "ai viet nam course 2025",
    "ai việt nam course 2025",
    "ai viet nam aio2025",
    "all in one course 2025",
}

_ANGLE_PLACEHOLDER_HEADINGS = _GENERIC_HEADINGS | {
    "content",
    "heading",
    "placeholder",
    "tba",
    "tbd",
    "title",
    "todo",
}

# Vietnamese normally separates syllables with spaces. Restrict joined-token
# detection to common word boundaries containing Vietnamese diacritics, so
# technical identifiers such as DataLoader, ConvNeXt, ResNet50, List<T>, and
# snake_case are not classified as OCR damage.
_OCR_LEFT_WORDS = {
    "bài",
    "các",
    "cho",
    "cuối",
    "của",
    "duyệt",
    "dữ",
    "giá",
    "gây",
    "học",
    "in",
    "khi",
    "là",
    "lấy",
    "lớn",
    "mô",
    "mỗi",
    "một",
    "mục",
    "này",
    "nếu",
    "nhỏ",
    "những",
    "nội",
    "nối",
    "ngoài",
    "ở",
    "phần",
    "phương",
    "qua",
    "ra",
    "số",
    "sẽ",
    "thay",
    "theo",
    "thì",
    "thứ",
    "trong",
    "trả",
    "trên",
    "trị",
    "tạo",
    "từ",
    "tử",
    "và",
    "về",
    "vị",
    "với",
    "xóa",
    "đầu",
    "để",
    "đến",
    "được",
    "đổi",
}
_OCR_RIGHT_WORDS = {
    "bài",
    "bước",
    "các",
    "cho",
    "cùng",
    "câu",
    "có",
    "của",
    "danh",
    "dữ",
    "gây",
    "giảng",
    "giá",
    "hàm",
    "khi",
    "khóa",
    "không",
    "kết",
    "lý",
    "lớn",
    "lỗi",
    "lần",
    "lấy",
    "lớp",
    "mô",
    "mỗi",
    "một",
    "ngẫu",
    "nghĩa",
    "nhỏ",
    "nhất",
    "nhiên",
    "nội",
    "nút",
    "phải",
    "pháp",
    "phần",
    "qua",
    "ra",
    "sách",
    "số",
    "thành",
    "thấy",
    "theo",
    "tiên",
    "trong",
    "trí",
    "trình",
    "trị",
    "tìm",
    "từng",
    "tử",
    "vào",
    "về",
    "vị",
    "đầu",
    "được",
    "đổi",
}


def _joined_ocr_token(title: str) -> str | None:
    for raw_token in _LETTER_TOKEN_RE.findall(unicodedata.normalize("NFKC", title)):
        has_internal_case_transition = any(
            left.islower() and right.isupper()
            for left, right in zip(raw_token, raw_token[1:])
        )
        if len(raw_token) < 4 or has_internal_case_transition or raw_token.isupper():
            continue
        token = raw_token.casefold()
        if not any(ord(character) > 127 for character in token):
            continue
        for left in _OCR_LEFT_WORDS:
            if not token.startswith(left) or len(token) <= len(left) + 1:
                continue
            remainder = token[len(left) :]
            if any(remainder.startswith(right) for right in _OCR_RIGHT_WORDS):
                return raw_token
    return None


def _visible_prose(lesson: Lesson, root: str) -> Iterator[tuple[str, str]]:
    yield f"{root}.learning_objective", lesson.learning_objective
    yield f"{root}.hook", lesson.hook
    yield f"{root}.intuition", lesson.intuition
    yield f"{root}.tiny_example.input", lesson.tiny_example.input
    for index, step in enumerate(lesson.tiny_example.steps):
        yield f"{root}.tiny_example.steps[{index}]", step
    yield f"{root}.tiny_example.output", lesson.tiny_example.output
    for index, step in enumerate(lesson.process_steps):
        yield f"{root}.process_steps[{index}]", step
    for formula_index, formula in enumerate(lesson.formula_blocks):
        yield f"{root}.formula_blocks[{formula_index}].plain_text", formula.plain_text
        for symbol_index, symbol in enumerate(formula.symbols):
            yield (
                f"{root}.formula_blocks[{formula_index}].symbols[{symbol_index}].meaning",
                symbol.meaning,
            )
    yield f"{root}.common_mistake", lesson.common_mistake
    for index, takeaway in enumerate(lesson.takeaways):
        yield f"{root}.takeaways[{index}]", takeaway
    yield f"{root}.recall_prompt", lesson.recall_prompt


def learner_visible_lesson_word_breakdown(lesson: Lesson) -> dict[str, int]:
    """Count learner-visible lesson words by editable field group."""

    def count(value: str) -> int:
        return len(_VISIBLE_WORD_RE.findall(value))

    return {
        "title": count(lesson.title),
        "objective": count(lesson.learning_objective),
        "hook": count(lesson.hook),
        "intuition": count(lesson.intuition),
        "tiny_example": sum(count(value) for value in (
            lesson.tiny_example.input,
            *lesson.tiny_example.steps,
            lesson.tiny_example.output,
        )),
        "process_steps": sum(count(value) for value in lesson.process_steps),
        "formula_blocks": sum(
            count(value)
            for formula in lesson.formula_blocks
            for value in (
                formula.plain_text,
                *(symbol.meaning for symbol in formula.symbols),
            )
        ),
        "common_mistake": count(lesson.common_mistake),
        "takeaways": sum(count(value) for value in lesson.takeaways),
        "recall_prompt": count(lesson.recall_prompt),
    }


def _append(
    issues: list[EditorialIssue],
    code: str,
    path: str,
    message: str,
) -> None:
    issues.append(EditorialIssue(code=code, path=path, message=message))


def validate_lesson_editorial(
    lesson: Lesson,
    *,
    path: str = "lesson",
    pedagogical: bool = False,
    learner_polish: bool = False,
) -> list[EditorialIssue]:
    """Return deterministic editorial quality issues for one lesson.

    The default mode remains conservative so existing course-v1 packs stay
    compatible. ``pedagogical=True`` additionally enforces the course-v2 shape:
    enough connected explanation and worked practice to be a real micro-lesson,
    without allowing an overlong lock-screen text dump. Neither mode attempts to
    judge factual correctness or replace source-grounded AI review.
    """

    issues: list[EditorialIssue] = []
    title_path = f"{path}.title"
    title = unicodedata.normalize("NFKC", lesson.title).strip()

    if _TITLE_PAGE_LABEL_RE.search(title):
        _append(
            issues,
            "LESSON_TITLE_PAGE_LABEL",
            title_path,
            "Lesson title exposes an imported page-range label ('· trang').",
        )
    if _INTERNAL_VISUAL_SUPPLEMENT_RE.search(title):
        _append(
            issues,
            "LESSON_TITLE_INTERNAL_SUPPLEMENT",
            title_path,
            "Lesson title exposes the internal visual-supplement label.",
        )
    if _VISUAL_TRANSCRIPTION_LABEL_RE.search(title):
        _append(
            issues,
            "LESSON_TITLE_VISUAL_TRANSCRIPTION",
            title_path,
            "Lesson title exposes an internal visual transcription label.",
        )

    angle_match = _ANGLE_WRAPPED_TITLE_RE.match(title)
    angle_label = angle_match.group("label").strip() if angle_match is not None else ""
    angle_heading = _normalized_heading(angle_label)
    angle_is_placeholder = bool(
        angle_match is not None
        and (
            angle_heading in _ANGLE_PLACEHOLDER_HEADINGS
            # A whole title wrapped around a prose phrase is an import marker.
            # Single technical parameters such as <T> and <K,V> remain valid.
            or (bool(re.search(r"\s", angle_label)) and not re.fullmatch(
                r"[A-Z][A-Za-z0-9_]*(?:\s*,\s*[A-Z][A-Za-z0-9_]*)+",
                angle_label,
            ))
        )
    )
    if angle_is_placeholder:
        _append(
            issues,
            "LESSON_TITLE_ANGLE_PLACEHOLDER",
            title_path,
            "Lesson title is an angle-bracket import placeholder.",
        )

    semantic_title = _TITLE_PAGE_SUFFIX_RE.sub("", title).strip()
    if _normalized_heading(semantic_title) in _GENERIC_HEADINGS:
        _append(
            issues,
            "LESSON_TITLE_GENERIC",
            title_path,
            "Lesson title is a generic slide/course heading rather than a teachable concept.",
        )

    if joined_token := _joined_ocr_token(semantic_title):
        _append(
            issues,
            "LESSON_TITLE_OCR_JOINED_TOKEN",
            title_path,
            f"Lesson title contains a likely OCR-joined Vietnamese token: {joined_token!r}.",
        )

    if _GENERIC_OBJECTIVE_RE.search(lesson.learning_objective):
        _append(
            issues,
            "LESSON_OBJECTIVE_SOURCE_TEMPLATE",
            f"{path}.learning_objective",
            "Learning objective repeats the import template instead of stating a learner outcome.",
        )

    if pedagogical:
        objective_word_count = len(_VISIBLE_WORD_RE.findall(lesson.learning_objective))
        if objective_word_count < _COURSE_V2_MIN_OBJECTIVE_WORDS:
            _append(
                issues,
                "LESSON_OBJECTIVE_TOO_THIN",
                f"{path}.learning_objective",
                (
                    f"Course-v2 objective has {objective_word_count} words; "
                    f"at least {_COURSE_V2_MIN_OBJECTIVE_WORDS} are required for a specific learner outcome."
                ),
            )
        if not has_observable_learning_objective(lesson.learning_objective):
            _append(
                issues,
                "LESSON_OBJECTIVE_NOT_OBSERVABLE",
                f"{path}.learning_objective",
                "Course-v2 objective must name an observable learner action.",
            )

        title_word_count = len(_VISIBLE_WORD_RE.findall(title))
        if not 2 <= title_word_count <= 14:
            _append(
                issues,
                "LESSON_TITLE_LENGTH_INVALID",
                title_path,
                (
                    f"Course-v2 title has {title_word_count} words; "
                    "use 2-14 words naming one teachable concept."
                ),
            )

        field_minimums = (
            ("hook", lesson.hook, _COURSE_V2_MIN_HOOK_WORDS),
            ("intuition", lesson.intuition, _COURSE_V2_MIN_INTUITION_WORDS),
            ("common_mistake", lesson.common_mistake, _COURSE_V2_MIN_COMMON_MISTAKE_WORDS),
            ("recall_prompt", lesson.recall_prompt, _COURSE_V2_MIN_RECALL_PROMPT_WORDS),
        )
        for field_name, value, minimum in field_minimums:
            word_count = len(_VISIBLE_WORD_RE.findall(value))
            if word_count < minimum:
                _append(
                    issues,
                    f"LESSON_{field_name.upper()}_TOO_THIN",
                    f"{path}.{field_name}",
                    (
                        f"Course-v2 {field_name} has {word_count} words; "
                        f"at least {minimum} are required."
                    ),
                )

        if len(lesson.tiny_example.steps) < 2:
            _append(
                issues,
                "LESSON_EXAMPLE_STEPS_TOO_FEW",
                f"{path}.tiny_example.steps",
                "Course-v2 worked example must contain at least two ordered reasoning steps.",
            )
        example_values = [
            lesson.tiny_example.input,
            *lesson.tiny_example.steps,
            lesson.tiny_example.output,
        ]
        example_word_count = sum(
            len(_VISIBLE_WORD_RE.findall(value))
            for value in example_values
        )
        if example_word_count < _COURSE_V2_MIN_EXAMPLE_WORDS:
            _append(
                issues,
                "LESSON_EXAMPLE_TOO_THIN",
                f"{path}.tiny_example",
                (
                    f"Course-v2 worked example has {example_word_count} words; "
                    f"at least {_COURSE_V2_MIN_EXAMPLE_WORDS} are required to show input, reasoning and result."
                ),
            )

        if len(lesson.process_steps) < 2:
            _append(
                issues,
                "LESSON_PROCESS_STEPS_TOO_FEW",
                f"{path}.process_steps",
                "Course-v2 lesson must contain at least two ordered teaching steps.",
            )

        for field_name, values in (
            ("process_steps", lesson.process_steps),
            ("takeaways", lesson.takeaways),
        ):
            seen: dict[str, int] = {}
            for index, value in enumerate(values):
                normalized = _normalized_heading(value)
                if normalized and normalized in seen:
                    _append(
                        issues,
                        f"LESSON_{field_name.upper()}_DUPLICATE",
                        f"{path}.{field_name}[{index}]",
                        (
                            f"Course-v2 {field_name}[{index}] duplicates "
                            f"{field_name}[{seen[normalized]}] instead of advancing the lesson."
                        ),
                    )
                else:
                    seen[normalized] = index

    if len(lesson.process_steps) > 8:
        _append(
            issues,
            "LESSON_PROCESS_STEPS_EXCESSIVE",
            f"{path}.process_steps",
            f"Lesson has {len(lesson.process_steps)} process steps; the deterministic limit is 8.",
        )

    visible_values = [title, *(value for _, value in _visible_prose(lesson, path))]
    visible_word_count = sum(learner_visible_lesson_word_breakdown(lesson).values())
    maximum_visible_words = (
        _COURSE_V2_MAX_VISIBLE_LESSON_WORDS
        if pedagogical
        else _MAX_VISIBLE_LESSON_WORDS
    )
    if visible_word_count > maximum_visible_words:
        _append(
            issues,
            "LESSON_TEXT_EXCESSIVE",
            path,
            (
                f"Lesson has {visible_word_count} learner-visible words; "
                f"the deterministic limit is {maximum_visible_words}."
            ),
        )
    if pedagogical and visible_word_count < _COURSE_V2_MIN_VISIBLE_LESSON_WORDS:
        _append(
            issues,
            "LESSON_TEXT_TOO_THIN",
            path,
            (
                f"Course-v2 lesson has {visible_word_count} learner-visible words; "
                f"at least {_COURSE_V2_MIN_VISIBLE_LESSON_WORDS} are required for a coherent micro-lesson."
            ),
        )

    if learner_polish:
        if _GENERIC_ROLE_OBJECTIVE_RE.search(lesson.learning_objective):
            _append(
                issues,
                "LESSON_OBJECTIVE_GENERIC_ROLE_TEMPLATE",
                f"{path}.learning_objective",
                (
                    "Learner objective uses a generic 'vai trò ... trong nội "
                    "dung đang học' or 'giải thích khái niệm ...' template "
                    "instead of a concrete outcome."
                ),
            )
        for prose_path, value in ((title_path, title), *_visible_prose(lesson, path)):
            artifact = learner_visible_import_artifact(value)
            if artifact is not None:
                _append(
                    issues,
                    "LESSON_TEXT_IMPORT_ARTIFACT",
                    prose_path,
                    f"Course-v2 learner text contains {artifact}.",
                )
            raw_filler = learner_visible_raw_filler(value)
            if raw_filler is not None:
                _append(
                    issues,
                    "LESSON_TEXT_RAW_FILLER_ENGLISH",
                    prose_path,
                    f"Course-v2 learner text contains {raw_filler}.",
                )
        if mixes_shell_platforms(visible_values):
            _append(
                issues,
                "LESSON_SHELL_PLATFORM_MIXED",
                path,
                "Course-v2 lesson mixes POSIX and Windows shell syntax in one procedure.",
            )

    for prose_path, value in _visible_prose(lesson, path):
        if _SOURCE_PAGE_NARRATION_RE.search(value):
            _append(
                issues,
                "LESSON_PROSE_PAGE_NARRATION",
                prose_path,
                "Learner-facing prose narrates a source page/slide number.",
            )
        if _LECTURE_NARRATION_RE.search(value):
            _append(
                issues,
                "LESSON_PROSE_LECTURE_NARRATION",
                prose_path,
                "Learner-facing prose narrates what the source lecture says instead of teaching directly.",
            )
        if pedagogical and _COURSE_V2_SOURCE_NARRATION_RE.search(value):
            _append(
                issues,
                "LESSON_PROSE_SOURCE_NARRATION",
                prose_path,
                "Course-v2 prose describes the source document or slide sequence instead of teaching directly.",
            )

    return issues


def issue_codes(issues: Iterable[EditorialIssue]) -> set[str]:
    """Return issue codes for concise callers and tests."""

    return {issue.code for issue in issues}
