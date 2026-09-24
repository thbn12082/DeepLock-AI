from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from deeplock_content.catalog_build import _visual_audit_complete
from deeplock_content.generation import ModelResponse
from deeplock_content.settings import Settings
from deeplock_content.util import sha256_bytes, write_json
from deeplock_content.vision_audit import audit_catalog_visual_pages


class FakeOoxmlVision:
    def __init__(
        self,
        *,
        useful: bool = True,
        reject_multi: bool = False,
        terminal_failures: set[str] | None = None,
    ):
        self.calls = 0
        self.batch_sizes: list[int] = []
        self.useful = useful
        self.reject_multi = reject_multi
        self.terminal_failures = terminal_failures or set()

    def structured(self, **kwargs):
        self.calls += 1
        assert kwargs["output_verbosity"] == "high"
        assert kwargs["schema_name"].startswith("ooxml_lecture_images_")
        urls = kwargs["input_image_data_urls"]
        assert all(item.startswith("data:image/png;base64,") for item in urls)
        self.batch_sizes.append(len(urls))
        hashes = json.loads(
            kwargs["input_text"]
            .split("REQUIRED_PNG_SHA256_IN_ORDER: ", 1)[1]
            .splitlines()[0]
        )
        assert len(hashes) == len(urls)
        if self.reject_multi and len(hashes) > 1:
            raise RuntimeError("9router 502 streaming reset")
        if len(hashes) == 1 and hashes[0] in self.terminal_failures:
            raise RuntimeError(f"terminal image failure: {hashes[0]}")
        return ModelResponse(
            data={
                "images": [{
                    "image_number": index,
                    "transcription": "Nội dung OCR giữ nguyên từ hình.",
                    "visual_explanation": "Sơ đồ nối dữ liệu vào mô hình rồi tạo dự đoán.",
                    "useful_illustration": self.useful,
                    "caption": "Sơ đồ luồng suy luận " + "chi tiết " * 90,
                } for index in range(1, len(hashes) + 1)]
            },
            response_id=f"ooxml_{self.calls}",
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


def _png_payload(label: str = "DATA -> MODEL -> PREDICTION") -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=320, height=180)
    page.insert_text((32, 70), label)
    payload = page.get_pixmap(alpha=False).tobytes("png")
    document.close()
    return payload


def _occurrence(
    *,
    occurrence_id: str,
    asset_member: str,
    page: int,
    source_hash_digit: str,
) -> dict[str, object]:
    return {
        "occurrence_id": occurrence_id,
        "asset_member": asset_member,
        "page": page,
        "ooxml_media_sha256": "sha256:" + source_hash_digit * 64,
        "caption": "Chú thích từ tài liệu.",
        "alt_text": "Mô tả từ tài liệu.",
        "reasons": ["MISSING_ALT_TEXT"],
        "page_text_length": 120,
    }


def _workspace(
    root: Path,
    *,
    name: str,
    occurrence: dict[str, object],
    png: bytes,
) -> Path:
    workspace = root / name
    media = workspace / "media"
    media.mkdir(parents=True)
    (media / str(occurrence["asset_member"])).write_bytes(png)
    illustration = {
        key: value
        for key, value in occurrence.items()
        if key not in {"reasons", "page_text_length"}
    }
    write_json(workspace / "curriculum.json", {
        "modules": [{
            "output": f"{name}.pptx",
            "visual_audit_required_pages": [],
            "ooxml_visual_audit_occurrences": [occurrence],
            "illustrations": [illustration],
        }],
    })
    return workspace


def test_ooxml_audit_deduplicates_exact_png_globally_and_is_idempotent(tmp_path):
    payload = _png_payload()
    first = _occurrence(
        occurrence_id="occ_first",
        asset_member="first.png",
        page=1,
        source_hash_digit="1",
    )
    second = _occurrence(
        occurrence_id="occ_second",
        asset_member="second.png",
        page=3,
        source_hash_digit="2",
    )
    workspace_a = _workspace(tmp_path, name="lecture_a", occurrence=first, png=payload)
    workspace_b = _workspace(tmp_path, name="lecture_b", occurrence=second, png=payload)
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [
            {
                "workspace": str(workspace_a),
                "visual_audit_required_pages": [],
                "ooxml_visual_audit_occurrences": [first],
            },
            {
                "workspace": str(workspace_b),
                "visual_audit_required_pages": [],
                "ooxml_visual_audit_occurrences": [second],
            },
        ],
    })

    fake = FakeOoxmlVision()
    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )
    assert fake.calls == 1
    assert fake.batch_sizes == [1]
    assert report["pages"] == 0
    assert report["ooxml"]["expected_occurrences"] == 2
    assert report["ooxml"]["occurrences"] == 2
    assert report["ooxml"]["unique_images"] == 1
    assert report["ooxml"]["completed_unique_images"] == 1

    before: list[str] = []
    for workspace, occurrence_id in (
        (workspace_a, "occ_first"),
        (workspace_b, "occ_second"),
    ):
        curriculum = json.loads(
            (workspace / "curriculum.json").read_text(encoding="utf-8")
        )
        module = curriculum["modules"][0]
        assert module["ooxml_visual_audit_completed_occurrence_ids"] == [occurrence_id]
        assert "Nội dung OCR" in next(iter(module["page_text_supplements"].values()))
        assert len(module["illustrations"][0]["caption"]) <= 500
        assert len(module["illustrations"][0]["alt_text"]) <= 500
        assert module["ooxml_visual_audit_completed"][0]["png_sha256"] == sha256_bytes(payload)
        _visual_audit_complete(
            curriculum,
            media_root=workspace / "media",
            expected_ooxml_contract_sha256=module[
                "ooxml_visual_audit_contract_sha256"
            ],
        )
        before.append(json.dumps(module["page_text_supplements"], sort_keys=True))

    audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )
    assert fake.calls == 1
    for index, workspace in enumerate((workspace_a, workspace_b)):
        curriculum = json.loads(
            (workspace / "curriculum.json").read_text(encoding="utf-8")
        )
        assert json.dumps(
            curriculum["modules"][0]["page_text_supplements"],
            sort_keys=True,
        ) == before[index]

    cache_path = tmp_path / "ooxml-vision-audit.json"
    tampered = json.loads(cache_path.read_text(encoding="utf-8"))
    only_result = next(iter(tampered["results"].values()))
    only_result["transcription"] = "Nội dung đã bị sửa ngoài cache."
    write_json(cache_path, tampered)
    audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )
    assert fake.calls == 2

    audit_catalog_visual_pages(
        plan_path=plan,
        settings=replace(_settings(), vision_reasoning_effort="medium"),
        adapter=fake,
        resume=True,
    )
    assert fake.calls == 3


def test_ooxml_decorative_asset_is_dropped_but_readable_text_is_kept(tmp_path):
    payload = _png_payload()
    occurrence = _occurrence(
        occurrence_id="occ_decorative",
        asset_member="decorative.png",
        page=2,
        source_hash_digit="3",
    )
    workspace = _workspace(tmp_path, name="lecture", occurrence=occurrence, png=payload)
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [{
            "workspace": str(workspace),
            "visual_audit_required_pages": [],
            "ooxml_visual_audit_occurrences": [occurrence],
        }],
    })
    audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=FakeOoxmlVision(useful=False),
        resume=True,
    )
    curriculum = json.loads((workspace / "curriculum.json").read_text(encoding="utf-8"))
    module = curriculum["modules"][0]
    assert module["illustrations"] == []
    assert "Nội dung OCR" in module["page_text_supplements"]["2"]
    _visual_audit_complete(curriculum, media_root=workspace / "media")
    module["ooxml_visual_audit_completed_occurrence_ids"] = []
    with pytest.raises(ValueError, match="incomplete"):
        _visual_audit_complete(curriculum, media_root=workspace / "media")


def _four_unique_ooxml_workspaces(tmp_path):
    lectures = []
    payload_hashes: list[str] = []
    for index in range(1, 5):
        payload = _png_payload(f"PIPELINE IMAGE {index}")
        payload_hashes.append(sha256_bytes(payload))
        occurrence = _occurrence(
            occurrence_id=f"occ_split_{index}",
            asset_member=f"split-{index}.png",
            page=index,
            source_hash_digit=str(index),
        )
        workspace = _workspace(
            tmp_path,
            name=f"lecture_split_{index}",
            occurrence=occurrence,
            png=payload,
        )
        lectures.append({
            "workspace": str(workspace),
            "visual_audit_required_pages": [],
            "ooxml_visual_audit_occurrences": [occurrence],
        })
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {"lectures": lectures})
    return plan, sorted(payload_hashes)


def test_ooxml_failed_batch_recursively_splits_and_caches_each_success(tmp_path):
    plan, payload_hashes = _four_unique_ooxml_workspaces(tmp_path)
    fake = FakeOoxmlVision(reject_multi=True)

    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=fake,
        resume=True,
    )

    assert fake.batch_sizes == [4, 2, 1, 1, 2, 1, 1]
    assert report["ooxml"]["api_batches"] == 7
    assert report["ooxml"]["completed_unique_images"] == 4
    cache = json.loads((tmp_path / "ooxml-vision-audit.json").read_text(encoding="utf-8"))
    assert sorted(cache["results"]) == payload_hashes
    assert cache["audit_contract"]["batch_size"] == 4
    assert cache["audit_contract"]["response_verbosity"] == "high"


def test_ooxml_terminal_single_image_failure_preserves_siblings_for_resume(tmp_path):
    plan, payload_hashes = _four_unique_ooxml_workspaces(tmp_path)
    failed_hash = payload_hashes[1]
    failing = FakeOoxmlVision(
        reject_multi=True,
        terminal_failures={failed_hash},
    )

    with pytest.raises(RuntimeError, match="Vision audit incomplete"):
        audit_catalog_visual_pages(
            plan_path=plan,
            settings=_settings(),
            adapter=failing,
            resume=True,
        )

    partial = json.loads((tmp_path / "ooxml-vision-audit.json").read_text(encoding="utf-8"))
    assert sorted(partial["results"]) == [item for item in payload_hashes if item != failed_hash]
    failure_report = json.loads(
        (tmp_path / "vision-audit-report.json").read_text(encoding="utf-8")
    )
    assert failure_report["ooxml"]["api_batches"] == 7
    assert failure_report["ooxml"]["completed_unique_images"] == 3
    assert failure_report["ooxml"]["failures"] == [{
        "png_sha256": [failed_hash],
        "error_type": "RuntimeError",
        "error": f"terminal image failure: {failed_hash}",
    }]

    resumed = FakeOoxmlVision()
    report = audit_catalog_visual_pages(
        plan_path=plan,
        settings=_settings(),
        adapter=resumed,
        resume=True,
    )
    assert resumed.batch_sizes == [1]
    assert report["ooxml"]["completed_unique_images"] == 4
    completed = json.loads((tmp_path / "ooxml-vision-audit.json").read_text(encoding="utf-8"))
    assert sorted(completed["results"]) == payload_hashes
