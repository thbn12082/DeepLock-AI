from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from deeplock_content.ai_review import run_ai_review
from deeplock_content.ai_review import reviewer as reviewer_module
from deeplock_content.ai_review.reviewer import (
    _module_batch,
    _pair_batch,
    _repair_mindmap_call,
    _repair_pair_call,
    _validate_review_response,
    expected_review_items,
)
from deeplock_content.cache import BuildCache
from deeplock_content.generation import ModelResponse
from deeplock_content.models import MindMapEdge, MindMapNode
from deeplock_content.settings import Settings


class FakeReviewer:
    def __init__(self, disagree: bool = False):
        self.disagree = disagree
        self.calls = 0

    def structured(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["input_text"])
        reviews = []
        source_ids = [item["source_ref_id"] for item in payload["source_context"]["source_refs"]]
        for item in payload["candidate_items"]:
            answer = None
            if item["item_type"] == "QUESTION":
                answer = "A" if self.disagree else next(
                    option["id"]
                    for option in item["candidate_content"]["options"]
                    if option["misconception_tag"] is None
                )
            reviews.append({
                "item_type": item["item_type"], "item_id": item["item_id"],
                "item_revision": item["item_revision"], "candidate_hash": item["candidate_hash"],
                "source_hash": item["source_hash"], "verdict": "APPROVE", "issue_codes": [],
                "evidence_ref_ids": source_ids[:1], "independent_correct_option_id": answer,
                "evidence_sufficient": True, "short_rationale": "Nguồn hỗ trợ nội dung.",
            })
        return ModelResponse({"reviews": reviews}, f"resp_{self.calls}", "cx/gpt-5.5-review")


class RepairThenApproveReviewer(FakeReviewer):
    def __init__(self):
        super().__init__()
        self.repaired = False
        self.requested_repair = False

    def structured(self, **kwargs):
        if kwargs["schema_name"] == "repaired_atom_content":
            self.calls += 1
            payload = json.loads(kwargs["input_text"])
            candidate = payload["candidate"]
            candidate["lesson"]["lesson_id"] = "lesson_repair_drift"
            candidate["lesson"]["pair_id"] = "pair_repair_drift"
            candidate["lesson"]["atom_id"] = "atom_repair_drift"
            candidate["lesson"]["prerequisite_ids"] = ["atom_repair_drift"]
            candidate["lesson"]["source_ref_ids"] = ["src_repair_drift"]
            candidate["lesson"]["illustration_source_ref_ids"] = ["src_repair_drift"]
            for index, question in enumerate(candidate["quiz_bundle"]["questions"]):
                question["question_id"] = f"q_repair_drift_{index}"
                question["pair_id"] = "pair_repair_drift"
                question["atom_id"] = "atom_repair_drift"
            candidate["lesson"]["intuition"] = "Bản sửa vẫn đi ngược gradient để giảm loss."
            candidate["mini_lab"] = None
            self.repaired = True
            return ModelResponse(candidate, f"resp_{self.calls}", "cx/gpt-5.5")
        self.calls += 1
        payload = json.loads(kwargs["input_text"])
        reviews = []
        source_ids = [item["source_ref_id"] for item in payload["source_context"]["source_refs"]]
        for item in payload["candidate_items"]:
            verdict = "APPROVE"
            issues = []
            if item["item_type"] == "LESSON" and not self.requested_repair:
                verdict = "REPAIR"; issues = ["CLARITY"]
                self.requested_repair = True
            answer = None
            if item["item_type"] == "QUESTION":
                answer = next(
                    option["id"]
                    for option in item["candidate_content"]["options"]
                    if option["misconception_tag"] is None
                )
            reviews.append({
                "item_type": item["item_type"], "item_id": item["item_id"],
                "item_revision": item["item_revision"], "candidate_hash": item["candidate_hash"],
                "source_hash": item["source_hash"], "verdict": verdict, "issue_codes": issues,
                "evidence_ref_ids": source_ids[:1],
                "independent_correct_option_id": answer,
                "evidence_sufficient": True, "short_rationale": "Kiểm tra độc lập.",
            })
        return ModelResponse({"reviews": reviews}, f"resp_{self.calls}", "cx/gpt-5.5-review")


class CombinedRepairThenApproveReviewer:
    """Exercise one large first review followed by one pair-sized re-review."""

    def __init__(self):
        self.calls = 0
        self.repair_calls = 0
        self.review_batches: list[list[str]] = []
        self.target_pair_id: str | None = None

    @staticmethod
    def _owned_source_ids(item: dict[str, object]) -> list[str]:
        item_type = item["item_type"]
        content = item["candidate_content"]
        assert isinstance(content, dict)
        if item_type == "LEARNING_PAIR":
            return list(content["bundle_owned_source_ref_ids"])
        if item_type == "MODULE":
            module = content["module"]
            assert isinstance(module, dict)
            return [
                source_ref_id
                for plan in module["atom_plans"]
                for source_ref_id in plan["source_ref_ids"]
            ]
        return list(content["source_ref_ids"])

    def structured(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["input_text"])
        if kwargs["schema_name"] == "repaired_atom_content":
            self.repair_calls += 1
            candidate = payload["candidate"]
            candidate["lesson"]["intuition"] = (
                "Báº£n sá»­a giáº£i thÃ­ch rÃµ cÃ¡ch Ä‘i ngÆ°á»£c gradient Ä‘á»ƒ giáº£m loss."
            )
            return ModelResponse(candidate, f"resp_{self.calls}", "cx/gpt-5.5")

        items = payload["candidate_items"]
        self.review_batches.append([item["item_id"] for item in items])
        review_number = len(self.review_batches)
        reviews = []
        repair_item_id = None
        if review_number == 1:
            repair_item = next(item for item in items if item["item_type"] == "LESSON")
            repair_item_id = repair_item["item_id"]
            self.target_pair_id = repair_item["candidate_content"]["pair_id"]

        for item in items:
            verdict = "REPAIR" if item["item_id"] == repair_item_id else "APPROVE"
            issue_codes = ["CLARITY"] if verdict == "REPAIR" else []
            answer = None
            if item["item_type"] == "QUESTION":
                answer = next(
                    option["id"]
                    for option in item["candidate_content"]["options"]
                    if option["misconception_tag"] is None
                )
            reviews.append({
                "item_type": item["item_type"],
                "item_id": item["item_id"],
                "item_revision": item["item_revision"],
                "candidate_hash": item["candidate_hash"],
                "source_hash": item["source_hash"],
                "verdict": verdict,
                "issue_codes": issue_codes,
                "evidence_ref_ids": self._owned_source_ids(item)[:1],
                "independent_correct_option_id": answer,
                "evidence_sufficient": True,
                "short_rationale": f"review-{review_number}",
            })
        return ModelResponse(
            {"reviews": reviews},
            f"resp_{self.calls}",
            "cx/gpt-5.5-review",
        )


class StaleMetadataOnceReviewer(FakeReviewer):
    def __init__(self):
        super().__init__()
        self.returned_stale_metadata = False

    def structured(self, **kwargs):
        response = super().structured(**kwargs)
        if kwargs["schema_name"] == "content_review_result" and not self.returned_stale_metadata:
            response.data["reviews"][0]["item_revision"] += 1
            self.returned_stale_metadata = True
        return response


class UnownedEvidenceOnceReviewer(FakeReviewer):
    def __init__(self):
        super().__init__()
        self.returned_unowned_evidence = False
        self.saw_retry_feedback = False

    def structured(self, **kwargs):
        if self.returned_unowned_evidence and "retry_feedback" in json.loads(kwargs["input_text"]):
            self.saw_retry_feedback = True
        response = super().structured(**kwargs)
        if kwargs["schema_name"] == "content_review_result" and not self.returned_unowned_evidence:
            response.data["reviews"][0]["evidence_ref_ids"] = ["src_not_owned"]
            self.returned_unowned_evidence = True
        return response


class PairMissingOwnEvidenceOnceReviewer(FakeReviewer):
    def __init__(self):
        super().__init__()
        self.returned_bad_pair_verdict = False
        self.saw_bundle_contract = False

    def structured(self, **kwargs):
        payload = json.loads(kwargs["input_text"])
        if kwargs["schema_name"] == "content_review_result":
            pair_items = [item for item in payload["candidate_items"] if item["item_type"] == "LEARNING_PAIR"]
            if pair_items:
                content = pair_items[0]["candidate_content"]
                self.saw_bundle_contract = bool(content.get("bundle_owned_source_ref_ids"))
        response = super().structured(**kwargs)
        if kwargs["schema_name"] == "content_review_result" and not self.returned_bad_pair_verdict:
            pair_result = next(
                (item for item in response.data["reviews"] if item["item_type"] == "LEARNING_PAIR"),
                None,
            )
            if pair_result is not None:
                pair_result.update({
                    "verdict": "REPAIR",
                    "issue_codes": ["PAIR_MISSING_OWN_SOURCE_REF_IDS"],
                    "evidence_ref_ids": [],
                    "evidence_sufficient": False,
                    "short_rationale": "Pair has no direct source_ref_ids field.",
                })
                self.returned_bad_pair_verdict = True
        return response


class TruncatedMindMapRepairer:
    def __init__(self):
        self.calls = 0

    def structured(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["input_text"])
        candidate = payload["candidate"]
        candidate["nodes"] = candidate["nodes"][:2]
        retained_ids = {node["id"] for node in candidate["nodes"]}
        candidate["edges"] = [
            edge
            for edge in candidate["edges"]
            if edge["from"] in retained_ids and edge["to"] in retained_ids
        ]
        return ModelResponse(candidate, f"resp_{self.calls}", "cx/gpt-5.5")


class StructurallyDriftingPairRepairer:
    def __init__(self):
        self.calls = 0

    def structured(self, **kwargs):
        self.calls += 1
        candidate = json.loads(kwargs["input_text"])["candidate"]
        candidate["lesson"].update({
            "lesson_id": "lesson_repair_drift",
            "pair_id": "pair_repair_drift",
            "atom_id": "atom_repair_drift",
            "grounding_type": "ENRICHMENT",
            "prerequisite_ids": ["atom_repair_drift"],
            "source_ref_ids": ["src_repair_drift"],
            "illustration_source_ref_ids": ["src_repair_drift"],
            "learning_objective": "Hiểu.",
            "intuition": "Quá ngắn.",
        })
        for index, question in enumerate(candidate["quiz_bundle"]["questions"]):
            question.update({
                "question_id": f"q_repair_drift_{index}",
                "pair_id": "pair_repair_drift",
                "atom_id": "atom_repair_drift",
                "grounding_type": "ENRICHMENT",
                "source_ref_ids": ["src_repair_drift"],
            })
        return ModelResponse(candidate, f"resp_{self.calls}", "cx/gpt-5.5")


def settings():
    return Settings(
        api_key=None, base_url="http://127.0.0.1:20128/v1", generator_model="gpt-5.5",
        review_model="gpt-5.5", model_prefix="cx/", concurrency=5,
        generator_reasoning_effort="medium", review_reasoning_effort="high",
        generator_prompt_version="course-v1", review_prompt_version="reviewer-v1",
        max_repair_rounds=2, request_timeout_seconds=30, max_api_attempts=1,
    )


def test_fresh_reviewer_covers_pair_and_module_and_approves(valid_pack, tmp_path):
    fake = FakeReviewer()
    updated, report = run_ai_review(
        pack=valid_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
        prompt_version="reviewer-v1", max_repair_rounds=2, resume=False, adapter=fake,
    )
    assert fake.calls == 4
    assert report["status"] == "AI_APPROVED"
    assert updated.learning_pairs[0].ai_review_status == "AI_APPROVED"


def test_module_review_uses_only_target_mindmap_projection(valid_pack):
    pack = valid_pack.model_copy(deep=True)
    pack.mindmaps[0].nodes.append(MindMapNode(
        id="module_unrelated", label="Unrelated", type="MODULE", atom_id=None,
    ))
    pack.mindmaps[0].edges.append(MindMapEdge(**{
        "from": pack.course.course_id,
        "to": "module_unrelated",
        "type": "CONTAINS",
    }))

    _, payload, _ = _module_batch(pack, pack.modules[0])
    candidate = payload["candidate_items"][0]["candidate_content"]
    projection = candidate["mindmap_projection"][0]

    assert candidate["review_scope"] == "MODULE_STRUCTURE_AND_PROJECTED_MINDMAP_ONLY_V3"
    assert "module_unrelated" not in {node["id"] for node in projection["nodes"]}
    assert all(edge["to"] != "module_unrelated" for edge in projection["contains_edges"])
    assert "intentionally omitted" in payload["task"]


def test_truncated_mindmap_repair_is_rejected_before_ready_cache(valid_pack, tmp_path):
    fake = TruncatedMindMapRepairer()
    cache = BuildCache(tmp_path / "cache.sqlite3")
    try:
        with pytest.raises(ValueError, match="preserve every .* mind map node"):
            _repair_mindmap_call(
                pack=valid_pack,
                issues=[{
                    "item_type": "MODULE",
                    "item_id": valid_pack.modules[0].module_id,
                    "issue_codes": ["MINDMAP_SCOPE"],
                    "evidence_ref_ids": [],
                }],
                adapter=fake,
                settings=settings(),
                model="gpt-5.5",
                cache=cache,
                resume=False,
            )

        row = cache.connection.execute(
            "SELECT step_type, status FROM generation_steps WHERE step_type=?",
            ("REPAIR_MIND_MAP",),
        ).fetchone()
        assert fake.calls == 1
        assert row is not None
        assert (row["step_type"], row["status"]) == ("REPAIR_MIND_MAP", "FAILED")
    finally:
        cache.close()


def test_course_v2_pair_repair_restores_structure_before_quality_gates(
    valid_pack,
    tmp_path,
    monkeypatch,
):
    pack = valid_pack.model_copy(deep=True)
    pack.generator_prompt_version = "course-v2"
    pair = pack.learning_pairs[0]
    lesson = next(item for item in pack.lessons if item.lesson_id == pair.micro_lesson_id)
    lesson.learning_objective = (
        "Giải thích vai trò của gradient trong cập nhật tham số mô hình."
    )
    questions = [
        next(item for item in pack.questions if item.question_id == question_id)
        for question_id in pair.question_ids
    ]
    lesson_gate_calls = 0
    quiz_gate_calls = 0

    def assert_normalized_lesson(value, *, pedagogical=False, **_kwargs):
        nonlocal lesson_gate_calls
        lesson_gate_calls += 1
        assert pedagogical
        assert (
            value.lesson_id,
            value.pair_id,
            value.atom_id,
            value.grounding_type,
            value.prerequisite_ids,
            value.source_ref_ids,
            value.illustration_source_ref_ids,
        ) == (
            lesson.lesson_id,
            lesson.pair_id,
            lesson.atom_id,
            lesson.grounding_type,
            lesson.prerequisite_ids,
            lesson.source_ref_ids,
            lesson.illustration_source_ref_ids,
        )
        issues = []
        if value.learning_objective == "Hiểu.":
            issues.append(SimpleNamespace(
                path="lesson.learning_objective",
                code="LESSON_OBJECTIVE_NOT_OBSERVABLE",
            ))
        if value.intuition == "Quá ngắn.":
            issues.append(SimpleNamespace(
                path="lesson.intuition",
                code="LESSON_INTUITION_TOO_THIN",
            ))
        return issues

    def assert_normalized_questions(values):
        nonlocal quiz_gate_calls
        quiz_gate_calls += 1
        assert [item.question_id for item in values] == [
            item.question_id for item in questions
        ]
        assert all(
            repaired.pair_id == original.pair_id
            and repaired.atom_id == original.atom_id
            and repaired.grounding_type == original.grounding_type
            and repaired.source_ref_ids == original.source_ref_ids
            for repaired, original in zip(values, questions, strict=True)
        )

    monkeypatch.setattr(
        reviewer_module,
        "validate_lesson_editorial",
        assert_normalized_lesson,
    )
    monkeypatch.setattr(
        reviewer_module,
        "validate_quiz_editorial",
        assert_normalized_questions,
    )
    fake = StructurallyDriftingPairRepairer()
    cache = BuildCache(tmp_path / "cache.sqlite3")
    try:
        repaired_pair_id, repaired, _ = _repair_pair_call(
            pack=pack,
            pair_id=pair.pair_id,
            issues=[{"issue_codes": ["CLARITY"]}],
            adapter=fake,
            settings=replace(settings(), generator_prompt_version="course-v2"),
            model="gpt-5.5",
            cache=cache,
            resume=False,
        )
    finally:
        cache.close()

    assert fake.calls == 1
    assert repaired_pair_id == pair.pair_id
    assert repaired.lesson.learning_objective == lesson.learning_objective
    assert repaired.lesson.intuition == lesson.intuition
    assert lesson_gate_calls >= 3
    assert quiz_gate_calls == 2


def test_answer_disagreement_fails_closed(valid_pack, tmp_path):
    with pytest.raises(PermissionError, match="ANSWER_DISAGREEMENT"):
        run_ai_review(
            pack=valid_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
            prompt_version="reviewer-v1", max_repair_rounds=2, resume=False,
            adapter=FakeReviewer(disagree=True),
        )


def test_stale_reviewer_metadata_is_retried_before_cache(valid_pack, tmp_path):
    fake = StaleMetadataOnceReviewer()
    updated, report = run_ai_review(
        pack=valid_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
        prompt_version="reviewer-v1", max_repair_rounds=2, resume=True, adapter=fake,
    )

    assert fake.returned_stale_metadata
    assert fake.calls == 5
    assert report["status"] == "AI_APPROVED"
    assert all(pair.ai_review_status == "AI_APPROVED" for pair in updated.learning_pairs)


def test_near_copy_well_formed_candidate_hash_is_still_rejected(valid_pack):
    batch_id, payload, expected = _pair_batch(
        valid_pack,
        valid_pack.learning_pairs[0],
    )
    response = FakeReviewer().structured(
        schema_name="content_review_result",
        input_text=json.dumps(payload),
    )
    result = response.data["reviews"][0]
    expected_digest = result["candidate_hash"].removeprefix("sha256:")
    # Model-like echo drift: two digest characters disappear in the middle and
    # two valid hex characters are appended, so the wrong value still passes
    # the public sha256 syntax/length pattern. It must never be rebound.
    wrong_digest = expected_digest[:24] + expected_digest[26:] + "f8"
    result["candidate_hash"] = "sha256:" + wrong_digest

    assert len(wrong_digest) == 64
    assert all(character in "0123456789abcdef" for character in wrong_digest)
    assert result["candidate_hash"] != expected[result["item_id"]]["candidate_hash"]
    with pytest.raises(PermissionError, match="stale/tampered metadata"):
        _validate_review_response(
            batch_id=batch_id,
            data=response.data,
            expected=expected,
        )


def test_unowned_approval_evidence_is_retried_before_cache(valid_pack, tmp_path):
    fake = UnownedEvidenceOnceReviewer()
    updated, report = run_ai_review(
        pack=valid_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
        prompt_version="reviewer-v1", max_repair_rounds=2, resume=True, adapter=fake,
    )

    assert fake.returned_unowned_evidence
    assert fake.saw_retry_feedback
    assert fake.calls == 5
    assert report["status"] == "AI_APPROVED"
    assert all(pair.ai_review_status == "AI_APPROVED" for pair in updated.learning_pairs)


def test_pair_bundle_source_ownership_misread_is_retried(valid_pack, tmp_path):
    fake = PairMissingOwnEvidenceOnceReviewer()
    updated, report = run_ai_review(
        pack=valid_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
        prompt_version="reviewer-v1", max_repair_rounds=2, resume=True, adapter=fake,
    )

    assert fake.returned_bad_pair_verdict
    assert fake.saw_bundle_contract
    assert fake.calls == 5
    assert report["status"] == "AI_APPROVED"
    assert all(pair.ai_review_status == "AI_APPROVED" for pair in updated.learning_pairs)


def test_repair_is_applied_then_reviewed_fresh(curriculum_pack, curriculum_contract, tmp_path):
    fake = RepairThenApproveReviewer()
    updated, report = run_ai_review(
        pack=curriculum_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
        prompt_version="reviewer-v1", max_repair_rounds=2, resume=False, adapter=fake,
        contract=curriculum_contract, require_contract=True, require_frozen_outline=True,
    )
    assert fake.repaired
    assert sorted(pair.revision for pair in updated.learning_pairs) == [1, 1, 2]
    assert all(pair.ai_review_status == "AI_APPROVED" for pair in updated.learning_pairs)
    assert report["repaired_count"] == 1

    repaired_pair = next(pair for pair in updated.learning_pairs if pair.revision == 2)
    original_pair = next(pair for pair in curriculum_pack.learning_pairs if pair.pair_id == repaired_pair.pair_id)
    repaired_lesson = next(item for item in updated.lessons if item.lesson_id == repaired_pair.micro_lesson_id)
    original_lesson = next(item for item in curriculum_pack.lessons if item.lesson_id == original_pair.micro_lesson_id)
    assert repaired_lesson.intuition != original_lesson.intuition
    assert (
        repaired_lesson.lesson_id,
        repaired_lesson.pair_id,
        repaired_lesson.atom_id,
        repaired_lesson.prerequisite_ids,
        repaired_lesson.source_ref_ids,
        repaired_lesson.illustration_source_ref_ids,
    ) == (
        original_lesson.lesson_id,
        original_lesson.pair_id,
        original_lesson.atom_id,
        original_lesson.prerequisite_ids,
        original_lesson.source_ref_ids,
        original_lesson.illustration_source_ref_ids,
    )
    repaired_questions = {
        item.question_id: item for item in updated.questions if item.pair_id == repaired_pair.pair_id
    }
    assert list(repaired_questions) == original_pair.question_ids
    assert all(
        repaired_questions[question_id].pair_id == repaired_pair.pair_id
        and repaired_questions[question_id].atom_id == repaired_pair.atom_id
        for question_id in original_pair.question_ids
    )
    assert report["pack_contract_sha256"]

    replay = FakeReviewer()
    replayed, replay_report = run_ai_review(
        pack=curriculum_pack, workdir=tmp_path, settings=settings(), model="gpt-5.5",
        prompt_version="reviewer-v1", max_repair_rounds=2, resume=True, adapter=replay,
        contract=curriculum_contract, require_contract=True, require_frozen_outline=True,
    )
    assert replay.calls == 0
    assert replay_report["status"] == "AI_APPROVED"
    assert [pair.revision for pair in replayed.learning_pairs] == [pair.revision for pair in updated.learning_pairs]


def test_post_repair_reuses_unchanged_approvals_and_reviews_only_changed_pair(
    curriculum_pack,
    curriculum_contract,
    tmp_path,
):
    fake = CombinedRepairThenApproveReviewer()
    updated, report = run_ai_review(
        pack=curriculum_pack,
        workdir=tmp_path,
        settings=replace(settings(), review_batch_size=5),
        model="gpt-5.5",
        prompt_version="reviewer-v1",
        max_repair_rounds=2,
        resume=False,
        adapter=fake,
        contract=curriculum_contract,
        require_contract=True,
        require_frozen_outline=True,
    )

    assert fake.repair_calls == 1
    assert len(fake.review_batches) == 2
    assert len(fake.review_batches[0]) == len(expected_review_items(curriculum_pack))
    assert fake.target_pair_id is not None
    repaired_pair = next(
        pair for pair in updated.learning_pairs if pair.pair_id == fake.target_pair_id
    )
    _, _, changed_pair_items = _pair_batch(updated, repaired_pair)
    changed_ids = set(changed_pair_items)
    assert set(fake.review_batches[1]) == changed_ids
    assert len(fake.review_batches[1]) < len(fake.review_batches[0])

    expected_final = expected_review_items(updated)
    reported = {item["item_id"]: item for item in report["results"]}
    assert set(reported) == set(expected_final)
    assert report["approved_items"] == len(expected_final)
    assert report["repaired_count"] == 1
    for item_id, expected in expected_final.items():
        result = reported[item_id]
        assert (
            result["item_type"],
            result["item_revision"],
            result["candidate_hash"],
            result["source_hash"],
        ) == (
            expected["item_type"],
            expected["item_revision"],
            expected["candidate_hash"],
            expected["source_hash"],
        )
        assert result["short_rationale"] == (
            "review-2" if item_id in changed_ids else "review-1"
        )
