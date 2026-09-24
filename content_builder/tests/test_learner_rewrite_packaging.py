from __future__ import annotations

import json
import zipfile

import pytest

from deeplock_content.generation.editorial_rewrite import learner_rewrite_module_hash
from deeplock_content.packaging import (
    EDITORIAL_REWRITE_APPROVED,
    LEARNER_REWRITE_REVIEW_SCOPE,
    LearnerRewriteGateConfig,
    package_content,
    verify_learner_rewrite_gate,
    verify_pack,
)
from deeplock_content.util import sha256_json


@pytest.fixture()
def rewrite_candidate(valid_pack):
    pack = valid_pack.model_copy(deep=True)
    for pair in pack.learning_pairs:
        pair.ai_review_status = "AI_APPROVED"
    for lab in pack.mini_labs:
        lab.ai_review_status = "AI_APPROVED"
    return pack


@pytest.fixture()
def rewrite_config():
    return LearnerRewriteGateConfig(
        editorial_model="gpt-5.5",
        editorial_configured_model="cx/gpt-5.5",
        editorial_model_snapshot="cx/gpt-5.5",
        editorial_prompt_version="learner-rewrite-v1",
        editorial_prompt_sha256="sha256:" + "1" * 64,
        editorial_schema_version="1",
        editorial_schema_sha256="sha256:" + "2" * 64,
        editorial_reasoning_effort="high",
    )


@pytest.fixture()
def rewrite_report(rewrite_candidate, rewrite_config):
    results = [
        {
            "module_id": module.module_id,
            "verdict": "APPROVE",
            "output_hash": learner_rewrite_module_hash(
                rewrite_candidate,
                module.module_id,
            ),
        }
        for module in rewrite_candidate.modules
    ]
    return {
        "status": EDITORIAL_REWRITE_APPROVED,
        "review_scope": LEARNER_REWRITE_REVIEW_SCOPE,
        "source_correctness_reviewed": False,
        "candidate_hash": sha256_json(
            rewrite_candidate.model_dump(mode="json", by_alias=True)
        ),
        "source_hash": rewrite_candidate.source_hash,
        "editorial_model": rewrite_config.editorial_model,
        "editorial_configured_model": rewrite_config.editorial_configured_model,
        "editorial_model_snapshots": [rewrite_config.editorial_model_snapshot],
        "editorial_prompt_version": rewrite_config.editorial_prompt_version,
        "editorial_prompt_sha256": rewrite_config.editorial_prompt_sha256,
        "editorial_schema_version": rewrite_config.editorial_schema_version,
        "editorial_schema_sha256": rewrite_config.editorial_schema_sha256,
        "editorial_reasoning_effort": rewrite_config.editorial_reasoning_effort,
        "store_responses": False,
        "approved_items": len(results),
        "approved_modules": len(results),
        "repaired_count": 0,
        "rejected_count": 0,
        "results_sha256": sha256_json(results),
        "results": results,
    }


def _rehash(report):
    report["results_sha256"] = sha256_json(report["results"])


def test_valid_learner_rewrite_gate_packages_with_honest_scope(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
    tmp_path,
):
    verify_learner_rewrite_gate(
        rewrite_candidate,
        rewrite_report,
        rewrite_config,
    )
    output = tmp_path / "learner-rewrite.dlpack"
    package_content(
        rewrite_candidate,
        rewrite_report,
        output,
        rewrite_config,
        review_scope=LEARNER_REWRITE_REVIEW_SCOPE,
    )

    manifest = verify_pack(output)
    assert manifest["review_status"] == EDITORIAL_REWRITE_APPROVED
    assert manifest["review_scope"] == LEARNER_REWRITE_REVIEW_SCOPE
    assert manifest["source_correctness_reviewed"] is False
    assert manifest["editorial_prompt_sha256"] == rewrite_config.editorial_prompt_sha256
    with zipfile.ZipFile(output) as archive:
        packaged_report = json.loads(archive.read("ai_review_manifest.json"))
    assert packaged_report["status"] == EDITORIAL_REWRITE_APPROVED
    assert packaged_report["source_correctness_reviewed"] is False
    assert packaged_report["editorial_model"] == "gpt-5.5"


def test_learner_rewrite_requires_explicit_packaging_scope(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
    tmp_path,
):
    with pytest.raises(PermissionError, match="explicit review_scope"):
        package_content(
            rewrite_candidate,
            rewrite_report,
            tmp_path / "implicit.dlpack",
            rewrite_config,
        )


def test_learner_rewrite_rejects_tampered_module_output_hash(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
):
    rewrite_report["results"][0]["output_hash"] = "sha256:" + "0" * 64
    _rehash(rewrite_report)
    with pytest.raises(PermissionError, match="module output hash mismatch"):
        verify_learner_rewrite_gate(
            rewrite_candidate,
            rewrite_report,
            rewrite_config,
        )


def test_learner_rewrite_requires_exact_module_coverage(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
):
    rewrite_report["results"].append(dict(rewrite_report["results"][0]))
    rewrite_report["approved_items"] += 1
    _rehash(rewrite_report)
    with pytest.raises(PermissionError, match="module coverage mismatch"):
        verify_learner_rewrite_gate(
            rewrite_candidate,
            rewrite_report,
            rewrite_config,
        )


def test_learner_rewrite_is_bound_to_exact_candidate(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
):
    rewrite_candidate.lessons[0].hook += " Nội dung bị sửa sau lượt biên tập."
    with pytest.raises(PermissionError, match="stale for current candidate"):
        verify_learner_rewrite_gate(
            rewrite_candidate,
            rewrite_report,
            rewrite_config,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("review_scope", "SOURCE_FACTUAL_REVIEW_V1", "review_scope"),
        ("source_correctness_reviewed", True, "must be false"),
        ("status", "AI_APPROVED", "not EDITORIAL_REWRITE_APPROVED"),
    ],
)
def test_learner_rewrite_rejects_false_scope_or_source_claim(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
    field,
    value,
    message,
):
    rewrite_report[field] = value
    with pytest.raises(PermissionError, match=message):
        verify_learner_rewrite_gate(
            rewrite_candidate,
            rewrite_report,
            rewrite_config,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("editorial_model", "gpt-5.6"),
        ("editorial_configured_model", "cx/stale"),
        ("editorial_model_snapshots", ["cx/stale"]),
        ("editorial_prompt_version", "learner-rewrite-v0"),
        ("editorial_prompt_sha256", "sha256:" + "0" * 64),
        ("editorial_schema_version", "0"),
        ("editorial_schema_sha256", "sha256:" + "0" * 64),
        ("editorial_reasoning_effort", "low"),
        ("store_responses", True),
    ],
)
def test_learner_rewrite_is_bound_to_exact_editor_config(
    rewrite_candidate,
    rewrite_report,
    rewrite_config,
    field,
    value,
):
    rewrite_report[field] = value
    with pytest.raises(PermissionError, match="missing or stale|config mismatch"):
        verify_learner_rewrite_gate(
            rewrite_candidate,
            rewrite_report,
            rewrite_config,
        )
