from __future__ import annotations

from types import SimpleNamespace

import pytest

from deeplock_content.cache import BuildCache
from deeplock_content.chunking import build_corpus
from deeplock_content.generation import ModelResponse
from deeplock_content.generation.builder import (
    _atom_task_prompt,
    _contiguous_module_atom_groups,
    _curriculum_outline,
    _curriculum_chunk_order_key,
    _mini_lab_matches_runtime,
    _normalize_takeaway_spacing,
    _deterministic_mindmap,
    _synchronize_outline_with_generated_lessons,
    _validate_takeaway_editorial,
    _validate_semantic_outline_title,
    _validate_page_atomic_assignments,
    _with_curriculum_page_overrides,
)
from deeplock_content.models import AtomPlan, Course, CourseOutline, Document, ModuleOutline
from deeplock_content.settings import Settings
from deeplock_content.util import resolve_content_id_seed


def _page_corpus(page_count: int):
    document = Document(
        document_id="doc_granularity",
        filename="granularity.pdf",
        mime_type="application/pdf",
        sha256="sha256:" + "9" * 64,
        page_count=page_count,
        created_at="2026-01-01T00:00:00Z",
        source_path=None,
    )
    return build_corpus([(
        document,
        [
            (page, f"Page {page} contains a distinct technical concept and worked detail.")
            for page in range(1, page_count + 1)
        ],
    )])


def test_visual_page_override_adds_missing_grounded_page(corpus):
    source = corpus.model_copy(deep=True)
    source.documents[0].page_count = 2
    filename = source.documents[0].filename
    curriculum = {
        "modules": [{
            "output": filename,
            "page_text_overrides": {
                "2": (
                    "Trang raster minh họa vòng lặp huấn luyện: zero_grad, forward, "
                    "tính loss, backward và optimizer.step; đồ thị loss giảm theo epoch."
                ),
            },
        }],
    }

    augmented = _with_curriculum_page_overrides(source, curriculum)

    added = [chunk for chunk in augmented.chunks if chunk.page_start == 2]
    assert len(added) == 1
    assert added[0].source_ref.page_start == 2
    assert "optimizer.step" in added[0].normalized_text
    assert augmented.source_hash != source.source_hash


def test_exact_duplicate_extractor_chunks_are_owned_once(corpus):
    source = corpus.model_copy(deep=True)
    source.chunks.append(source.chunks[0].model_copy(deep=True))

    augmented = _with_curriculum_page_overrides(
        source,
        {"modules": [{"output": source.documents[0].filename}]},
    )

    assert len(augmented.chunks) == len(corpus.chunks)
    assert len({chunk.source_ref.source_ref_id for chunk in augmented.chunks}) == len(augmented.chunks)


def test_conflicting_duplicate_source_identity_is_rejected(corpus):
    source = corpus.model_copy(deep=True)
    conflicting = source.chunks[0].model_copy(deep=True)
    conflicting.normalized_text += " conflicting payload"
    source.chunks.append(conflicting)

    with pytest.raises(ValueError, match="Conflicting source chunks share source_ref_id"):
        _with_curriculum_page_overrides(
            source,
            {"modules": [{"output": source.documents[0].filename}]},
        )


def test_visual_page_override_cannot_replace_extracted_text(corpus):
    filename = corpus.documents[0].filename
    curriculum = {
        "modules": [{
            "output": filename,
            "page_text_overrides": {"1": "x" * 100},
        }],
    }

    with pytest.raises(ValueError, match="would replace extracted source text"):
        _with_curriculum_page_overrides(corpus, curriculum)


def test_visual_page_supplement_extends_partially_extracted_page(corpus):
    filename = corpus.documents[0].filename
    curriculum = {
        "modules": [{
            "output": filename,
            "page_text_supplements": {
                "1": "Visual infographic lists Physics, Molecule, Image, Text, Social Network and Generation; "
                     "its graph tasks are Graph Classification, Node Classification, Link Prediction, "
                     "Community Detection, Graph Embedding and Graph Generation."
            },
        }],
    }

    augmented = _with_curriculum_page_overrides(corpus, curriculum)

    assert len(augmented.chunks) == len(corpus.chunks) + 1
    assert any("Graph Generation" in chunk.normalized_text for chunk in augmented.chunks)
    assert augmented.source_hash != corpus.source_hash


def test_visual_supplement_stays_after_page_text_and_cannot_split_across_atoms(corpus):
    curriculum = {
        "modules": [{
            "output": corpus.documents[0].filename,
            "page_text_supplements": {
                "1": "A visually audited equation and diagram explanation adds grounded technical meaning "
                     "that belongs to the same physical page as the extracted heading and prose."
            },
        }],
    }
    augmented = _with_curriculum_page_overrides(corpus, curriculum)
    page_chunks = sorted(
        [chunk for chunk in augmented.chunks if chunk.page_start == 1],
        key=_curriculum_chunk_order_key,
    )
    source_ids = [chunk.source_ref.source_ref_id for chunk in page_chunks]

    assert len(source_ids) >= 2
    assert all(
        "visual supplement (audited)" not in chunk.heading_path[0]
        for chunk in page_chunks[:-1]
    )
    assert page_chunks[-1].heading_path[0].endswith("visual supplement (audited)")
    _validate_page_atomic_assignments([source_ids], page_chunks, "module_fixture")
    with pytest.raises(ValueError, match="split across atoms"):
        _validate_page_atomic_assignments(
            [source_ids[:-1], [source_ids[-1]]],
            page_chunks,
            "module_fixture",
        )


def test_page_group_gate_counts_physical_pages_and_rejects_an_oversized_atom():
    corpus = _page_corpus(6)
    source_ids = [chunk.source_ref.source_ref_id for chunk in corpus.chunks]

    _validate_page_atomic_assignments(
        [source_ids[:3], source_ids[3:]],
        corpus.chunks,
        "module_balanced",
        max_page_groups_per_atom=3,
    )
    with pytest.raises(ValueError, match=r"spans 4 physical page-groups; maximum is 3"):
        _validate_page_atomic_assignments(
            [source_ids[:4], source_ids[4:]],
            corpus.chunks,
            "module_oversized",
            max_page_groups_per_atom=3,
        )


def test_semantic_outline_enforces_target_plus_one_page_group_cap(tmp_path):
    corpus = _page_corpus(6)
    source_ids = [chunk.source_ref.source_ref_id for chunk in corpus.chunks]
    curriculum = {
        "course_id": "course_granularity",
        "pages_per_part": 6,
        "target_pages_per_atom": 2,
        "max_atoms_per_part": 3,
        "modules": [{
            "module_id": "lecture_granularity",
            "title": "Granularity",
            "output": "granularity.pdf",
        }],
    }

    class OversizedOutlineAdapter:
        def __init__(self):
            self.inputs: list[str] = []

        def structured(self, **kwargs):
            self.inputs.append(str(kwargs["input_text"]))
            return ModelResponse(
                data={
                    "course": {
                        "course_id": "temporary",
                        "title": "Temporary",
                        "description": "Temporary wrapper",
                        "language": "vi",
                    },
                    "modules": [{
                        "module_id": "temporary_module",
                        "order_index": 0,
                        "title": "PhÃ¢n chia khá»‘i kiáº¿n thá»©c",
                        "objective": "Giáº£i thÃ­ch cÃ¡ch chia khá»‘i kiáº¿n thá»©c.",
                        "atom_plans": [
                            {
                                "atom_id": "temporary_atom_1",
                                "order_index": 0,
                                "title": "KhÃ¡i niá»‡m tá»« bá»‘n nguá»“n",
                                "objective": "PhÃ¢n tÃ­ch khá»‘i khÃ¡i niá»‡m Ä‘áº§u tiÃªn.",
                                "prerequisite_ids": [],
                                "source_ref_ids": source_ids[:4],
                            },
                            {
                                "atom_id": "temporary_atom_2",
                                "order_index": 1,
                                "title": "KhÃ¡i niá»‡m thá»© nÄƒm",
                                "objective": "MÃ´ táº£ khÃ¡i niá»‡m thá»© nÄƒm.",
                                "prerequisite_ids": [],
                                "source_ref_ids": source_ids[4:5],
                            },
                            {
                                "atom_id": "temporary_atom_3",
                                "order_index": 2,
                                "title": "KhÃ¡i niá»‡m thá»© sÃ¡u",
                                "objective": "MÃ´ táº£ khÃ¡i niá»‡m thá»© sÃ¡u.",
                                "prerequisite_ids": [],
                                "source_ref_ids": source_ids[5:],
                            },
                        ],
                    }],
                },
                response_id="response_oversized_outline",
                model_snapshot="cx/gpt-5.5",
            )

    settings = Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="low",
        review_reasoning_effort="high",
        generator_prompt_version="course-v2",
        review_prompt_version="reviewer-v2",
        max_repair_rounds=1,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )
    adapter = OversizedOutlineAdapter()
    cache = BuildCache(tmp_path / "granularity-outline-cache.sqlite3")
    try:
        with pytest.raises(ValueError, match=r"spans 4 physical page-groups; maximum is 3"):
            _curriculum_outline(
                corpus=corpus,
                curriculum=curriculum,
                cache=cache,
                adapter=adapter,
                settings=settings,
                model="gpt-5.5",
                prompt_version="course-v2",
                resume=False,
            )
    finally:
        cache.close()

    assert len(adapter.inputs) == 1
    assert "MAX_PHYSICAL_PAGE_GROUPS_PER_ATOM: 3" in adapter.inputs[0]


def test_semantic_outline_capacity_preflight_makes_no_model_call(tmp_path):
    corpus = _page_corpus(16)
    curriculum = {
        "pages_per_part": 16,
        "target_pages_per_atom": 2,
        "max_atoms_per_part": 5,
        "modules": [{
            "module_id": "lecture_over_capacity",
            "title": "Over capacity",
            "output": "granularity.pdf",
        }],
    }

    class NoCallAdapter:
        calls = 0

        def structured(self, **kwargs):
            self.calls += 1
            raise AssertionError("preflight must reject before a model call")

    settings = Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="low",
        review_reasoning_effort="high",
        generator_prompt_version="course-v2",
        review_prompt_version="reviewer-v2",
        max_repair_rounds=1,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )
    adapter = NoCallAdapter()
    cache = BuildCache(tmp_path / "capacity-preflight-cache.sqlite3")
    try:
        with pytest.raises(ValueError, match="granularity capacity is insufficient"):
            _curriculum_outline(
                corpus=corpus,
                curriculum=curriculum,
                cache=cache,
                adapter=adapter,
                settings=settings,
                model="gpt-5.5",
                prompt_version="course-v2",
                resume=False,
            )
    finally:
        cache.close()

    assert adapter.calls == 0


def test_stable_content_seed_preserves_unrelated_atom_identity_and_prompt(corpus):
    class Plan:
        atom_id = "atom_fixture"
        source_ref_ids = [corpus.chunks[0].source_ref.source_ref_id]
        prerequisite_ids = []

        @staticmethod
        def model_dump(*, mode):
            assert mode == "json"
            return {
                "atom_id": "atom_fixture",
                "order_index": 0,
                "title": "Fixture",
                "objective": "Understand the fixture.",
                "prerequisite_ids": [],
                "source_ref_ids": [corpus.chunks[0].source_ref.source_ref_id],
            }

    seed = corpus.source_hash
    before = _atom_task_prompt(Plan(), "module_fixture", corpus, seed)
    curriculum = {
        "stable_content_id_seed": seed,
        "modules": [{
            "output": corpus.documents[0].filename,
            "page_text_supplements": {
                "1": "An additional audited visual explanation supplies enough grounded technical detail "
                     "to create a distinct source reference without changing the logical lesson identity."
            },
        }],
    }
    augmented = _with_curriculum_page_overrides(corpus, curriculum)
    after = _atom_task_prompt(Plan(), "module_fixture", augmented, seed)

    assert augmented.source_hash != corpus.source_hash
    assert resolve_content_id_seed(augmented.source_hash, curriculum) == seed
    assert before == after


def test_stable_content_seed_rejects_malformed_digest(corpus):
    with pytest.raises(ValueError, match="stable_content_id_seed"):
        resolve_content_id_seed(corpus.source_hash, {"stable_content_id_seed": "not-a-digest"})


def test_visual_page_supplement_requires_existing_text(corpus):
    source = corpus.model_copy(deep=True)
    source.documents[0].page_count = 2
    curriculum = {
        "modules": [{
            "output": source.documents[0].filename,
            "page_text_supplements": {"2": "x" * 100},
        }],
    }

    with pytest.raises(ValueError, match="supplement requires existing extracted text"):
        _with_curriculum_page_overrides(source, curriculum)


def test_image_only_page_must_be_transcribed_or_explicitly_excluded(corpus):
    source = corpus.model_copy(deep=True)
    source.documents[0].page_count = 2
    curriculum = {
        "modules": [{"output": source.documents[0].filename}],
        "excluded_image_only_pages": [],
    }

    with pytest.raises(ValueError, match=r"undeclared=\[2\]"):
        _with_curriculum_page_overrides(source, curriculum)


def test_extracted_nontechnical_page_is_removed_with_explicit_reason(corpus):
    curriculum = {
        "modules": [{"output": corpus.documents[0].filename}],
        "excluded_nontechnical_pages": [{
            "file": corpus.documents[0].filename,
            "page": 1,
            "reason": "end divider; no technical content",
        }],
    }

    augmented = _with_curriculum_page_overrides(corpus, curriculum)

    assert not augmented.chunks
    assert augmented.source_hash != corpus.source_hash


def test_nontechnical_exclusion_requires_extracted_page(corpus):
    source = corpus.model_copy(deep=True)
    source.documents[0].page_count = 2
    curriculum = {
        "modules": [{"output": source.documents[0].filename}],
        "excluded_image_only_pages": [{
            "file": source.documents[0].filename,
            "page": 2,
            "reason": "image-only divider",
        }],
        "excluded_nontechnical_pages": [{
            "file": source.documents[0].filename,
            "page": 2,
            "reason": "incorrect exclusion type",
        }],
    }

    with pytest.raises(ValueError, match="has no extracted text"):
        _with_curriculum_page_overrides(source, curriculum)


@pytest.mark.parametrize(
    "text",
    [
        "A takeaway ending with stray comma,",
        "Missing sentence space.Next clause starts.",
        'An "unbalanced quote.',
        "x" * 501,
    ],
)
def test_takeaway_editorial_artifacts_fail_closed(text):
    with pytest.raises(ValueError):
        _validate_takeaway_editorial([text])


def test_takeaway_editorial_accepts_clean_text():
    _validate_takeaway_editorial([
        "Normalize pixels to [0, 1] before optimization.",
        "The relation 28x28 = 784 is stated clearly.",
    ])


def test_takeaway_sentence_spacing_is_normalized_before_editorial_gate():
    normalized = _normalize_takeaway_spacing([
        "Message passing gom lân cận.Sau đó nút được cập nhật.",
    ])

    assert normalized == ["Message passing gom lân cận. Sau đó nút được cập nhật."]
    _validate_takeaway_editorial(normalized)


def test_transposed_convolution_cannot_use_standard_convolution_lab():
    class Plan:
        atom_id = "atom_transposed"
        title = "Transposed Convolution"
        objective = "Compute the expanded 3x3 output."

    assert not _mini_lab_matches_runtime(Plan(), "CONVOLUTION_2D")
    assert _mini_lab_matches_runtime(Plan(), "ACTIVATION_CURVE")


def test_curriculum_mini_lab_allowlist_is_fail_closed():
    class Plan:
        atom_id = "atom_exact"
        title = "ReLU"
        objective = "Compute max(0, x)."

    assert _mini_lab_matches_runtime(Plan(), "ACTIVATION_CURVE", {"atom_exact"})
    assert not _mini_lab_matches_runtime(Plan(), "ACTIVATION_CURVE", set())


def test_module_atoms_are_split_into_deterministic_contiguous_groups(valid_pack):
    module = valid_pack.modules[0]

    groups = _contiguous_module_atom_groups(module, 2)

    assert [
        [plan.atom_id for plan in group]
        for group in groups
    ] == [
        ["atom_gradient", "atom_lr"],
        ["atom_batch"],
    ]
    assert [
        plan.atom_id
        for group in groups
        for plan in group
    ] == [plan.atom_id for plan in module.atom_plans]


def test_generated_lesson_wording_replaces_provisional_outline_before_mindmap():
    plan = AtomPlan(
        atom_id="atom_mlflow_tracking",
        order_index=0,
        title="Page 3 visual supplement (audited)",
        objective="Imported placeholder objective.",
        prerequisite_ids=[],
        source_ref_ids=["src_mlflow"],
    )
    outline = CourseOutline(
        course=Course(
            course_id="course_mlflow",
            title="MLflow",
            description="Theo dõi vòng đời mô hình.",
            language="vi",
        ),
        modules=[ModuleOutline(
            module_id="module_mlflow",
            order_index=0,
            title="Theo dõi vòng đời thí nghiệm",
            objective="Hiểu luồng theo dõi thí nghiệm.",
            atom_plans=[plan],
        )],
    )
    generated = {
        plan.atom_id: (
            SimpleNamespace(lesson=SimpleNamespace(
                title="Theo dõi tham số và metric",
                learning_objective="Phân biệt tham số đầu vào với metric đánh giá.",
            )),
            "cx/gpt-5.5",
        ),
    }

    _synchronize_outline_with_generated_lessons(outline, generated)
    mindmap = _deterministic_mindmap(outline, "sha256:" + "a" * 64)

    assert plan.title == "Theo dõi tham số và metric"
    assert plan.objective == "Phân biệt tham số đầu vào với metric đánh giá."
    atom_node = next(node for node in mindmap.nodes if node.atom_id == plan.atom_id)
    assert atom_node.label == plan.title
    with pytest.raises(ValueError, match="internal vision label"):
        _validate_semantic_outline_title(
            "MLflow · Phần 1/2 (trang 1–20)",
            "module.title",
        )


def test_semantic_outline_uses_model_module_wording_and_exact_dynamic_contract(
    corpus,
    tmp_path,
):
    source_ids = [chunk.source_ref.source_ref_id for chunk in corpus.chunks]
    curriculum = {
        "course_id": "course_semantic",
        "title": "Tối ưu mô hình",
        "description": "Học theo tiến trình khái niệm.",
        "pages_per_part": 20,
        "target_pages_per_atom": 4,
        # Existing catalog workspaces may still carry this retired boolean;
        # course-v2 must nevertheless take the semantic planner path.
        "deterministic_outline": True,
        "modules": [{
            "module_id": "lecture_optimization",
            "title": "Tên tệp bài giảng không dùng làm tên bài",
            "output": corpus.documents[0].filename,
        }],
    }

    class SemanticOutlineAdapter:
        def __init__(self):
            self.inputs: list[str] = []

        def structured(self, **kwargs):
            self.inputs.append(str(kwargs["input_text"]))
            return ModelResponse(
                data={
                    "course": {
                        "course_id": "temporary",
                        "title": "Temporary",
                        "description": "Temporary wrapper",
                        "language": "vi",
                    },
                    "modules": [{
                        # IDs/order are deliberately wrong: they are stable
                        # builder metadata and must be restored without another
                        # API request.
                        "module_id": "model_invented_module_id",
                        "order_index": 77,
                        "title": "Trực giác của gradient descent",
                        "objective": "Giải thích cách gradient dẫn hướng bước cập nhật.",
                        "atom_plans": [{
                            "atom_id": "model_invented_atom_id",
                            "order_index": 42,
                            "title": "Gradient dẫn hướng cập nhật",
                            "objective": "Mô tả vai trò của dấu gradient trong một bước tối ưu.",
                            "prerequisite_ids": ["model_invented_prerequisite"],
                            "source_ref_ids": source_ids,
                        }],
                    }],
                },
                response_id="response_semantic_outline",
                model_snapshot="cx/gpt-5.5",
            )

    settings = Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=1,
        generator_reasoning_effort="low",
        review_reasoning_effort="high",
        generator_prompt_version="course-v2",
        review_prompt_version="reviewer-v2",
        max_repair_rounds=1,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )
    adapter = SemanticOutlineAdapter()
    cache = BuildCache(tmp_path / "semantic-outline-cache.sqlite3")
    try:
        outline, snapshots = _curriculum_outline(
            corpus=corpus,
            curriculum=curriculum,
            cache=cache,
            adapter=adapter,
            settings=settings,
            model="gpt-5.5",
            prompt_version="course-v2",
            resume=False,
        )
    finally:
        cache.close()

    module = outline.modules[0]
    assert module.module_id == "lecture_optimization_v2_part_001"
    assert module.order_index == 0
    assert module.title == "Trực giác của gradient descent"
    assert module.objective == "Giải thích cách gradient dẫn hướng bước cập nhật."
    assert module.atom_plans[0].source_ref_ids == source_ids
    assert module.atom_plans[0].atom_id == "lecture_optimization_v2_part_001_atom_1"
    assert module.atom_plans[0].order_index == 0
    assert module.atom_plans[0].prerequisite_ids == []
    assert snapshots == ["cx/gpt-5.5"]
    assert "Each atom must teach one central concept" in adapter.inputs[0]
    assert "(trang" not in module.title.casefold()
