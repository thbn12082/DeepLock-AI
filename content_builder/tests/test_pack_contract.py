from __future__ import annotations

import base64
from types import SimpleNamespace

from deeplock_content.contracts import (
    build_pack_contract,
    check_pack_contract,
    freeze_outline_assignment,
    illustration_identities_sha256,
)
from deeplock_content.models import Document, DocumentChunk, ExtractedCorpus, IllustrationAsset, SourceRef
from deeplock_content.generation.builder import _attach_curriculum_illustrations
from deeplock_content.util import sha256_bytes, sha256_json, stable_id
from deeplock_content.validators import validate_pack


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def synthetic_curriculum_source() -> ExtractedCorpus:
    document = Document(
        document_id="doc_contract",
        filename="lecture.pdf",
        mime_type="application/pdf",
        sha256=sha256_bytes(b"fixture-pdf"),
        page_count=6,
        created_at="2026-08-24T00:00:00Z",
    )
    chunks = []
    for page_number in range(1, 7):
        text = f"Nội dung kỹ thuật có kiểm chứng ở trang {page_number}." * 4
        text_hash = sha256_bytes(text.encode("utf-8"))
        chunk_id = stable_id("chunk", document.document_id, str(page_number), text_hash)
        source_ref = SourceRef(
            source_ref_id=stable_id("src", chunk_id),
            document_id=document.document_id,
            chunk_id=chunk_id,
            page_start=page_number,
            page_end=page_number,
            anchor_start=0,
            anchor_end=len(text),
        )
        chunks.append(DocumentChunk(
            chunk_id=chunk_id,
            document_id=document.document_id,
            page_start=page_number,
            page_end=page_number,
            heading_path=[f"Trang {page_number}"],
            normalized_text=text,
            text_hash=text_hash,
            source_ref=source_ref,
        ))
    return ExtractedCorpus(
        documents=[document],
        chunks=chunks,
        source_hash=sha256_json([item.model_dump(mode="json") for item in chunks]),
    )


def test_contract_is_derived_without_candidate_or_generation_cache(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "illustration_module_fixture.png").write_bytes(PNG_1X1)
    curriculum = {
        "course_id": "course_contract",
        "pages_per_part": 6,
        "target_pages_per_atom": 2,
        "deterministic_outline": True,
        "modules": [{
            "module_id": "module_fixture",
            "title": "Fixture",
            "output": "lecture.pdf",
            "illustration_page": 2,
            "illustration_caption": "Sơ đồ fixture.",
        }],
    }
    contract = build_pack_contract(
        source=synthetic_curriculum_source(),
        curriculum=curriculum,
        media_root=media,
    )

    assert contract.expected_counts.model_dump() == {
        "modules": 1,
        "full_lessons": 1,
        "atoms": 3,
        "lessons": 3,
        "pairs": 3,
        "questions": 15,
        "source_refs": 6,
        "source_excerpts": 6,
        "illustrations": 1,
        "mindmaps": 1,
    }
    assert contract.ordered_module_ids == ["module_fixture_v2_part_001"]
    assert contract.ordered_atom_ids == [
        f"module_fixture_v2_part_001_atom_{index}" for index in range(1, 4)
    ]
    assert contract.outline_assignment_sha256 is None


def test_contract_and_generator_preserve_multiple_occurrences_on_same_page(tmp_path):
    source = synthetic_curriculum_source()
    media = tmp_path / "media"
    media.mkdir()
    (media / "occurrence_a.png").write_bytes(PNG_1X1)
    (media / "occurrence_b.png").write_bytes(PNG_1X1)
    curriculum = {
        "course_id": "course_occurrences",
        "pages_per_part": 6,
        "target_pages_per_atom": 2,
        "modules": [{
            "module_id": "module_occurrences",
            "title": "Multiple visuals",
            "output": "lecture.pdf",
            "illustrations": [
                {
                    "page": 2,
                    "caption": "Sơ đồ thứ nhất.",
                    "alt_text": "Luồng thứ nhất.",
                    "asset_member": "occurrence_a.png",
                    "occurrence_id": "occurrence_a",
                },
                {
                    "page": 2,
                    "caption": "Sơ đồ thứ hai.",
                    "alt_text": "Luồng thứ hai.",
                    "asset_member": "occurrence_b.png",
                    "occurrence_id": "occurrence_b",
                },
            ],
        }],
    }
    contract = build_pack_contract(
        source=source,
        curriculum=curriculum,
        media_root=media,
    )
    page_chunk = next(item for item in source.chunks if item.page_start == 2)
    plan = SimpleNamespace(
        atom_id="atom_visuals",
        source_ref_ids=[page_chunk.source_ref.source_ref_id],
    )
    outline = SimpleNamespace(modules=[SimpleNamespace(atom_plans=[plan])])
    lesson = SimpleNamespace(
        atom_id="atom_visuals",
        source_ref_ids=[page_chunk.source_ref.source_ref_id],
        illustration_source_ref_ids=[],
    )
    generated = _attach_curriculum_illustrations(
        corpus=source,
        curriculum=curriculum,
        workdir=tmp_path,
        outline=outline,
        lessons=[lesson],
        content_id_seed=source.source_hash,
    )

    assert contract.expected_counts.illustrations == 2
    assert len(generated) == 2
    assert len({item.illustration_id for item in generated}) == 2
    assert {item.page_number for item in generated} == {2}
    assert {item.alt_text for item in generated} == {"Luồng thứ nhất.", "Luồng thứ hai."}
    assert illustration_identities_sha256(
        SimpleNamespace(illustrations=generated)
    ) == contract.illustration_identities_sha256


def test_contract_gate_rejects_truncation_metadata_and_assignment_swaps(
    curriculum_pack,
    curriculum_contract,
):
    report = validate_pack(
        curriculum_pack,
        curriculum_contract,
        require_contract=True,
        require_frozen_outline=True,
    )
    assert report.valid, report.issues

    truncated = curriculum_pack.model_copy(deep=True)
    truncated.lessons.pop()
    codes = {item.code for item in validate_pack(truncated, curriculum_contract).issues}
    assert "PACK_CONTRACT_COUNT_MISMATCH" in codes

    metadata_changed = curriculum_pack.model_copy(deep=True)
    metadata_changed.source_refs[0].anchor_end += 1
    codes = {item.code for item in validate_pack(metadata_changed, curriculum_contract).issues}
    assert "PACK_CONTRACT_SOURCE_RECORDS_MISMATCH" in codes

    swapped = curriculum_pack.model_copy(deep=True)
    first, second = swapped.modules[0].atom_plans[:2]
    first.source_ref_ids, second.source_ref_ids = second.source_ref_ids, first.source_ref_ids
    for lesson in swapped.lessons:
        plan = next(item for item in swapped.modules[0].atom_plans if item.atom_id == lesson.atom_id)
        lesson.source_ref_ids = list(plan.source_ref_ids)
    codes = {item.code for item in validate_pack(swapped, curriculum_contract).issues}
    assert "PACK_CONTRACT_MODULE_SOURCE_ASSIGNMENT_MISMATCH" in codes
    assert "PACK_CONTRACT_OUTLINE_MISMATCH" in codes


def test_contract_is_required_and_outline_must_be_candidate_frozen(
    curriculum_pack,
    curriculum_contract,
):
    missing_codes = {
        item.code for item in validate_pack(curriculum_pack, require_contract=True).issues
    }
    assert "PACK_CONTRACT_REQUIRED" in missing_codes

    base = curriculum_contract.model_copy(update={"outline_assignment_sha256": None})
    unfrozen_codes = {
        item.code
        for item in validate_pack(
            curriculum_pack,
            base,
            require_contract=True,
            require_frozen_outline=True,
        ).issues
    }
    assert "PACK_CONTRACT_OUTLINE_UNFROZEN" in unfrozen_codes
    frozen = freeze_outline_assignment(base, curriculum_pack)
    assert frozen.outline_assignment_sha256 == curriculum_contract.outline_assignment_sha256
    assert not check_pack_contract(curriculum_pack, frozen, require_frozen_outline=True)


def test_contract_freeze_rejects_wrong_stable_identity_graph(curriculum_pack, curriculum_contract):
    base = curriculum_contract.model_copy(update={"outline_assignment_sha256": None})
    tampered = curriculum_pack.model_copy(deep=True)
    tampered.full_lessons[0].full_lesson_id = "full_replaced"
    try:
        freeze_outline_assignment(base, tampered)
    except ValueError as error:
        assert "PACK_CONTRACT_IDENTITY_GRAPH_MISMATCH" in str(error)
    else:
        raise AssertionError("Contract freeze accepted a substituted stable ID")


def test_contract_binds_audited_illustration_bytes_and_metadata(curriculum_pack, curriculum_contract):
    pack = curriculum_pack
    source_ref = pack.source_refs[0]
    pack.lessons[0].illustration_source_ref_ids = [source_ref.source_ref_id]
    pack.illustrations = [IllustrationAsset(
        illustration_id="illustration_contract",
        source_ref_id=source_ref.source_ref_id,
        asset_member="illustration_contract.png",
        mime_type="image/png",
        sha256=sha256_bytes(PNG_1X1),
        byte_size=len(PNG_1X1),
        width=1,
        height=1,
        page_number=source_ref.page_start,
        caption="Hình đã duyệt.",
        alt_text="Hình minh họa đã duyệt.",
    )]
    contract = curriculum_contract.model_copy(deep=True)
    contract.expected_counts.illustrations = 1
    contract.illustration_identities_sha256 = illustration_identities_sha256(pack)
    assert not check_pack_contract(pack, contract, require_frozen_outline=True)

    replacement = PNG_1X1 + b"replacement"
    pack.illustrations[0].sha256 = sha256_bytes(replacement)
    pack.illustrations[0].byte_size = len(replacement)
    codes = {item.code for item in validate_pack(pack, contract).issues}
    assert "PACK_CONTRACT_ILLUSTRATIONS_MISMATCH" in codes
