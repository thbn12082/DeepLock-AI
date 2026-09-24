from __future__ import annotations

import json

import pymupdf
import pytest

from deeplock_content.chunking import build_corpus
from deeplock_content.catalog_build import _visual_audit_complete
from deeplock_content.generation import ModelResponse
from deeplock_content.generation.builder import _with_curriculum_page_overrides
from deeplock_content.models import Document
from deeplock_content.settings import Settings
from deeplock_content.util import sha256_bytes, write_json
from deeplock_content.vision_audit import audit_catalog_visual_pages


class FakeVision:
    def __init__(self):
        self.calls = 0

    def structured(self, **kwargs):
        self.calls += 1
        assert kwargs["output_verbosity"] == "high"
        assert kwargs["input_image_data_urls"][0].startswith("data:image/png;base64,")
        pages = json.loads(
            kwargs["input_text"].split("REQUIRED_PAGE_NUMBERS_IN_ORDER: ", 1)[1].splitlines()[0]
        )
        return ModelResponse(
            data={
                "pages": [{
                    "page_number": page,
                    "content_kind": "TECHNICAL",
                    "transcription": "Prometheus thu thập metric từ API.",
                    "visual_explanation": "Mũi tên nối API tới Prometheus rồi tới Grafana dashboard.",
                    "useful_illustration": True,
                    "caption": "Luồng monitoring Prometheus và Grafana.",
                } for page in pages]
            },
            response_id="vision_1",
            model_snapshot="cx/gpt-5.5",
        )


class SplittingPdfVision(FakeVision):
    def __init__(self, *, terminal_failures: set[int] | None = None):
        super().__init__()
        self.batch_sizes: list[int] = []
        self.terminal_failures = terminal_failures or set()

    def structured(self, **kwargs):
        pages = json.loads(
            kwargs["input_text"].split("REQUIRED_PAGE_NUMBERS_IN_ORDER: ", 1)[1].splitlines()[0]
        )
        self.batch_sizes.append(len(pages))
        if len(pages) > 1:
            self.calls += 1
            raise RuntimeError("9router 502 streaming reset")
        if pages[0] in self.terminal_failures:
            self.calls += 1
            raise RuntimeError(f"terminal page failure: {pages[0]}")
        return super().structured(**kwargs)


class ClassifiedPdfVision(FakeVision):
    def structured(self, **kwargs):
        self.calls += 1
        pages = json.loads(
            kwargs["input_text"].split("REQUIRED_PAGE_NUMBERS_IN_ORDER: ", 1)[1].splitlines()[0]
        )
        by_page = {
            1: {
                "content_kind": "NONTECHNICAL",
                "transcription": "Practical MLOps",
                "visual_explanation": "Decorative cover layout with a logo and presenter name.",
                "useful_illustration": False,
                "caption": "Course cover",
            },
            2: {
                "content_kind": "NONTECHNICAL",
                "transcription": "Agenda and housekeeping",
                "visual_explanation": "Administrative agenda layout and section dividers.",
                "useful_illustration": False,
                "caption": "Agenda",
            },
            3: {
                "content_kind": "TECHNICAL",
                "transcription": "y = W x + b",
                "visual_explanation": "Arrows show input x transformed by W before bias b is added.",
                "useful_illustration": True,
                "caption": "Affine transformation y = W x + b",
            },
            4: {
                "content_kind": "NONTECHNICAL",
                "transcription": "Thank you",
                "visual_explanation": "Closing layout with contact details and decorative shapes.",
                "useful_illustration": False,
                "caption": "Closing slide",
            },
        }
        return ModelResponse(
            data={
                "pages": [{"page_number": page, **by_page[page]} for page in pages]
            },
            response_id="vision_classified",
            model_snapshot="cx/gpt-5.5",
        )


def _settings() -> Settings:
    return Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=5,
        generator_reasoning_effort="medium",
        review_reasoning_effort="high",
        generator_prompt_version="course-v1",
        review_prompt_version="reviewer-v1",
        max_repair_rounds=2,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )


def test_visual_audit_adds_missing_page_override_through_multimodal_adapter(tmp_path):
    pdf = tmp_path / "monitoring.pdf"
    raw = pymupdf.open()
    raw.new_page().insert_text((72, 72), "Trang có text")
    raw.new_page().draw_line((80, 100), (400, 100), width=4)
    raw.save(pdf)
    raw.close()
    document = Document(
        document_id="doc_monitoring",
        filename="monitoring.pdf",
        mime_type="application/pdf",
        sha256=sha256_bytes(pdf.read_bytes()),
        page_count=2,
        created_at="2026-01-01T00:00:00Z",
        source_path=str(pdf),
    )
    corpus = build_corpus([(document, [(1, "Trang có text")])])
    workspace = tmp_path / "work" / "lec_monitoring"
    write_json(workspace / "source.json", corpus.model_dump(mode="json"))
    write_json(workspace / "curriculum.json", {
        "course_id": "course_monitoring",
        "title": "Monitoring",
        "description": "MLOps",
        "modules": [{
            "module_id": "lecture_monitoring",
            "title": "Monitoring",
            "output": "monitoring.pdf",
            "visual_audit_required_pages": [2],
            "illustrations": [{
                "page": 2,
                "asset_member": "monitoring-page-2.png",
                "caption": "Original slide illustration.",
            }],
        }],
    })
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [{
            "workspace": str(workspace),
            "visual_audit_required_pages": [2],
        }],
    })
    fake = FakeVision()
    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )
    curriculum = json.loads((workspace / "curriculum.json").read_text(encoding="utf-8"))
    assert report["pages"] == 1
    assert fake.calls == 1
    assert "Prometheus" in curriculum["modules"][0]["page_text_overrides"]["2"]
    assert curriculum["modules"][0]["illustrations"][0]["caption"] == (
        "Lu\u1ed3ng monitoring Prometheus v\u00e0 Grafana."
    )
    cache = json.loads((workspace / "vision-audit.json").read_text(encoding="utf-8"))
    assert cache["pdf_payload_sha256"] == document.sha256
    assert cache["audit_contract"]["response_verbosity"] == "high"


def test_visual_audit_excludes_nontechnical_pages_and_retains_technical_visual(
    tmp_path,
):
    pdf = tmp_path / "mixed-slides.pdf"
    raw = pymupdf.open()
    for label in ("Practical MLOps", "Agenda", "Technical diagram", "Thank you"):
        raw.new_page().insert_text((72, 72), label)
    raw.save(pdf)
    raw.close()
    document = Document(
        document_id="doc_mixed_slides",
        filename=pdf.name,
        mime_type="application/pdf",
        sha256=sha256_bytes(pdf.read_bytes()),
        page_count=4,
        created_at="2026-01-01T00:00:00Z",
        source_path=str(pdf),
    )
    # Cover and agenda have short OCR chunks. The technical diagram and closing
    # slide are image-only from the extractor's perspective.
    corpus = build_corpus([(
        document,
        [(1, "Practical MLOps"), (2, "Agenda and housekeeping")],
    )])
    workspace = tmp_path / "work" / "lec_mixed_slides"
    write_json(workspace / "source.json", corpus.model_dump(mode="json"))
    write_json(workspace / "curriculum.json", {
        "modules": [{
            "output": pdf.name,
            "visual_audit_required_pages": [1, 2, 3, 4],
            "illustrations": [
                {"page": 1, "asset_member": "cover.png", "caption": "Cover"},
                {"page": 3, "asset_member": "equation.png", "caption": "Equation"},
                {"page": 4, "asset_member": "thanks.png", "caption": "Thanks"},
            ],
        }],
    })
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [{
            "workspace": str(workspace),
            "visual_audit_required_pages": [1, 2, 3, 4],
        }],
    })

    fake = ClassifiedPdfVision()
    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )
    curriculum = json.loads((workspace / "curriculum.json").read_text(encoding="utf-8"))
    module = curriculum["modules"][0]

    assert report["pages"] == 4
    assert module["visual_audit_required_pages"] == [3]
    assert module["page_text_supplements"] == {}
    assert set(module["page_text_overrides"]) == {"3"}
    assert "y = W x + b" in module["page_text_overrides"]["3"]
    assert {item["page"] for item in curriculum["excluded_nontechnical_pages"]} == {1, 2}
    assert {item["page"] for item in curriculum["excluded_image_only_pages"]} == {4}
    assert [item["page"] for item in module["illustrations"]] == [3]
    assert module["illustrations"][0]["caption"] == "Affine transformation y = W x + b"

    # Both packaging completeness and source-page coverage accept the explicit
    # exclusions, while no cover/agenda/thanks/layout prose reaches a lesson.
    _visual_audit_complete(curriculum)
    learner_corpus = _with_curriculum_page_overrides(corpus, curriculum)
    assert {chunk.page_start for chunk in learner_corpus.chunks} == {3}
    assert all(
        token not in " ".join(chunk.normalized_text for chunk in learner_corpus.chunks).casefold()
        for token in ("practical mlops", "agenda", "thank you", "decorative", "layout")
    )

    # Resume reuses the cached classifications and does not duplicate exclusion
    # entries even though excluded pages left the module's technical page list.
    resumed = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )
    assert resumed["pages"] == 4
    assert fake.calls == 1
    resumed_curriculum = json.loads(
        (workspace / "curriculum.json").read_text(encoding="utf-8")
    )
    assert len(resumed_curriculum["excluded_nontechnical_pages"]) == 2
    assert len(resumed_curriculum["excluded_image_only_pages"]) == 1


def test_visual_audit_rejects_pdf_bytes_that_do_not_match_corpus_before_cache_or_render(
    tmp_path,
):
    pdf = tmp_path / "source.pdf"
    raw = pymupdf.open()
    raw.new_page().insert_text((72, 72), "Original")
    raw.save(pdf)
    raw.close()
    original_sha256 = sha256_bytes(pdf.read_bytes())
    document = Document(
        document_id="doc_source",
        filename="source.pdf",
        mime_type="application/pdf",
        sha256=original_sha256,
        page_count=1,
        created_at="2026-01-01T00:00:00Z",
        source_path=str(pdf),
    )
    corpus = build_corpus([(document, [(1, "Original")])])
    workspace = tmp_path / "work" / "lec_source"
    write_json(workspace / "source.json", corpus.model_dump(mode="json"))
    write_json(workspace / "curriculum.json", {
        "modules": [{
            "output": "source.pdf",
            "visual_audit_required_pages": [1],
            "illustrations": [],
        }],
    })
    write_json(workspace / "vision-audit.json", {
        "schema_version": "1.0",
        "document_sha256": original_sha256,
        "pages": [{
            "page_number": 1,
            "content_kind": "TECHNICAL",
            "transcription": "Stale cache must not be trusted.",
            "visual_explanation": "Stale cache must not be trusted.",
            "useful_illustration": False,
            "caption": "",
        }],
    })
    changed = pymupdf.open()
    changed.new_page().insert_text((72, 72), "Changed after extraction")
    changed.save(pdf)
    changed.close()
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [{
            "workspace": str(workspace),
            "visual_audit_required_pages": [1],
        }],
    })
    fake = FakeVision()
    with pytest.raises(RuntimeError, match="Vision audit incomplete"):
        audit_catalog_visual_pages(
            plan_path=plan,
            settings=_settings(),
            adapter=fake,
            resume=True,
        )
    assert fake.calls == 0
    report = json.loads(
        (tmp_path / "vision-audit-report.json").read_text(encoding="utf-8")
    )
    assert "PDF hash does not match" in report["failures"][0]["error"]


def _four_page_pdf_workspace(tmp_path):
    pdf = tmp_path / "four-pages.pdf"
    raw = pymupdf.open()
    for page_number in range(1, 5):
        raw.new_page().insert_text((72, 72), f"Technical diagram {page_number}")
    raw.save(pdf)
    raw.close()
    document = Document(
        document_id="doc_four_pages",
        filename=pdf.name,
        mime_type="application/pdf",
        sha256=sha256_bytes(pdf.read_bytes()),
        page_count=4,
        created_at="2026-01-01T00:00:00Z",
        source_path=str(pdf),
    )
    corpus = build_corpus([(
        document,
        [(page, f"Technical diagram {page}") for page in range(1, 5)],
    )])
    workspace = tmp_path / "work" / "lec_four_pages"
    write_json(workspace / "source.json", corpus.model_dump(mode="json"))
    write_json(workspace / "curriculum.json", {
        "modules": [{
            "output": pdf.name,
            "visual_audit_required_pages": [1, 2, 3, 4],
            "illustrations": [],
        }],
    })
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [{
            "workspace": str(workspace),
            "visual_audit_required_pages": [1, 2, 3, 4],
        }],
    })
    return workspace, plan


def test_pdf_failed_batch_recursively_splits_and_caches_each_success(tmp_path):
    workspace, plan = _four_page_pdf_workspace(tmp_path)
    fake = SplittingPdfVision()

    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )

    assert report["pages"] == 4
    assert fake.batch_sizes == [4, 2, 1, 1, 2, 1, 1]
    cache = json.loads((workspace / "vision-audit.json").read_text(encoding="utf-8"))
    assert [item["page_number"] for item in cache["pages"]] == [1, 2, 3, 4]
    assert cache["audit_contract"]["batch_size"] == 4
    assert cache["audit_contract"]["response_verbosity"] == "high"


def test_pdf_terminal_single_page_failure_preserves_siblings_for_resume(tmp_path):
    workspace, plan = _four_page_pdf_workspace(tmp_path)
    failing = SplittingPdfVision(terminal_failures={2})

    with pytest.raises(RuntimeError, match="Vision audit incomplete"):
        audit_catalog_visual_pages(
            plan_path=plan,
            settings=_settings(),
            adapter=failing,
            resume=True,
        )

    assert failing.batch_sizes == [4, 2, 1, 1, 2, 1, 1]
    partial = json.loads((workspace / "vision-audit.json").read_text(encoding="utf-8"))
    assert [item["page_number"] for item in partial["pages"]] == [1, 3, 4]
    failure_report = json.loads(
        (tmp_path / "vision-audit-report.json").read_text(encoding="utf-8")
    )
    assert "terminal page failure: 2" in failure_report["failures"][0]["error"]

    resumed = SplittingPdfVision()
    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=resumed,
        resume=True,
    )
    assert report["pages"] == 4
    assert resumed.batch_sizes == [1]
    completed = json.loads((workspace / "vision-audit.json").read_text(encoding="utf-8"))
    assert [item["page_number"] for item in completed["pages"]] == [1, 2, 3, 4]
