from __future__ import annotations

import pytest

from deeplock_content.ai_review.reviewer import _module_batch, _validate_review_response
from deeplock_content.util import PROMPT_ROOT


def test_reviewer_v2_separates_module_structure_from_lesson_quality() -> None:
    prompt = (PROMPT_ROOT / "reviewer-v2" / "system.txt").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "MODULE_STRUCTURE_AND_PROJECTED_MINDMAP_ONLY_V3" in normalized
    assert "expected to contain several atom objectives" in normalized
    assert "Stable builder IDs" in normalized
    assert "Never apply lesson-level MIXED_OBJECTIVES" in normalized
    assert "Use REPAIR for a MODULE only" in normalized


def test_module_lesson_issue_is_retried_instead_of_sent_to_mindmap_repair(
    valid_pack,
) -> None:
    module = valid_pack.modules[0]
    batch_id, _payload, expected = _module_batch(valid_pack, module)
    item = expected[module.module_id]
    result = {
        "item_type": "MODULE",
        "item_id": module.module_id,
        "item_revision": item["item_revision"],
        "candidate_hash": item["candidate_hash"],
        "source_hash": item["source_hash"],
        "verdict": "REPAIR",
        "issue_codes": ["MIXED_OBJECTIVES"],
        "evidence_ref_ids": item["owned_evidence_ref_ids"][:1],
        "independent_correct_option_id": None,
        "evidence_sufficient": True,
        "short_rationale": "Sai phạm vi: module tự nhiên chứa nhiều atom.",
    }

    with pytest.raises(PermissionError, match="may request repair only"):
        _validate_review_response(
            batch_id=batch_id,
            data={"reviews": [result]},
            expected=expected,
        )
