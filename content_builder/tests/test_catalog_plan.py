from __future__ import annotations

import json
from types import SimpleNamespace

import pymupdf

from deeplock_content.chunking import build_corpus
from deeplock_content.curriculum.catalog_plan import (
    category_for_document,
    plan_lecture_packs,
)
from deeplock_content.models import Document
from deeplock_content.extractors.ooxml_media import (
    OoxmlMediaAsset,
    OoxmlMediaCounts,
    OoxmlMediaManifest,
    OoxmlMediaOccurrence,
    ooxml_media_manifest_sha256,
)
from deeplock_content.util import sha256_bytes, write_json


def test_category_mapping_keeps_mlops_and_aivin_discoverable():
    mlops = SimpleNamespace(
        filename="mlflow.pdf",
        logical_path="Full DL .zip/AIO - Sao chép/Extra 3. MLOps/8. MLFlow.pdf",
    )
    aivin = SimpleNamespace(
        filename="llmops.pptx",
        logical_path=(
            "aivin.zip/aivin/ai20k-cohort-1/23 - Day 22/"
            "01 - Day 22 - Track 2 - LLMOPS-prompt-versioning.pptx"
        ),
    )
    assert category_for_document(mlops).category_id == "cat_mlops"
    assert category_for_document(aivin).category_id == "cat_aivin_ai20k"


def test_planner_creates_one_workspace_per_unique_document(tmp_path):
    document = Document(
        document_id="doc_unique",
        filename="doc_unique.pdf",
        mime_type="application/pdf",
        sha256="sha256:" + "2" * 64,
        page_count=2,
        created_at="2026-01-01T00:00:00Z",
        source_path=None,
    )
    corpus = build_corpus([(
        document,
        [(1, "MLOps với MLflow."), (2, "Theo dõi experiment và model registry.")],
    )])
    plan = plan_lecture_packs(
        corpus=corpus,
        output_root=tmp_path / "catalog",
        render_visual_assets=False,
    )
    assert plan["counts"]["unique_lectures"] == 1
    lecture = plan["lectures"][0]
    workspace = tmp_path / "catalog" / "work" / lecture["lecture_id"]
    assert (workspace / "source.json").is_file()
    assert (workspace / "curriculum.json").is_file()
    curriculum = json.loads((workspace / "curriculum.json").read_text(encoding="utf-8"))
    assert curriculum["outline_mode"] == "SEMANTIC_V2"
    assert curriculum["content_identity_namespace"] == "v3"
    assert "deterministic_outline" not in curriculum
    assert curriculum["pages_per_part"] == 20
    assert curriculum["target_pages_per_atom"] == 2
    assert curriculum["max_atoms_per_part"] == 10
    # Synthetic corpora without a source PDF cannot be sent to the visual
    # audit: there are no page pixels to inspect.
    assert lecture["visual_audit_required_pages"] == []


def test_planner_materializes_every_ooxml_occurrence_and_converts_jpeg(tmp_path):
    ingest = tmp_path / "ingest"
    documents = ingest / "documents"
    media_root = ingest / "ooxml-media"
    documents.mkdir(parents=True)
    media_root.mkdir()

    pixmap = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 2, 2), 0)
    pixmap.clear_with(0x33AA55)
    jpeg = pixmap.tobytes("jpeg")
    digest = sha256_bytes(jpeg)
    media_id = "media_fixture"
    source_asset = media_root / f"media_{digest.removeprefix('sha256:')}.jpg"
    source_asset.write_bytes(jpeg)
    occurrence_ids = ["occ_slide_two_a", "occ_slide_two_b", "occ_slide_three"]
    asset = OoxmlMediaAsset(
        media_id=media_id,
        sha256=digest,
        mime_type="image/jpeg",
        suffix=".jpg",
        byte_size=len(jpeg),
        extracted_path=f"ooxml-media/{source_asset.name}",
        occurrence_ids=occurrence_ids,
        document_ids=["doc_pptx"],
    )
    occurrences = [
        OoxmlMediaOccurrence(
            occurrence_id=occurrence_ids[0],
            media_id=media_id,
            document_id="doc_pptx",
            source_format="PPTX",
            package_part="ppt/media/diagram.jpg",
            relationship_source_part="ppt/slides/slide2.xml",
            relationship_id="rId1",
            page_number=2,
            section_title="Deployment pipeline",
            alt_text="Sơ đồ deployment pipeline.",
            mapping_kind="PPTX_SLIDE_EMBED",
        ),
        OoxmlMediaOccurrence(
            occurrence_id=occurrence_ids[1],
            media_id=media_id,
            document_id="doc_pptx",
            source_format="PPTX",
            package_part="ppt/media/diagram.jpg",
            relationship_source_part="ppt/slides/slide2.xml",
            relationship_id="rId2",
            page_number=2,
            section_title="Deployment pipeline",
            mapping_kind="PPTX_SLIDE_EMBED",
        ),
        OoxmlMediaOccurrence(
            occurrence_id=occurrence_ids[2],
            media_id=media_id,
            document_id="doc_pptx",
            source_format="PPTX",
            package_part="ppt/media/diagram.jpg",
            relationship_source_part="ppt/slides/slide3.xml",
            relationship_id="rId1",
            page_number=3,
            section_title="Image-only architecture",
            mapping_kind="PPTX_SLIDE_EMBED",
        ),
    ]
    placeholder = "sha256:" + "0" * 64
    manifest = OoxmlMediaManifest(
        manifest_sha256=placeholder,
        assets=[asset],
        occurrences=occurrences,
        counts=OoxmlMediaCounts(
            documents_scanned=1,
            documents_with_media=1,
            package_media_parts=1,
            media_occurrences=3,
            unique_assets=1,
            exact_duplicate_media_parts=0,
            unreferenced_media_parts=0,
            by_mime_type={"image/jpeg": 1},
        ),
    )
    manifest = manifest.model_copy(update={
        "manifest_sha256": ooxml_media_manifest_sha256(manifest),
    })
    manifest_path = ingest / "ooxml-media.json"
    write_json(manifest_path, manifest.model_dump(mode="json"))

    document = Document(
        document_id="doc_pptx",
        filename="doc_pptx.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        sha256="sha256:" + "3" * 64,
        page_count=3,
        created_at="2026-01-01T00:00:00Z",
        source_path=str(documents / "doc_pptx.pptx"),
        display_name="Deployment.pptx",
        source_format="PPTX",
    )
    corpus = build_corpus([(
        document,
        [
            (1, "Introduction to deployment."),
            (2, "CI/CD deployment pipeline, artifact registry, rollout strategy, and monitoring."),
        ],
    )])
    output = tmp_path / "catalog"
    plan = plan_lecture_packs(
        corpus=corpus,
        output_root=output,
        ooxml_media_manifest=manifest_path,
    )

    lecture = plan["lectures"][0]
    workspace = output / "work" / lecture["lecture_id"]
    curriculum = json.loads((workspace / "curriculum.json").read_text(encoding="utf-8"))
    specs = curriculum["modules"][0]["illustrations"]
    assert len(specs) == 3
    assert len({item["asset_member"] for item in specs}) == 3
    assert {item["occurrence_id"] for item in specs} == set(occurrence_ids)
    payloads = [(workspace / "media" / item["asset_member"]).read_bytes() for item in specs]
    assert all(payload[:8] == b"\x89PNG\r\n\x1a\n" for payload in payloads)
    assert len(set(payloads)) == 1
    assert lecture["ooxml_media_occurrences"] == 3
    assert lecture["ooxml_image_only_pages"] == [3]
    assert plan["counts"]["ooxml_media_occurrences"] == 3
    audits = curriculum["modules"][0]["ooxml_visual_audit_occurrences"]
    assert {item["occurrence_id"] for item in audits} == {
        "occ_slide_two_b",
        "occ_slide_three",
    }
    image_only = next(item for item in audits if item["occurrence_id"] == "occ_slide_three")
    assert "IMAGE_ONLY_PAGE" in image_only["reasons"]
