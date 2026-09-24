from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from deeplock_content import catalog_build
from deeplock_content.contracts import illustration_identities_sha256
from deeplock_content.generation import ContentGenerationStagePaused
from deeplock_content.models import IllustrationAsset
from deeplock_content.settings import MAX_ROUTER_CONCURRENCY, Settings
from deeplock_content.util import sha256_json, write_json


def _settings(concurrency: int) -> Settings:
    return Settings(
        api_key=None,
        base_url="http://127.0.0.1:20128/v1",
        generator_model="gpt-5.5",
        review_model="gpt-5.5",
        model_prefix="cx/",
        concurrency=concurrency,
        generator_reasoning_effort="medium",
        review_reasoning_effort="high",
        generator_prompt_version="course-v1",
        review_prompt_version="reviewer-v1",
        max_repair_rounds=2,
        request_timeout_seconds=30,
        max_api_attempts=1,
    )


def test_catalog_build_spreads_the_request_budget_over_every_lecture(
    tmp_path,
    monkeypatch,
):
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [
            {"lecture_id": f"lecture_{index}", "title": f"Lecture {index}"}
            for index in range(24)
        ],
    })
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    maximum = 0

    def fake_build(*, lecture, settings, **_kwargs):
        nonlocal active, maximum
        # 64 request budget over 24 lectures: all 24 run at once, 2 workers each.
        assert settings.concurrency == MAX_ROUTER_CONCURRENCY // 24
        assert settings.review_batch_size == 5
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 24:
                release.set()
        assert release.wait(timeout=2)
        time.sleep(0.01)
        with lock:
            active -= 1
        return {
            "lecture_id": lecture["lecture_id"],
            "status": "READY",
            "output": "fixture.dlpack",
            "content_pack_id": f"pack_{lecture['lecture_id']}",
            "version": "fixture",
        }

    monkeypatch.setattr(catalog_build, "_pipeline_signature", lambda _settings: "signature")
    monkeypatch.setattr(catalog_build, "_build_one_pack", fake_build)
    report = catalog_build.build_catalog_packs(
        plan_path=plan,
        output_dir=tmp_path / "packs",
        settings=_settings(concurrency=MAX_ROUTER_CONCURRENCY + 50),
        resume=True,
    )
    assert report["ready"] == 24
    assert report["failed"] == 0
    assert maximum == 24


def test_catalog_build_splits_twenty_request_budget_across_four_lectures(
    tmp_path,
    monkeypatch,
):
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [
            {"lecture_id": f"lecture_{index}", "title": f"Lecture {index}"}
            for index in range(4)
        ],
    })
    seen_inner_concurrency: list[int] = []

    def fake_build(*, lecture, settings, **_kwargs):
        seen_inner_concurrency.append(settings.concurrency)
        return {
            "lecture_id": lecture["lecture_id"],
            "status": "READY",
            "output": "fixture.dlpack",
            "content_pack_id": f"pack_{lecture['lecture_id']}",
            "version": "fixture",
        }

    monkeypatch.setattr(catalog_build, "_pipeline_signature", lambda _settings: "signature")
    monkeypatch.setattr(catalog_build, "_build_one_pack", fake_build)
    report = catalog_build.build_catalog_packs(
        plan_path=plan,
        output_dir=tmp_path / "packs",
        settings=_settings(concurrency=20),
        resume=True,
    )

    assert report["ready"] == 4
    assert seen_inner_concurrency == [5, 5, 5, 5]


def test_catalog_resume_provenance_binds_generation_group_limit():
    two_atoms = _settings(concurrency=1)
    three_atoms = replace(two_atoms, max_atoms_per_generation_call=3)

    assert catalog_build._pipeline_signature(two_atoms) != catalog_build._pipeline_signature(
        three_atoms
    )
    provenance = catalog_build._expected_provenance(two_atoms, "fixture-signature")
    assert provenance["max_atoms_per_generation_call"] == 2


def test_catalog_provenance_records_explicit_learner_rewrite_without_claiming_factual_review():
    settings = replace(
        _settings(concurrency=1),
        editorial_rewrite_enabled=True,
        editorial_model="gpt-5.5",
        editorial_reasoning_effort="high",
        editorial_prompt_version="learner-rewrite-v1",
    )

    provenance = catalog_build._expected_provenance(settings, "fixture-signature")

    assert provenance["editorial_rewrite_enabled"] is True
    assert provenance["review_scope"] == "LEARNER_REWRITE_ONLY_V1"
    assert provenance["source_correctness_reviewed"] is False
    assert provenance["editorial_model"] == "gpt-5.5"
    assert provenance["editorial_transport_model"] == "cx/gpt-5.5"
    assert provenance["max_atoms_per_editorial_call"] == 2
    assert provenance["editorial_prompt_version"] == "learner-rewrite-v1"
    assert provenance["editorial_schema_version"] == "1"
    assert provenance["editorial_prompt_sha256"].startswith("sha256:")
    assert provenance["editorial_schema_sha256"].startswith("sha256:")
    assert "review_prompt_version" not in provenance


def test_catalog_workspace_provenance_invalidates_when_editorial_feedback_changes(
    tmp_path,
):
    settings = replace(
        _settings(concurrency=1),
        editorial_rewrite_enabled=True,
        editorial_model="gpt-5.5",
        editorial_prompt_version="learner-rewrite-v1",
    )

    missing = catalog_build._expected_workspace_provenance(
        settings,
        "fixture-signature",
        tmp_path,
    )
    assert missing["editorial_quality_feedback_file_sha256"] is None

    feedback_path = (
        tmp_path / catalog_build.EDITORIAL_QUALITY_FEEDBACK_FILENAME
    )
    write_json(feedback_path, {"schema_version": "1", "atoms": ["atom_a"]})
    first = catalog_build._expected_workspace_provenance(
        settings,
        "fixture-signature",
        tmp_path,
    )
    write_json(feedback_path, {"schema_version": "1", "atoms": ["atom_b"]})
    second = catalog_build._expected_workspace_provenance(
        settings,
        "fixture-signature",
        tmp_path,
    )

    assert first["editorial_quality_feedback_file_sha256"].startswith("sha256:")
    assert first != second


def test_catalog_resume_signature_binds_editorial_group_size():
    two_atoms = replace(
        _settings(concurrency=1),
        editorial_rewrite_enabled=True,
        editorial_prompt_version="learner-rewrite-v1",
        max_atoms_per_editorial_call=2,
    )
    one_atom = replace(two_atoms, max_atoms_per_editorial_call=1)

    assert catalog_build._pipeline_signature(two_atoms) != catalog_build._pipeline_signature(
        one_atom
    )


def test_frozen_editorial_input_is_reused_immutably_with_only_current_curriculum_metadata(
    curriculum_pack,
    curriculum_contract,
    tmp_path,
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    frozen = curriculum_pack.model_copy(deep=True)
    source_ref = frozen.source_refs[0]
    frozen.illustrations = [IllustrationAsset(
        illustration_id="illustration_fixture",
        source_ref_id=source_ref.source_ref_id,
        asset_member="fixture.png",
        mime_type="image/png",
        sha256="sha256:" + "a" * 64,
        byte_size=128,
        width=32,
        height=24,
        page_number=source_ref.page_start,
        caption="Caption pass 1",
        alt_text="Alt pass 1",
    )]
    snapshot_path = workspace / catalog_build.EDITORIAL_INPUT_SNAPSHOT_FILENAME
    write_json(snapshot_path, frozen.model_dump(mode="json", by_alias=True))
    snapshot_bytes = snapshot_path.read_bytes()

    generated = frozen.model_copy(deep=True)
    generated.course.title = "Tiêu đề curriculum hiện tại"
    generated.course.description = "Mô tả curriculum hiện tại"
    generated.illustrations[0].caption = "Caption curriculum hiện tại"
    generated.illustrations[0].alt_text = "Alt curriculum hiện tại"

    # Simulate a retry that regenerated learner-owned content differently.
    regenerated_question = generated.questions[0]
    regenerated_question.difficulty = 5
    regenerated_question.bloom_level = "ANALYZE"
    regenerated_question.source_ref_ids = [generated.source_refs[1].source_ref_id]
    regenerated_question.stem = "Nội dung bị sinh lại không được lọt vào pass 2"
    generated.lessons[0].hook = "Hook bị sinh lại không được lọt vào pass 2"

    current_contract = curriculum_contract.model_copy(update={
        "course_sha256": sha256_json(generated.course.model_dump(mode="json")),
        "expected_counts": curriculum_contract.expected_counts.model_copy(
            update={"illustrations": 1}
        ),
        "illustration_identities_sha256": illustration_identities_sha256(generated),
        "outline_assignment_sha256": None,
    })

    restored, frozen_contract = catalog_build._restore_frozen_editorial_input(
        workspace=workspace,
        generated=generated,
        contract=current_contract,
    )

    assert restored.course == generated.course
    assert restored.illustrations == generated.illustrations
    assert restored.lessons[0].hook == frozen.lessons[0].hook
    assert restored.questions[0].stem == frozen.questions[0].stem
    assert restored.questions[0].difficulty == frozen.questions[0].difficulty
    assert restored.questions[0].bloom_level == frozen.questions[0].bloom_level
    assert restored.questions[0].source_ref_ids == frozen.questions[0].source_ref_ids
    assert restored.questions[0].model_dump() != regenerated_question.model_dump()
    assert frozen_contract.outline_assignment_sha256 is not None
    assert snapshot_path.read_bytes() == snapshot_bytes


def test_frozen_editorial_input_fails_closed_without_replacing_stale_graph(
    curriculum_pack,
    curriculum_contract,
    tmp_path,
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    stale = curriculum_pack.model_copy(deep=True)
    stale.atoms = list(reversed(stale.atoms))
    snapshot_path = workspace / catalog_build.EDITORIAL_INPUT_SNAPSHOT_FILENAME
    write_json(snapshot_path, stale.model_dump(mode="json", by_alias=True))
    snapshot_bytes = snapshot_path.read_bytes()
    current_contract = curriculum_contract.model_copy(
        update={"outline_assignment_sha256": None}
    )

    with pytest.raises(ValueError, match="identity/source/order/count"):
        catalog_build._restore_frozen_editorial_input(
            workspace=workspace,
            generated=curriculum_pack.model_copy(deep=True),
            contract=current_contract,
        )

    assert snapshot_path.read_bytes() == snapshot_bytes


def test_build_one_pack_uses_editorial_rewrite_instead_of_factual_review(
    curriculum_pack,
    curriculum_contract,
    corpus,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "work" / "lecture_rewrite"
    workspace.mkdir(parents=True)
    (workspace / "media").mkdir()
    write_json(workspace / "source.json", corpus.model_dump(mode="json"))
    write_json(workspace / "curriculum.json", {"modules": []})
    output_dir = tmp_path / "packs"
    settings = replace(
        _settings(concurrency=1),
        editorial_rewrite_enabled=True,
        editorial_prompt_version="learner-rewrite-v1",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        catalog_build,
        "build_pack_contract",
        lambda **_kwargs: curriculum_contract,
    )
    monkeypatch.setattr(
        catalog_build,
        "generate_candidate",
        lambda **_kwargs: curriculum_pack.model_copy(deep=True),
    )
    monkeypatch.setattr(
        catalog_build,
        "freeze_outline_assignment",
        lambda contract, _pack: contract,
    )

    def fake_editor(**kwargs):
        calls["editor"] = kwargs
        updated = kwargs["pack"].model_copy(deep=True)
        for pair in updated.learning_pairs:
            pair.ai_review_status = "AI_APPROVED"
        for lab in updated.mini_labs:
            lab.ai_review_status = "AI_APPROVED"
        return updated, {"status": "EDITORIAL_REWRITE_APPROVED"}

    def factual_review_must_not_run(**_kwargs):
        raise AssertionError("factual review ran in explicit learner rewrite mode")

    def fake_package(pack, report, output, config, **kwargs):
        calls["package"] = {
            "pack": pack,
            "report": report,
            "config": config,
            **kwargs,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fixture-pack")
        return output

    monkeypatch.setattr(catalog_build, "run_editorial_rewrite", fake_editor)
    monkeypatch.setattr(catalog_build, "run_ai_review", factual_review_must_not_run)
    monkeypatch.setattr(catalog_build, "package_content", fake_package)
    monkeypatch.setattr(
        catalog_build,
        "verify_pack",
        lambda _output: {"content_pack_id": "pack_rewrite", "version": "v1"},
    )

    result = catalog_build._build_one_pack(
        lecture={
            "lecture_id": "lecture_rewrite",
            "workspace": str(workspace),
        },
        output_dir=output_dir,
        settings=settings,
        pipeline_signature="fixture-signature",
        resume=False,
    )

    assert result["status"] == "READY"
    editor_call = calls["editor"]
    assert editor_call["contract"] == curriculum_contract
    assert editor_call["require_contract"] is True
    assert editor_call["require_frozen_outline"] is True
    package_call = calls["package"]
    assert package_call["review_scope"] == "LEARNER_REWRITE_ONLY_V1"
    assert isinstance(package_call["config"], catalog_build.LearnerRewriteGateConfig)
    skipped = catalog_build.read_json(workspace / "ai-review.json")
    assert skipped["status"] == "SKIPPED"
    assert skipped["source_correctness_reviewed"] is False
    assert skipped["replacement"] == "editorial-rewrite-report.json"
    assert (
        workspace / catalog_build.EDITORIAL_INPUT_SNAPSHOT_FILENAME
    ).is_file()


def test_queue_refills_while_first_lecture_is_still_running(tmp_path, monkeypatch):
    from threading import Event

    third_started = Event()
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {"lectures": [
        {"lecture_id": str(i), "title": str(i)} for i in range(3)
    ]})
    monkeypatch.setenv("NINEROUTER_LECTURE_WORKERS", "2")
    monkeypatch.setattr(catalog_build, "_pipeline_signature", lambda _: "signature")

    def build(**kwargs):
        assert kwargs["settings"].concurrency == 4
        lecture_id = kwargs["lecture"]["lecture_id"]
        if lecture_id == "0":
            assert third_started.wait(5), "Queue waited for the slow lecture"
        if lecture_id == "2":
            third_started.set()
        return {"lecture_id": lecture_id, "status": "READY"}

    monkeypatch.setattr(catalog_build, "_build_one_pack", build)
    report = catalog_build.build_catalog_packs(
        plan_path=plan, output_dir=tmp_path / "packs",
        settings=_settings(concurrency=8), resume=True,
    )
    assert report["ready"] == 3


def test_elastic_lecture_can_borrow_idle_request_slots(tmp_path, monkeypatch):
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {"lectures": [{"lecture_id": str(i), "title": str(i)} for i in range(2)]})
    monkeypatch.setenv("NINEROUTER_ELASTIC_REQUESTS", "1")
    monkeypatch.setenv("NINEROUTER_LECTURE_WORKERS", "2")
    monkeypatch.setattr(catalog_build, "_pipeline_signature", lambda _: "signature")

    def build(**kwargs):
        from deeplock_content.generation import api
        assert kwargs["settings"].concurrency == 8  # Previously fixed at four.
        assert api._shared_request_slots is not None
        return {"lecture_id": kwargs["lecture"]["lecture_id"], "status": "READY"}

    monkeypatch.setattr(catalog_build, "_build_one_pack", build)
    report = catalog_build.build_catalog_packs(plan_path=plan, output_dir=tmp_path / "packs", settings=_settings(8))
    assert report["ready"] == 2
    from deeplock_content.generation import api
    assert api._shared_request_slots is None


def test_fast_lecture_builds_while_other_vision_is_pending(tmp_path, monkeypatch):
    from threading import Event
    from deeplock_content import vision_audit

    built = Event()
    audited = set()
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {"lectures": [
        {"lecture_id": name, "title": name, "workspace": str(tmp_path / name)}
        for name in ("slow", "fast")
    ]})
    monkeypatch.setenv("NINEROUTER_LECTURE_WORKERS", "2")
    monkeypatch.setattr(catalog_build, "_pipeline_signature", lambda _: "signature")

    def audit(**kwargs):
        entries = catalog_build.read_json(kwargs["plan_path"])["lectures"]
        assert len(entries) == 1
        name = entries[0]["lecture_id"]
        if name == "slow":
            assert built.wait(5), "Slow vision blocked the fast lecture"
        audited.add(name)
        return {"status": "READY"}

    def build(**kwargs):
        name = kwargs["lecture"]["lecture_id"]
        assert name in audited
        assert kwargs["settings"].concurrency == 4
        if name == "fast":
            built.set()
        return {"lecture_id": name, "status": "READY"}

    monkeypatch.setattr(vision_audit, "audit_catalog_visual_pages", audit)
    monkeypatch.setattr(catalog_build, "_build_one_pack", build)
    result = catalog_build.build_catalog_packs(
        plan_path=plan, output_dir=tmp_path / "packs",
        settings=_settings(concurrency=8), vision_per_lecture=True,
    )
    assert result["ready"] == 2


def test_catalog_reports_successful_one_lecture_canary_as_staged(
    tmp_path,
    monkeypatch,
):
    plan = tmp_path / "catalog-plan.json"
    write_json(plan, {
        "lectures": [{"lecture_id": "lecture_canary", "title": "Canary"}],
    })

    def fake_build(**_kwargs):
        raise ContentGenerationStagePaused(requests_started=1, atoms_cached=2)

    monkeypatch.setattr(catalog_build, "_pipeline_signature", lambda _settings: "signature")
    monkeypatch.setattr(catalog_build, "_build_one_pack", fake_build)
    report = catalog_build.build_catalog_packs(
        plan_path=plan,
        output_dir=tmp_path / "packs",
        settings=_settings(concurrency=1),
        resume=True,
        content_request_limit=1,
    )

    assert report["status"] == "STAGED"
    assert report["ready"] == 0
    assert report["failed"] == 0
    assert report["staged"] == 1
    assert report["stages"][0]["requests_started"] == 1
    assert report["stages"][0]["atoms_cached"] == 2
