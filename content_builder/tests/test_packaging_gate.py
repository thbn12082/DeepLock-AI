from __future__ import annotations

import base64
import json
import zipfile

import pytest

from deeplock_content.ai_review import run_ai_review
from deeplock_content.contracts import PACK_CONTRACT_FILENAME, pack_contract_sha256
from deeplock_content.generation import ModelResponse
from deeplock_content.models import IllustrationAsset
from deeplock_content.packaging.pack import (
    ReviewGateConfig,
    package_content,
    verify_ai_review_gate,
    verify_pack,
)
from deeplock_content.settings import Settings
from deeplock_content.util import sha256_bytes, sha256_json


class StrictApprover:
    def __init__(self):
        self.calls = 0

    def structured(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["input_text"])
        source_ids = [item["source_ref_id"] for item in payload["source_context"]["source_refs"]]
        reviews = []
        for item in payload["candidate_items"]:
            reviews.append({
                "item_type": item["item_type"],
                "item_id": item["item_id"],
                "item_revision": item["item_revision"],
                "candidate_hash": item["candidate_hash"],
                "source_hash": item["source_hash"],
                "verdict": "APPROVE",
                "issue_codes": [],
                "evidence_ref_ids": source_ids[:1],
                "independent_correct_option_id": "B" if item["item_type"] == "QUESTION" else None,
                "evidence_sufficient": True,
                "short_rationale": "Nguồn hỗ trợ kết luận.",
            })
        return ModelResponse({"reviews": reviews}, f"resp_{self.calls}", "cx/gpt-5.5-review")


def settings() -> Settings:
    return Settings(
        api_key=None, base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5", review_model="gpt-5.5",
        model_prefix="cx/", concurrency=5,
        generator_reasoning_effort="medium", review_reasoning_effort="high",
        generator_prompt_version="course-v1", review_prompt_version="reviewer-v1",
        max_repair_rounds=2, request_timeout_seconds=30, max_api_attempts=1,
    )


@pytest.fixture()
def approved(valid_pack, tmp_path):
    updated, report = run_ai_review(
        pack=valid_pack,
        workdir=tmp_path,
        settings=settings(),
        model="gpt-5.5",
        prompt_version="reviewer-v1",
        max_repair_rounds=2,
        resume=False,
        adapter=StrictApprover(),
    )
    return updated, report


def rehash(report):
    report["results_sha256"] = sha256_json(report["results"])


def test_legitimate_report_passes_gate_and_packages(approved, tmp_path):
    pack, report = approved
    verify_ai_review_gate(pack, report, ReviewGateConfig())
    output = tmp_path / "valid.dlpack"
    package_content(pack, report, output, ReviewGateConfig())
    manifest = verify_pack(output)
    assert manifest["ai_approved_count"] == len(report["results"])


def test_review_package_and_offline_verify_bind_frozen_contract(
    curriculum_pack,
    curriculum_contract,
    tmp_path,
):
    pack, report = run_ai_review(
        pack=curriculum_pack,
        workdir=tmp_path,
        settings=settings(),
        model="gpt-5.5",
        prompt_version="reviewer-v1",
        max_repair_rounds=2,
        resume=False,
        adapter=StrictApprover(),
        contract=curriculum_contract,
        require_contract=True,
        require_frozen_outline=True,
    )
    expected_hash = pack_contract_sha256(curriculum_contract)
    assert report["pack_contract_sha256"] == expected_hash
    output = tmp_path / "contracted.dlpack"
    package_content(
        pack,
        report,
        output,
        ReviewGateConfig(),
        contract=curriculum_contract,
        require_contract=True,
        require_frozen_outline=True,
    )
    manifest = verify_pack(output)
    assert manifest["pack_contract_sha256"] == expected_hash
    with zipfile.ZipFile(output) as archive:
        assert PACK_CONTRACT_FILENAME in archive.namelist()

    stale_report = dict(report)
    stale_report["pack_contract_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(PermissionError, match="PackContract"):
        package_content(
            pack,
            stale_report,
            tmp_path / "stale-contract.dlpack",
            ReviewGateConfig(),
            contract=curriculum_contract,
            require_contract=True,
            require_frozen_outline=True,
        )


def test_forged_or_empty_report_fails_closed_without_output(approved, tmp_path):
    pack, report = approved
    report["results"] = []
    report["approved_items"] = 0
    rehash(report)
    output = tmp_path / "forged.dlpack"
    with pytest.raises(PermissionError, match="AI_REVIEW_REQUIRED"):
        package_content(pack, report, output, ReviewGateConfig())
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer_model", "gpt-5.6"),
        ("reviewer_configured_model", "cx/gpt-5.5-stale"),
        ("reviewer_model_snapshots", ["cx/gpt-5.5-stale"]),
        ("reviewer_prompt_version", "reviewer-v0"),
        ("reviewer_prompt_sha256", "sha256:" + "0" * 64),
        ("reviewer_schema_version", "0"),
        ("reviewer_schema_sha256", "sha256:" + "0" * 64),
        ("reviewer_reasoning_effort", "low"),
        ("store_responses", True),
    ],
)
def test_stale_reviewer_config_fails(approved, field, value):
    pack, report = approved
    report[field] = value
    with pytest.raises(PermissionError, match="AI_REVIEW_REQUIRED"):
        verify_ai_review_gate(pack, report, ReviewGateConfig())


def test_result_hash_and_exact_item_coverage_are_required(approved):
    pack, report = approved
    report["results_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(PermissionError, match="results hash"):
        verify_ai_review_gate(pack, report, ReviewGateConfig())

    pack, report = approved
    report["results"].pop()
    report["approved_items"] -= 1
    rehash(report)
    with pytest.raises(PermissionError, match="coverage"):
        verify_ai_review_gate(pack, report, ReviewGateConfig())


@pytest.mark.parametrize("mutation", ["empty", "unowned", "answer", "candidate", "source", "revision", "reject"])
def test_each_review_item_is_reverified_at_package_time(approved, mutation):
    pack, report = approved
    item = next(value for value in report["results"] if value["item_type"] == "QUESTION")
    if mutation == "empty":
        item["evidence_ref_ids"] = []
    elif mutation == "unowned":
        item["evidence_ref_ids"] = ["src_not_owned"]
    elif mutation == "answer":
        item["independent_correct_option_id"] = "A"
    elif mutation == "candidate":
        item["candidate_hash"] = "sha256:" + "0" * 64
    elif mutation == "source":
        item["source_hash"] = "sha256:" + "0" * 64
    elif mutation == "revision":
        item["item_revision"] += 1
    elif mutation == "reject":
        item["verdict"] = "REJECT"
        item["issue_codes"] = ["FAULT_INJECTION"]
    rehash(report)
    with pytest.raises(PermissionError, match="AI_REVIEW_REQUIRED"):
        verify_ai_review_gate(pack, report, ReviewGateConfig())


def test_full_candidate_hash_must_match(approved):
    pack, report = approved
    report["candidate_hash"] = "sha256:" + "0" * 64
    with pytest.raises(PermissionError, match="stale"):
        verify_ai_review_gate(pack, report, ReviewGateConfig())


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def approved_with_illustration(valid_pack, tmp_path):
    source_ref = valid_pack.source_refs[0]
    member = "illustration_gradient.png"
    valid_pack.lessons[0].illustration_source_ref_ids = [source_ref.source_ref_id]
    valid_pack.illustrations = [IllustrationAsset(
        illustration_id="illustration_gradient",
        source_ref_id=source_ref.source_ref_id,
        asset_member=member,
        mime_type="image/png",
        sha256=sha256_bytes(PNG_1X1),
        byte_size=len(PNG_1X1),
        width=1,
        height=1,
        page_number=source_ref.page_start,
        caption="Minh họa một bước cập nhật gradient.",
        alt_text="Sơ đồ cập nhật trọng số ngược hướng gradient.",
    )]
    media = tmp_path / "media"
    media.mkdir()
    (media / member).write_bytes(PNG_1X1)
    updated, report = run_ai_review(
        pack=valid_pack,
        workdir=tmp_path,
        settings=settings(),
        model="gpt-5.5",
        prompt_version="reviewer-v1",
        max_repair_rounds=2,
        resume=False,
        adapter=StrictApprover(),
    )
    return updated, report, media, member


def test_package_includes_and_verifies_hashed_illustration(valid_pack, tmp_path):
    pack, report, media, member = approved_with_illustration(valid_pack, tmp_path)
    output = tmp_path / "illustrated.dlpack"
    package_content(pack, report, output, ReviewGateConfig(), media_root=media)
    manifest = verify_pack(output)
    with zipfile.ZipFile(output) as archive:
        assert archive.read(member) == PNG_1X1
        assert json.loads(archive.read("illustrations.json"))[0]["asset_member"] == member
    assert manifest["members"][member] == sha256_bytes(PNG_1X1)


def test_package_rejects_missing_or_tampered_illustration(valid_pack, tmp_path):
    pack, report, media, member = approved_with_illustration(valid_pack, tmp_path)
    (media / member).unlink()
    with pytest.raises(ValueError, match="Missing or unsafe illustration"):
        package_content(pack, report, tmp_path / "missing.dlpack", media_root=media)
    (media / member).write_bytes(PNG_1X1 + b"tampered")
    with pytest.raises(ValueError, match="byte_size mismatch"):
        package_content(pack, report, tmp_path / "tampered.dlpack", media_root=media)


def test_offline_verify_keeps_legacy_factual_scope_compatibility(approved, tmp_path):
    pack, report = approved
    current = tmp_path / "current-factual.dlpack"
    package_content(pack, report, current, ReviewGateConfig())

    with zipfile.ZipFile(current) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(payloads["manifest.json"])
    review_manifest = json.loads(payloads["ai_review_manifest.json"])
    for field in ("review_status", "review_scope", "source_correctness_reviewed"):
        manifest.pop(field)
    for field in (
        "status",
        "review_scope",
        "source_correctness_reviewed",
        "generator_model_snapshot",
    ):
        review_manifest.pop(field)
    payloads["ai_review_manifest.json"] = json.dumps(
        review_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest["members"]["ai_review_manifest.json"] = sha256_bytes(
        payloads["ai_review_manifest.json"]
    )
    payloads["manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    legacy = tmp_path / "legacy-factual.dlpack"
    with zipfile.ZipFile(legacy, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    verified = verify_pack(legacy)
    assert "review_scope" not in verified
