from __future__ import annotations

import re

SUPPORTED_LOGICAL_MODELS = ("gpt-5.5",)

# Codex renames its models under the same account: gpt-5.5 stopped existing
# upstream ("The model `gpt-5.5` does not exist or you do not have access to
# it") while gpt-5.6-luna and friends appeared in its place. Pinning one exact
# version turned that rename into a hard outage, so match the family instead.
_GPT_FAMILY = re.compile(r"^gpt-[0-9][0-9A-Za-z._-]*$")


def _require_supported_model(model: str, *, stage: str) -> None:
    """Allow any gpt-* route the router serves, or an explicit claude-* model.

    The build cache is keyed by logical model, so two models never mix inside
    one pack; this only decides which transports the pipeline will speak to.
    """

    if model in SUPPORTED_LOGICAL_MODELS or _GPT_FAMILY.match(model) or model.startswith("claude-"):
        return
    raise ValueError(
        f"DeepLock {stage} supports a gpt-* or claude-* model; got {model!r}"
    )


from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(StrictModel):
    document_id: str
    filename: str
    mime_type: str
    sha256: str
    page_count: int = Field(ge=1)
    created_at: str
    source_path: str | None = None
    display_name: str | None = None
    logical_path: str | None = None
    source_aliases: list[str] = Field(default_factory=list)
    source_format: Literal["PDF", "PPTX", "DOCX", "MARKDOWN", "TEXT"] | None = None


class SourceRef(StrictModel):
    source_ref_id: str
    document_id: str
    chunk_id: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    anchor_start: int = Field(ge=0)
    anchor_end: int = Field(gt=0)
    claim_type: Literal["SOURCE_GROUNDED", "ENRICHMENT"] = "SOURCE_GROUNDED"


class DocumentChunk(StrictModel):
    chunk_id: str
    document_id: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    heading_path: list[str]
    normalized_text: str
    text_hash: str
    source_ref: SourceRef


class ExtractedCorpus(StrictModel):
    documents: list[Document]
    chunks: list[DocumentChunk]
    source_hash: str


class TinyExample(StrictModel):
    input: str
    steps: list[str]
    output: str


class FormulaSymbol(StrictModel):
    symbol: str
    meaning: str


class FormulaBlock(StrictModel):
    latex: str
    plain_text: str
    symbols: list[FormulaSymbol]


class Lesson(StrictModel):
    lesson_id: str
    pair_id: str
    atom_id: str
    title: str
    learning_objective: str
    grounding_type: Literal["SOURCE_GROUNDED", "ENRICHMENT"]
    prerequisite_ids: list[str]
    hook: str
    intuition: str
    tiny_example: TinyExample
    process_steps: list[str]
    formula_blocks: list[FormulaBlock]
    common_mistake: str
    takeaways: list[str] = Field(min_length=1, max_length=3)
    recall_prompt: str
    estimated_seconds: int = Field(ge=30, le=90)
    source_ref_ids: list[str] = Field(min_length=1)
    illustration_source_ref_ids: list[str] = Field(default_factory=list)


class QuizOption(StrictModel):
    id: Literal["A", "B", "C", "D"]
    text: str
    rationale: str
    misconception_tag: str | None = None


class Question(StrictModel):
    question_id: str
    pair_id: str
    atom_id: str
    stem: str
    options: list[QuizOption] = Field(min_length=4, max_length=4)
    correct_option_id: Literal["A", "B", "C", "D"]
    explanation: str
    difficulty: int = Field(ge=1, le=5)
    bloom_level: Literal["RECALL", "UNDERSTAND", "APPLY", "ANALYZE"]
    grounding_type: Literal["SOURCE_GROUNDED", "ENRICHMENT"]
    source_ref_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def exactly_abcd(self) -> "Question":
        if [item.id for item in self.options] != ["A", "B", "C", "D"]:
            raise ValueError("options must be ordered A-D exactly once")
        return self


class QuizBundle(StrictModel):
    questions: list[Question] = Field(min_length=3, max_length=5)
    insufficient_evidence_items: list[str]


class MiniLabFallback(StrictModel):
    columns: list[str]
    rows: list[list[str]]
    takeaways: list[str] = Field(min_length=1, max_length=3)


class MiniLab(StrictModel):
    mini_lab_id: str
    atom_id: str
    lab_type: Literal["LR_STEP_1D", "ACTIVATION_CURVE", "CONVOLUTION_2D", "OVERFITTING_CURVE", "CONFUSION_MATRIX"]
    spec: dict[str, Any]
    fallback: MiniLabFallback
    source_ref_ids: list[str] = Field(min_length=1)
    ai_review_status: Literal["PENDING", "AI_APPROVED", "QUARANTINED"] = "PENDING"


class IllustrationAsset(StrictModel):
    illustration_id: str = Field(min_length=1)
    source_ref_id: str = Field(min_length=1)
    asset_member: str = Field(
        min_length=1,
        max_length=180,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:png|webp)$",
    )
    mime_type: Literal["image/png", "image/webp"]
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    byte_size: int = Field(ge=1, le=16 * 1024 * 1024)
    width: int = Field(ge=1, le=8192)
    height: int = Field(ge=1, le=8192)
    page_number: int = Field(ge=1)
    caption: str = Field(min_length=1, max_length=500)
    alt_text: str = Field(min_length=1, max_length=500)


class AtomContent(StrictModel):
    lesson: Lesson
    quiz_bundle: QuizBundle
    mini_lab: MiniLab | None


class AtomPlan(StrictModel):
    atom_id: str
    order_index: int = Field(ge=0)
    title: str
    objective: str
    prerequisite_ids: list[str]
    source_ref_ids: list[str] = Field(min_length=1)


class ModuleOutline(StrictModel):
    module_id: str
    order_index: int = Field(ge=0)
    title: str
    objective: str
    atom_plans: list[AtomPlan] = Field(min_length=1, max_length=10)


class Course(StrictModel):
    course_id: str
    title: str
    description: str
    language: Literal["vi"] = "vi"


class CourseOutline(StrictModel):
    course: Course
    modules: list[ModuleOutline] = Field(min_length=1)


class FullLesson(StrictModel):
    full_lesson_id: str
    module_id: str
    order_index: int
    title: str
    objective: str
    atom_ids: list[str]
    estimated_minutes: int


class KnowledgeAtom(StrictModel):
    atom_id: str
    module_id: str
    order_index: int
    title: str
    grounding_type: Literal["SOURCE_GROUNDED", "ENRICHMENT"]
    prerequisite_ids: list[str]
    estimated_seconds: int


class LearningPair(StrictModel):
    pair_id: str
    atom_id: str
    micro_lesson_id: str
    question_ids: list[str] = Field(min_length=3)
    revision: int = Field(ge=1)
    content_hash: str
    ai_review_status: Literal["PENDING", "AI_APPROVED", "QUARANTINED"] = "PENDING"


class MindMapNode(StrictModel):
    id: str
    label: str
    type: Literal["COURSE", "MODULE", "TOPIC", "ATOM"]
    atom_id: str | None


class MindMapEdge(StrictModel):
    from_: str = Field(alias="from")
    to: str
    type: Literal["CONTAINS", "PREREQUISITE", "RELATED"]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MindMap(StrictModel):
    mind_map_id: str
    root_node_id: str
    nodes: list[MindMapNode]
    edges: list[MindMapEdge]


class SourceExcerpt(StrictModel):
    source_excerpt_id: str
    source_ref_id: str
    document_id: str
    chunk_id: str
    document_label: str
    heading_path: list[str]
    page_start: int
    page_end: int
    context_text: str
    highlight_start: int
    highlight_end: int
    excerpt_hash: str


class AIReviewItem(StrictModel):
    item_type: Literal["LESSON", "QUESTION", "LEARNING_PAIR", "MINI_LAB", "MODULE"]
    item_id: str
    item_revision: int
    candidate_hash: str
    source_hash: str
    verdict: Literal["APPROVE", "REPAIR", "REJECT"]
    issue_codes: list[str]
    evidence_ref_ids: list[str]
    independent_correct_option_id: Literal["A", "B", "C", "D"] | None
    evidence_sufficient: bool
    short_rationale: str = Field(max_length=500)


class AIReviewBatch(StrictModel):
    reviews: list[AIReviewItem] = Field(min_length=1)


class PackData(StrictModel):
    course: Course
    modules: list[ModuleOutline]
    full_lessons: list[FullLesson]
    atoms: list[KnowledgeAtom]
    lessons: list[Lesson]
    learning_pairs: list[LearningPair]
    questions: list[Question]
    mindmaps: list[MindMap]
    mini_labs: list[MiniLab]
    illustrations: list[IllustrationAsset] = Field(default_factory=list)
    source_refs: list[SourceRef]
    source_excerpts: list[SourceExcerpt]
    source_hash: str
    generator_model_snapshot: str
    generator_prompt_version: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
