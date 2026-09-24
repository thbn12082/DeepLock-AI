from __future__ import annotations

import pytest

from deeplock_content.chunking import build_corpus
from deeplock_content.curriculum import (
    build_deterministic_curriculum_outline,
    curriculum_part_ranges,
    semantic_content_identity_namespace,
    semantic_curriculum_parts,
)
from deeplock_content.models import Document
from deeplock_content.util import stable_id


def _corpus(page_count: int):
    document = Document(
        document_id="doc_auto",
        filename="lecture.pdf",
        mime_type="application/pdf",
        sha256="sha256:" + "1" * 64,
        page_count=page_count,
        created_at="2026-01-01T00:00:00Z",
        source_path=None,
    )
    return build_corpus([(
        document,
        [
            (page, f"Trang {page}\nNội dung kỹ thuật đầy đủ của trang {page}. " * (page + 1))
            for page in range(1, page_count + 1)
        ],
    )])


def test_part_ranges_cover_document_exactly():
    assert curriculum_part_ranges(page_count=10, pages_per_part=4) == [
        (1, 4),
        (5, 8),
        (9, 10),
    ]
    assert curriculum_part_ranges(
        page_count=10,
        pages_per_part=4,
        explicit_parts=[
            {"page_start": 1, "page_end": 3},
            {"page_start": 4, "page_end": 10},
        ],
    ) == [(1, 3), (4, 10)]
    with pytest.raises(ValueError, match="continue at page 4"):
        curriculum_part_ranges(
            page_count=10,
            pages_per_part=4,
            explicit_parts=[
                {"page_start": 1, "page_end": 3},
                {"page_start": 5, "page_end": 10},
            ],
        )


def test_deterministic_outline_adapts_short_document_to_one_atom():
    corpus = _corpus(2)
    outline = build_deterministic_curriculum_outline(
        corpus=corpus,
        curriculum={
            "course_id": "course_auto",
            "title": "MLOps",
            "description": "Kho MLOps đầy đủ",
            "pages_per_part": 24,
            "target_pages_per_atom": 6,
            "modules": [{
                "module_id": "lecture_mlflow",
                "title": "MLflow",
                "output": "lecture.pdf",
            }],
        },
    )
    assert len(outline.modules) == 1
    assert len(outline.modules[0].atom_plans) == 1
    assert outline.modules[0].atom_plans[0].source_ref_ids == [
        chunk.source_ref.source_ref_id for chunk in corpus.chunks
    ]


def test_deterministic_outline_assigns_every_source_once_in_page_order():
    corpus = _corpus(25)
    outline = build_deterministic_curriculum_outline(
        corpus=corpus,
        curriculum={
            "course_id": "course_auto",
            "title": "MLOps",
            "description": "Kho MLOps đầy đủ",
            "pages_per_part": 12,
            "target_pages_per_atom": 4,
            "modules": [{
                "module_id": "lecture_mlflow",
                "title": "MLflow",
                "output": "lecture.pdf",
            }],
        },
    )
    assigned = [
        source_ref_id
        for module in outline.modules
        for plan in module.atom_plans
        for source_ref_id in plan.source_ref_ids
    ]
    expected = [chunk.source_ref.source_ref_id for chunk in corpus.chunks]
    assert assigned == expected
    assert len(assigned) == len(set(assigned))
    assert [module.order_index for module in outline.modules] == [0, 1, 2]


def test_deterministic_outline_skips_a_nonlearning_empty_final_part():
    corpus = _corpus(25)
    # Model the post-vision state of a 25-page deck whose last page is only a
    # thank-you slide: the document still has 25 physical pages, while page 25
    # deliberately owns no learning source chunk.
    corpus.chunks = [chunk for chunk in corpus.chunks if chunk.page_start != 25]
    outline = build_deterministic_curriculum_outline(
        corpus=corpus,
        curriculum={
            "course_id": "course_auto",
            "title": "YOLO",
            "description": "Bài giảng đã audit hình ảnh",
            "pages_per_part": 24,
            "target_pages_per_atom": 6,
            "modules": [{
                "module_id": "lecture_yolo",
                "title": "YOLOv1",
                "output": "lecture.pdf",
            }],
        },
    )
    assert len(outline.modules) == 1
    assert outline.modules[0].module_id == "lecture_yolo_part_001"
    assigned = [
        source_ref_id
        for plan in outline.modules[0].atom_plans
        for source_ref_id in plan.source_ref_ids
    ]
    assert assigned == [chunk.source_ref.source_ref_id for chunk in corpus.chunks]


def test_deterministic_outline_rejects_a_document_with_no_learning_sources():
    corpus = _corpus(1)
    corpus.chunks = []
    with pytest.raises(ValueError, match="contains no learning source chunks"):
        build_deterministic_curriculum_outline(
            corpus=corpus,
            curriculum={
                "pages_per_part": 24,
                "target_pages_per_atom": 6,
                "modules": [{
                    "module_id": "lecture_empty",
                    "title": "Only a cover",
                    "output": "lecture.pdf",
                }],
            },
        )


def test_semantic_parts_adapt_a_short_lecture_to_one_atom():
    corpus = _corpus(2)
    parts = semantic_curriculum_parts(
        corpus=corpus,
        curriculum={
            "pages_per_part": 20,
            "target_pages_per_atom": 4,
            "modules": [{
                "module_id": "lecture_mlflow",
                "title": "Theo dõi thí nghiệm với MLflow",
                "output": "lecture.pdf",
            }],
        },
    )

    assert len(parts) == 1
    assert parts[0].atom_count == 1
    assert parts[0].module_id == "lecture_mlflow_v2_part_001"
    assert parts[0].required_atom_ids == ["lecture_mlflow_v2_part_001_atom_1"]


def test_semantic_parts_use_physical_page_groups_and_preserve_exact_order():
    corpus = _corpus(11)
    # Add a separately audited visual supplement to page 3. It is a second
    # source block, but must not inflate the physical-page-based atom count.
    supplement = corpus.chunks[2].model_copy(deep=True)
    supplement.chunk_id = "chunk_page_3_visual"
    supplement.source_ref.chunk_id = supplement.chunk_id
    supplement.source_ref.source_ref_id = "src_page_3_visual"
    supplement.normalized_text = "Sơ đồ trực quan giải thích luồng theo dõi thí nghiệm."
    supplement.source_ref.anchor_end = len(supplement.normalized_text)
    corpus.chunks.insert(3, supplement)
    parts = semantic_curriculum_parts(
        corpus=corpus,
        curriculum={
            "pages_per_part": 20,
            "target_pages_per_atom": 4,
            "modules": [{
                "module_id": "lecture_mlflow",
                "title": "Theo dõi thí nghiệm với MLflow",
                "output": "lecture.pdf",
            }],
        },
    )

    assert [part.atom_count for part in parts] == [3]
    assert parts[0].required_atom_ids == [
        f"lecture_mlflow_v2_part_001_atom_{index}"
        for index in range(1, 4)
    ]
    assert [
        chunk.source_ref.source_ref_id
        for part in parts
        for chunk in part.chunks
    ] == [chunk.source_ref.source_ref_id for chunk in corpus.chunks]


def test_semantic_parts_default_to_five_atoms_per_part_for_existing_curricula():
    parts = semantic_curriculum_parts(
        corpus=_corpus(25),
        curriculum={
            "pages_per_part": 25,
            "target_pages_per_atom": 5,
            "modules": [{
                "module_id": "lecture_existing",
                "title": "Existing curriculum",
                "output": "lecture.pdf",
            }],
        },
    )

    assert [part.atom_count for part in parts] == [5]


def test_semantic_parts_allow_ten_atoms_per_part_when_explicitly_configured():
    parts = semantic_curriculum_parts(
        corpus=_corpus(48),
        curriculum={
            "pages_per_part": 48,
            "target_pages_per_atom": 4,
            "max_atoms_per_part": 10,
            "modules": [{
                "module_id": "lecture_dense",
                "title": "Dense curriculum",
                "output": "lecture.pdf",
            }],
        },
    )

    assert [part.atom_count for part in parts] == [10]


@pytest.mark.parametrize("max_atoms_per_part", [0, 11])
def test_semantic_parts_reject_invalid_max_atoms_per_part(max_atoms_per_part):
    with pytest.raises(ValueError, match="max_atoms_per_part must be between 1 and 10"):
        semantic_curriculum_parts(
            corpus=_corpus(1),
            curriculum={
                "max_atoms_per_part": max_atoms_per_part,
                "modules": [{
                    "module_id": "lecture_invalid",
                    "title": "Invalid granularity",
                    "output": "lecture.pdf",
                }],
            },
        )


def test_semantic_parts_fail_preflight_when_atom_capacity_cannot_fit_page_groups():
    with pytest.raises(ValueError, match="granularity capacity is insufficient"):
        semantic_curriculum_parts(
            corpus=_corpus(16),
            curriculum={
                "pages_per_part": 16,
                "target_pages_per_atom": 2,
                "max_atoms_per_part": 5,
                "modules": [{
                    "module_id": "lecture_over_capacity",
                    "title": "Over capacity",
                    "output": "lecture.pdf",
                }],
            },
        )


def test_semantic_v3_identity_graph_is_disjoint_from_v2():
    corpus = _corpus(2)
    base = {
        "pages_per_part": 20,
        "target_pages_per_atom": 2,
        "max_atoms_per_part": 10,
        "modules": [{
            "module_id": "lecture_rewrite",
            "title": "Rewritten lecture",
            "output": "lecture.pdf",
        }],
    }

    v2 = semantic_curriculum_parts(corpus=corpus, curriculum={**base, "content_identity_namespace": "v2"})
    v3 = semantic_curriculum_parts(corpus=corpus, curriculum={**base, "content_identity_namespace": "v3"})
    v2_atom_id = v2[0].required_atom_ids[0]
    v3_atom_id = v3[0].required_atom_ids[0]
    identity_seed = "sha256:" + "8" * 64
    v2_pair_id = stable_id("pair", identity_seed, v2_atom_id)
    v3_pair_id = stable_id("pair", identity_seed, v3_atom_id)

    assert v2[0].module_id != v3[0].module_id
    assert v2_atom_id != v3_atom_id
    assert v2_pair_id != v3_pair_id
    assert {
        stable_id("q", v2_pair_id, str(index)) for index in range(1, 6)
    }.isdisjoint({
        stable_id("q", v3_pair_id, str(index)) for index in range(1, 6)
    })


def test_semantic_v2_identity_graph_is_disjoint_from_legacy_without_resetting_data():
    corpus = _corpus(2)
    curriculum = {
        "course_id": "course_identity",
        "pages_per_part": 20,
        "target_pages_per_atom": 4,
        # This retired flag is present in already-planned pilot workspaces;
        # missing namespace must still resolve to the safe v2 identity graph.
        "deterministic_outline": True,
        "modules": [{
            "module_id": "lecture_mlflow",
            "title": "MLflow",
            "output": "lecture.pdf",
        }],
    }

    legacy = build_deterministic_curriculum_outline(corpus=corpus, curriculum=curriculum)
    semantic = semantic_curriculum_parts(corpus=corpus, curriculum=curriculum)
    legacy_atom_id = legacy.modules[0].atom_plans[0].atom_id
    semantic_atom_id = semantic[0].required_atom_ids[0]
    identity_seed = "sha256:" + "7" * 64
    legacy_pair_id = stable_id("pair", identity_seed, legacy_atom_id)
    semantic_pair_id = stable_id("pair", identity_seed, semantic_atom_id)

    assert semantic_content_identity_namespace(curriculum) == "v2"
    assert legacy.modules[0].module_id == "lecture_mlflow_part_001"
    assert semantic[0].module_id == "lecture_mlflow_v2_part_001"
    assert legacy_atom_id != semantic_atom_id
    assert legacy_pair_id != semantic_pair_id
    assert {
        stable_id("q", legacy_pair_id, str(index)) for index in range(1, 6)
    }.isdisjoint({
        stable_id("q", semantic_pair_id, str(index)) for index in range(1, 6)
    })


@pytest.mark.parametrize("namespace", ["", "v2/rewrite", " v2 ", "x" * 33, True])
def test_semantic_identity_namespace_rejects_ambiguous_or_unsafe_values(namespace):
    with pytest.raises(ValueError, match="content_identity_namespace"):
        semantic_content_identity_namespace({"content_identity_namespace": namespace})
