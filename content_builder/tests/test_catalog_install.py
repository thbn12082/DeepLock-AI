from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeplock_content.ai_review import run_ai_review
from deeplock_content.curriculum.catalog_plan import finalize_android_catalog
from deeplock_content.generation import ModelResponse
from deeplock_content.packaging import (
    ReviewGateConfig,
    install_android_catalog,
    package_content,
    upgrade_android_catalog_pack,
    verify_pack,
)
from deeplock_content.settings import Settings
from deeplock_content.util import read_json, sha256_bytes, write_json


class _Approver:
    def structured(self, **kwargs):
        assert kwargs["output_verbosity"] == "low"
        payload = json.loads(kwargs["input_text"])
        source_ids = [
            item["source_ref_id"]
            for item in payload["source_context"]["source_refs"]
        ]
        return ModelResponse(
            data={
                "reviews": [{
                    "item_type": item["item_type"],
                    "item_id": item["item_id"],
                    "item_revision": item["item_revision"],
                    "candidate_hash": item["candidate_hash"],
                    "source_hash": item["source_hash"],
                    "verdict": "APPROVE",
                    "issue_codes": [],
                    "evidence_ref_ids": source_ids[:1],
                    "independent_correct_option_id": (
                        "B" if item["item_type"] == "QUESTION" else None
                    ),
                    "evidence_sufficient": True,
                    "short_rationale": "Nguá»“n há»— trá»£ káº¿t luáº­n.",
                } for item in payload["candidate_items"]],
            },
            response_id="review_fixture",
            model_snapshot="cx/gpt-5.5-review",
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


def _build_fixture_pack(valid_pack, root: Path, lecture_id: str) -> Path:
    approved, review = run_ai_review(
        pack=valid_pack,
        workdir=root / "review",
        settings=_settings(),
        model="gpt-5.5",
        prompt_version="reviewer-v1",
        max_repair_rounds=2,
        resume=False,
        adapter=_Approver(),
    )
    output = root / "packs" / f"{lecture_id}.dlpack"
    package_content(approved, review, output, ReviewGateConfig())
    verify_pack(output)
    return output


def _plan(path: Path, lecture_id: str) -> Path:
    plan = path / "catalog-plan.json"
    write_json(plan, {
        "source_hash": "sha256:" + "1" * 64,
        "categories": [{
            "category_id": "cat_mlops",
            "title": "MLOps",
            "description": "MLOps",
            "order_index": 0,
            "search_terms": ["MLflow"],
        }],
        "lectures": [{
            "lecture_id": lecture_id,
            "category_id": "cat_mlops",
            "title": "MLflow",
            "description": "Experiment tracking",
            "order_index": 0,
            "search_terms": ["tracking"],
        }],
    })
    return plan


def _installed_legacy_catalog(assets: Path, *, target_lecture_id: str) -> dict:
    """Install a coherent catalog whose pack bytes predate current verification."""

    pack_root = assets / "content" / "packs"
    pack_root.mkdir(parents=True)
    target_asset = pack_root / "legacy-target.dlpack"
    keep_asset = pack_root / "legacy-keep.dlpack"
    # These deliberately are not ZIP/dlpack payloads. The upgrade path must
    # authenticate an unchanged legacy asset by its committed checksum only.
    target_asset.write_bytes(b"legacy-target-invalid-under-current-semantic-gates")
    keep_asset.write_bytes(b"legacy-keep-invalid-under-current-semantic-gates")
    catalog = {
        "schema_version": "1.0",
        "catalog_id": "catalog_legacy_fixture",
        "title": "Legacy library",
        "language": "vi",
        "default_pack_id": "pack_legacy_target",
        "categories": [{
            "category_id": "cat_legacy",
            "title": "Legacy category",
            "description": "must remain byte-for-byte as metadata",
            "order_index": 0,
            "search_terms": ["legacy"],
        }],
        "lectures": [{
            "lecture_id": target_lecture_id,
            "category_id": "cat_legacy",
            "title": "Target lecture title",
            "description": "target metadata",
            "order_index": 0,
            "search_terms": ["target"],
        }, {
            "lecture_id": "lecture_keep",
            "category_id": "cat_legacy",
            "title": "Unchanged lecture title",
            "description": "unchanged metadata",
            "order_index": 1,
            "search_terms": ["keep"],
        }],
        "packs": [{
            "pack_id": "pack_legacy_target",
            "lecture_id": target_lecture_id,
            "asset_path": "content/packs/legacy-target.dlpack",
            "title": "Target descriptor title",
            "order_index": 0,
            "course_id": "course_legacy_target",
            "asset_sha256": sha256_bytes(target_asset.read_bytes()),
        }, {
            "pack_id": "pack_legacy_keep",
            "lecture_id": "lecture_keep",
            "asset_path": "content/packs/legacy-keep.dlpack",
            "title": "Keep descriptor title",
            "order_index": 0,
            "course_id": "course_legacy_keep",
            "asset_sha256": sha256_bytes(keep_asset.read_bytes()),
        }],
        "atoms": [{
            "pack_id": "pack_legacy_target",
            "atom_id": "atom_legacy_target",
            "pair_id": "pair_legacy_target",
            "lesson_id": "lesson_legacy_target",
            "module_id": "module_legacy_target",
            "title": "Old target atom",
            "order_index": 0,
            "prerequisite_ids": [],
            "question_ids": [
                "q_legacy_target_1", "q_legacy_target_2", "q_legacy_target_3",
            ],
        }, {
            "pack_id": "pack_legacy_keep",
            "atom_id": "atom_legacy_keep",
            "pair_id": "pair_legacy_keep",
            "lesson_id": "lesson_legacy_keep",
            "module_id": "module_legacy_keep",
            "title": "Unchanged atom",
            "order_index": 1,
            "prerequisite_ids": [],
            "question_ids": [
                "q_legacy_keep_1", "q_legacy_keep_2", "q_legacy_keep_3",
            ],
        }],
    }
    write_json(assets / "content" / "catalog.json", catalog)
    return catalog


def test_finalizer_verifies_pack_then_reconciles_manifest(
    valid_pack,
    tmp_path,
    monkeypatch,
):
    lecture_id = "lecture_mlflow"
    pack = _build_fixture_pack(valid_pack, tmp_path, lecture_id)
    plan = _plan(tmp_path, lecture_id)
    import deeplock_content.packaging as packaging

    real_verify = packaging.verify_pack
    calls: list[Path] = []

    def recording_verify(path: Path):
        calls.append(path)
        return real_verify(path)

    monkeypatch.setattr(packaging, "verify_pack", recording_verify)
    output = tmp_path / "catalog.json"
    catalog = finalize_android_catalog(
        plan_path=plan,
        packaged_dir=pack.parent,
        output=output,
        default_category_id="cat_mlops",
        default_lecture_id=lecture_id,
    )
    manifest = real_verify(pack)
    assert calls == [pack]
    assert catalog["packs"][0]["pack_id"] == manifest["content_pack_id"]
    assert catalog["packs"][0]["course_id"] == manifest["course_id"]
    assert catalog["packs"][0]["asset_sha256"] == sha256_bytes(pack.read_bytes())
    assert catalog["default_pack_id"] == manifest["content_pack_id"]

    with pytest.raises(ValueError, match="Unknown default lecture ID"):
        finalize_android_catalog(
            plan_path=plan,
            packaged_dir=pack.parent,
            output=tmp_path / "invalid-default.json",
            default_lecture_id="lecture_missing",
        )


def test_catalog_install_is_content_addressed_catalog_last_and_preserves_unrelated_assets(
    valid_pack,
    tmp_path,
):
    lecture_id = "lecture_mlflow"
    pack = _build_fixture_pack(valid_pack, tmp_path, lecture_id)
    plan = _plan(tmp_path, lecture_id)
    catalog_path = tmp_path / "finalized-catalog.json"
    finalized = finalize_android_catalog(
        plan_path=plan,
        packaged_dir=pack.parent,
        output=catalog_path,
        default_category_id="cat_mlops",
    )
    assets = tmp_path / "android-assets"
    unrelated = assets / "content" / "packs" / "keep-me.bin"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated-user-asset")
    old_catalog = assets / "content" / "catalog.json"
    write_json(old_catalog, {"schema_version": "old", "packs": []})

    report = install_android_catalog(
        catalog_path=catalog_path,
        packaged_dir=pack.parent,
        android_assets=assets,
    )
    installed = read_json(old_catalog)
    descriptor = installed["packs"][0]
    digest_hex = finalized["packs"][0]["asset_sha256"].removeprefix("sha256:")
    assert descriptor["asset_path"] == (
        f"content/packs/{lecture_id}.{digest_hex}.dlpack"
    )
    assert descriptor["pack_id"] == finalized["packs"][0]["pack_id"]
    assert descriptor["course_id"] == finalized["packs"][0]["course_id"]
    assert descriptor["asset_sha256"] == finalized["packs"][0]["asset_sha256"]
    installed_pack = assets / Path(descriptor["asset_path"])
    assert verify_pack(installed_pack)["content_pack_id"] == descriptor["pack_id"]
    assert sha256_bytes(installed_pack.read_bytes()) == descriptor["asset_sha256"]
    assert unrelated.read_bytes() == b"unrelated-user-asset"
    assert report["pack_count"] == 1
    assert report["newly_installed"] == 1
    assert report["reused"] == 0

    repeated = install_android_catalog(
        catalog_path=catalog_path,
        packaged_dir=pack.parent,
        android_assets=assets,
    )
    assert repeated["pack_count"] == 1
    assert repeated["newly_installed"] == 0
    assert repeated["reused"] == 1
    assert unrelated.read_bytes() == b"unrelated-user-asset"


def test_catalog_install_fails_closed_on_pack_coverage_mismatch(valid_pack, tmp_path):
    lecture_id = "lecture_mlflow"
    pack = _build_fixture_pack(valid_pack, tmp_path, lecture_id)
    plan = _plan(tmp_path, lecture_id)
    catalog_path = tmp_path / "finalized-catalog.json"
    finalize_android_catalog(
        plan_path=plan,
        packaged_dir=pack.parent,
        output=catalog_path,
        default_category_id="cat_mlops",
    )
    (pack.parent / "unplanned.dlpack").write_bytes(b"not-a-pack")
    assets = tmp_path / "android-assets"
    with pytest.raises(ValueError, match="coverage mismatch"):
        install_android_catalog(
            catalog_path=catalog_path,
            packaged_dir=pack.parent,
            android_assets=assets,
        )
    assert not (assets / "content" / "catalog.json").exists()


def test_single_pack_upgrade_accepts_checksum_authenticated_legacy_assets(
    valid_pack,
    tmp_path,
):
    lecture_id = "lecture_upgrade"
    new_pack = _build_fixture_pack(valid_pack, tmp_path / "new", lecture_id)
    manifest = verify_pack(new_pack)
    assets = tmp_path / "android-assets"
    before = _installed_legacy_catalog(assets, target_lecture_id=lecture_id)
    old_target = assets / "content" / "packs" / "legacy-target.dlpack"
    old_target_bytes = old_target.read_bytes()
    old_keep = assets / "content" / "packs" / "legacy-keep.dlpack"
    old_keep_bytes = old_keep.read_bytes()

    report = upgrade_android_catalog_pack(
        lecture_id=lecture_id,
        new_pack=new_pack,
        android_assets=assets,
    )

    installed = read_json(assets / "content" / "catalog.json")
    assert installed["categories"] == before["categories"]
    assert installed["lectures"] == before["lectures"]
    assert installed["packs"][1] == before["packs"][1]
    upgraded = installed["packs"][0]
    assert upgraded["title"] == before["packs"][0]["title"]
    assert upgraded["order_index"] == before["packs"][0]["order_index"]
    assert upgraded["pack_id"] == manifest["content_pack_id"]
    assert upgraded["course_id"] == manifest["course_id"]
    assert installed["default_pack_id"] == manifest["content_pack_id"]
    assert [item["order_index"] for item in installed["atoms"]] == list(
        range(len(installed["atoms"]))
    )
    new_atom_records = [
        item for item in installed["atoms"]
        if item["pack_id"] == manifest["content_pack_id"]
    ]
    assert len(new_atom_records) == len(valid_pack.atoms)
    assert all(item["pack_id"] != "pack_legacy_target" for item in installed["atoms"])
    installed_new_pack = assets / Path(upgraded["asset_path"])
    assert verify_pack(installed_new_pack)["content_pack_id"] == manifest["content_pack_id"]
    assert old_target.read_bytes() == old_target_bytes
    assert old_keep.read_bytes() == old_keep_bytes
    assert report["status"] == "UPGRADED"
    assert report["new_pack_verifications"] == ["SOURCE", "STAGED", "PUBLISHED"]
    assert report["unchanged_assets_sha256_verified"] == 1
    assert report["old_atom_count"] == 1
    assert report["new_atom_count"] == len(valid_pack.atoms)


def test_single_pack_upgrade_fails_closed_when_unchanged_legacy_asset_is_tampered(
    valid_pack,
    tmp_path,
):
    lecture_id = "lecture_upgrade"
    new_pack = _build_fixture_pack(valid_pack, tmp_path / "new", lecture_id)
    assets = tmp_path / "android-assets"
    _installed_legacy_catalog(assets, target_lecture_id=lecture_id)
    catalog_path = assets / "content" / "catalog.json"
    original_catalog_bytes = catalog_path.read_bytes()
    (assets / "content" / "packs" / "legacy-keep.dlpack").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="legacy asset checksum mismatch"):
        upgrade_android_catalog_pack(
            lecture_id=lecture_id,
            new_pack=new_pack,
            android_assets=assets,
        )

    assert catalog_path.read_bytes() == original_catalog_bytes
    assert not any(
        path.name.startswith(f"{lecture_id}.")
        for path in (assets / "content" / "packs").iterdir()
    )


def test_single_pack_upgrade_restores_exact_catalog_bytes_on_post_commit_failure(
    valid_pack,
    tmp_path,
    monkeypatch,
):
    lecture_id = "lecture_upgrade"
    new_pack = _build_fixture_pack(valid_pack, tmp_path / "new", lecture_id)
    assets = tmp_path / "android-assets"
    _installed_legacy_catalog(assets, target_lecture_id=lecture_id)
    catalog_path = assets / "content" / "catalog.json"
    original_catalog_bytes = catalog_path.read_bytes()

    import deeplock_content.packaging.catalog as catalog_module

    real_validate = catalog_module._validate_installed_catalog
    validation_calls = 0

    def fail_only_after_catalog_replace(value):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 3:
            raise ValueError("simulated post-commit verification failure")
        return real_validate(value)

    monkeypatch.setattr(
        catalog_module,
        "_validate_installed_catalog",
        fail_only_after_catalog_replace,
    )
    with pytest.raises(ValueError, match="post-commit verification failure"):
        upgrade_android_catalog_pack(
            lecture_id=lecture_id,
            new_pack=new_pack,
            android_assets=assets,
        )

    assert validation_calls == 3
    assert catalog_path.read_bytes() == original_catalog_bytes
