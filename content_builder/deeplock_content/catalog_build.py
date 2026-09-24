from __future__ import annotations

import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from .ai_review import run_ai_review
from .ai_review.reviewer import REVIEW_OUTPUT_VERBOSITY
from .contracts import (
    PACK_CONTRACT_FILENAME,
    PackContract,
    build_pack_contract,
    freeze_outline_assignment,
)
from .generation import (
    ContentGenerationStagePaused,
    ResponsesAdapter,
    generate_candidate,
)
from .generation.editorial_rewrite import (
    EDITORIAL_QUALITY_FEEDBACK_FILENAME,
    EDITORIAL_REWRITE_REPORT_FILENAME,
    EDITORIAL_REWRITE_SCHEMA_VERSION,
    editorial_prompt_sha256,
    editorial_schema_sha256,
    run_editorial_rewrite,
)
from .generation.api import DEFAULT_OUTPUT_VERBOSITY, shared_request_budget
from .generation.adapters import build_adapter
from .models import ExtractedCorpus, PackData
from .packaging import (
    LEARNER_REWRITE_REVIEW_SCOPE,
    LearnerRewriteGateConfig,
    ReviewGateConfig,
    package_content,
    verify_pack,
)
from .settings import MAX_ROUTER_CONCURRENCY, Settings
from .util import PROMPT_ROOT, REPO_ROOT, SCHEMA_ROOT, read_json, sha256_bytes, sha256_json, write_json
from .validators import validate_pack
from .vision_audit import ooxml_audit_contract_sha256


BUILD_PROVENANCE_FILENAME = "catalog-build-provenance.json"
EDITORIAL_INPUT_SNAPSHOT_FILENAME = "candidate-before-learner-rewrite.json"


def _illustration_frozen_identity(illustration: Any) -> dict[str, Any]:
    """Return illustration fields that a caption-only refresh may not change."""

    return {
        "illustration_id": illustration.illustration_id,
        "source_ref_id": illustration.source_ref_id,
        "asset_member": illustration.asset_member,
        "mime_type": illustration.mime_type,
        "sha256": illustration.sha256,
        "byte_size": illustration.byte_size,
        "width": illustration.width,
        "height": illustration.height,
        "page_number": illustration.page_number,
    }


def _restore_frozen_editorial_input(
    *,
    workspace: Path,
    generated: PackData,
    contract: PackContract,
) -> tuple[PackData, PackContract]:
    """Select the immutable pass-1 candidate used by the learner rewrite.

    Generation still runs first so current, deterministic curriculum metadata
    is available. Once a pass-1 snapshot exists, however, regenerated lesson
    or quiz content must never become editorial input. Only curriculum-owned
    display metadata may be refreshed. Stable course/illustration identities
    are checked before that refresh, and the complete restored pack is then
    checked against the current source/curriculum contract while freezing its
    original outline assignment.
    """

    snapshot_path = workspace / EDITORIAL_INPUT_SNAPSHOT_FILENAME
    if snapshot_path.is_file():
        try:
            frozen = PackData.model_validate(read_json(snapshot_path))
        except Exception as error:
            raise ValueError(
                "Frozen editorial input is unreadable; refusing to replace "
                f"{snapshot_path}: {error}"
            ) from error
    else:
        # Persist before any editorial mutation. This file is intentionally
        # write-once: later resumes either reuse it or fail closed.
        write_json(
            snapshot_path,
            generated.model_dump(mode="json", by_alias=True),
        )
        frozen = generated.model_copy(deep=True)

    if frozen.course.course_id != generated.course.course_id:
        raise ValueError(
            "Frozen editorial input has a stale course identity; refusing to "
            f"replace {snapshot_path}"
        )

    frozen_illustrations = [
        _illustration_frozen_identity(item) for item in frozen.illustrations
    ]
    generated_illustrations = [
        _illustration_frozen_identity(item) for item in generated.illustrations
    ]
    if frozen_illustrations != generated_illustrations:
        raise ValueError(
            "Frozen editorial input has stale illustration identity/source/"
            f"order/count; refusing to replace {snapshot_path}"
        )

    restored = frozen.model_copy(deep=True)
    # Course title/description/language and illustration captions/alt text are
    # owned by the inspected curriculum. Every other field, including model
    # snapshot, module/atom prose, questions and their builder metadata, comes
    # exclusively from the frozen pass-1 candidate.
    restored.course = generated.course.model_copy(deep=True)
    restored.illustrations = [
        item.model_copy(deep=True) for item in generated.illustrations
    ]
    try:
        frozen_contract = freeze_outline_assignment(contract, restored)
    except ValueError as error:
        raise ValueError(
            "Frozen editorial input is incompatible with the current "
            "identity/source/order/count contract; refusing to regenerate or "
            f"replace {snapshot_path}: {error}"
        ) from error
    return restored, frozen_contract


def _pipeline_signature(settings: Settings) -> str:
    """Bind resume to every prompt/schema/code path that can change a pack."""

    roots = [
        REPO_ROOT / "content_builder" / "deeplock_content",
        SCHEMA_ROOT,
        PROMPT_ROOT / settings.generator_prompt_version,
    ]
    if settings.editorial_rewrite_enabled:
        roots.append(PROMPT_ROOT / settings.editorial_prompt_version)
    else:
        roots.append(PROMPT_ROOT / settings.review_prompt_version)
    records: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Build provenance input is missing: {root}")
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if root.name == "deeplock_content" and path.suffix != ".py":
                continue
            records[path.relative_to(REPO_ROOT).as_posix()] = sha256_bytes(path.read_bytes())
    signature: dict[str, Any] = {
        "files": records,
        "generator_model": settings.generator_model,
        "generator_transport_model": settings.transport_model(settings.generator_model),
        "generator_reasoning_effort": settings.generator_reasoning_effort,
        "generator_output_verbosity": DEFAULT_OUTPUT_VERBOSITY,
        "max_atoms_per_generation_call": settings.max_atoms_per_generation_call,
        "generator_prompt_version": settings.generator_prompt_version,
        "editorial_rewrite_enabled": settings.editorial_rewrite_enabled,
    }
    if settings.editorial_rewrite_enabled:
        editorial_transport = settings.transport_model(settings.editorial_model)
        signature.update({
            "review_scope": LEARNER_REWRITE_REVIEW_SCOPE,
            "source_correctness_reviewed": False,
            "editorial_model": settings.editorial_model,
            "editorial_transport_model": editorial_transport,
            "editorial_reasoning_effort": settings.editorial_reasoning_effort,
            "editorial_output_verbosity": DEFAULT_OUTPUT_VERBOSITY,
            "max_atoms_per_editorial_call": settings.max_atoms_per_editorial_call,
            "editorial_prompt_version": settings.editorial_prompt_version,
            "editorial_prompt_sha256": editorial_prompt_sha256(
                settings.editorial_prompt_version
            ),
            "editorial_schema_version": EDITORIAL_REWRITE_SCHEMA_VERSION,
            "editorial_schema_sha256": editorial_schema_sha256(),
            "expected_editorial_snapshot": (
                os.getenv("OPENAI_EDITORIAL_MODEL_SNAPSHOT") or editorial_transport
            ),
        })
    else:
        signature.update({
            "review_model": settings.review_model,
            "review_transport_model": settings.transport_model(
                settings.review_model,
                reviewer=True,
            ),
            "review_reasoning_effort": settings.review_reasoning_effort,
            "review_output_verbosity": REVIEW_OUTPUT_VERBOSITY,
            "review_prompt_version": settings.review_prompt_version,
            "review_batch_size": settings.review_batch_size,
            "max_repair_rounds": settings.max_repair_rounds,
            "expected_review_snapshot": (
                os.getenv("OPENAI_REVIEW_MODEL_SNAPSHOT")
                or settings.transport_model(settings.review_model, reviewer=True)
            ),
        })
    return sha256_json(signature)


def _expected_workspace_provenance(
    settings: Settings,
    pipeline_signature: str,
    workspace: Path,
) -> dict[str, Any]:
    """Bind a cached pack to optional per-workspace editorial feedback."""

    provenance = _expected_provenance(settings, pipeline_signature)
    if not settings.editorial_rewrite_enabled:
        return provenance
    feedback_path = workspace / EDITORIAL_QUALITY_FEEDBACK_FILENAME
    if feedback_path.is_symlink():
        raise ValueError("Editorial quality feedback file may not be a symlink")
    if feedback_path.exists() and not feedback_path.is_file():
        raise ValueError("Editorial quality feedback path must be a regular file")
    provenance["editorial_quality_feedback_file_sha256"] = (
        sha256_bytes(feedback_path.read_bytes()) if feedback_path.is_file() else None
    )
    return provenance


def _expected_provenance(settings: Settings, pipeline_signature: str) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "schema_version": "1.0",
        "pipeline_signature_sha256": pipeline_signature,
        "generator_model": settings.generator_model,
        "generator_transport_model": settings.transport_model(settings.generator_model),
        "generator_reasoning_effort": settings.generator_reasoning_effort,
        "generator_output_verbosity": DEFAULT_OUTPUT_VERBOSITY,
        "max_atoms_per_generation_call": settings.max_atoms_per_generation_call,
        "generator_prompt_version": settings.generator_prompt_version,
        "editorial_rewrite_enabled": settings.editorial_rewrite_enabled,
    }
    if settings.editorial_rewrite_enabled:
        provenance.update({
            "review_scope": LEARNER_REWRITE_REVIEW_SCOPE,
            "source_correctness_reviewed": False,
            "editorial_model": settings.editorial_model,
            "editorial_transport_model": settings.transport_model(
                settings.editorial_model,
            ),
            "editorial_reasoning_effort": settings.editorial_reasoning_effort,
            "editorial_output_verbosity": DEFAULT_OUTPUT_VERBOSITY,
            "max_atoms_per_editorial_call": settings.max_atoms_per_editorial_call,
            "editorial_prompt_version": settings.editorial_prompt_version,
            "editorial_prompt_sha256": editorial_prompt_sha256(
                settings.editorial_prompt_version
            ),
            "editorial_schema_version": EDITORIAL_REWRITE_SCHEMA_VERSION,
            "editorial_schema_sha256": editorial_schema_sha256(),
        })
    else:
        provenance.update({
            "review_model": settings.review_model,
            "review_transport_model": settings.transport_model(
                settings.review_model,
                reviewer=True,
            ),
            "review_reasoning_effort": settings.review_reasoning_effort,
            "review_output_verbosity": REVIEW_OUTPUT_VERBOSITY,
            "review_prompt_version": settings.review_prompt_version,
            "review_batch_size": settings.review_batch_size,
            "max_repair_rounds": settings.max_repair_rounds,
        })
    return provenance


def _visual_audit_complete(
    curriculum: dict[str, Any],
    *,
    media_root: Path | None = None,
    expected_ooxml_contract_sha256: str | None = None,
) -> None:
    for module in curriculum.get("modules") or []:
        required = {int(page) for page in module.get("visual_audit_required_pages") or []}
        completed = {
            int(page)
            for field in ("page_text_overrides", "page_text_supplements")
            for page in (module.get(field) or {})
        }
        missing = required - completed
        if missing:
            raise ValueError(
                f"Visual page audit is incomplete for {module.get('output')}: {sorted(missing)}"
            )
        raw_ooxml = list(module.get("ooxml_visual_audit_occurrences") or [])
        if not raw_ooxml:
            continue
        if any(not isinstance(item, dict) for item in raw_ooxml):
            raise ValueError(f"Invalid OOXML visual audit requirements for {module.get('output')}")
        required_by_id = {
            str(item.get("occurrence_id") or ""): item
            for item in raw_ooxml
        }
        if "" in required_by_id or len(required_by_id) != len(raw_ooxml):
            raise ValueError(f"Duplicate/blank OOXML occurrence IDs for {module.get('output')}")
        completed = list(module.get("ooxml_visual_audit_completed") or [])
        if any(not isinstance(item, dict) for item in completed):
            raise ValueError(f"Invalid OOXML visual audit completion for {module.get('output')}")
        completed_by_id = {
            str(item.get("occurrence_id") or ""): item
            for item in completed
        }
        completed_ids = list(module.get("ooxml_visual_audit_completed_occurrence_ids") or [])
        expected_ids = sorted(required_by_id)
        if (
            "" in completed_by_id
            or len(completed_by_id) != len(completed)
            or sorted(completed_by_id) != expected_ids
            or completed_ids != expected_ids
        ):
            missing_occurrences = sorted(set(required_by_id) - set(completed_by_id))
            extra_occurrences = sorted(set(completed_by_id) - set(required_by_id))
            raise ValueError(
                f"OOXML visual audit is incomplete for {module.get('output')}: "
                f"missing={missing_occurrences}, extra={extra_occurrences}"
            )
        ordered_completed = [completed_by_id[item] for item in expected_ids]
        if module.get("ooxml_visual_audit_completed_sha256") != sha256_json(ordered_completed):
            raise ValueError(f"OOXML visual audit completion hash mismatch for {module.get('output')}")
        if (
            expected_ooxml_contract_sha256 is not None
            and module.get("ooxml_visual_audit_contract_sha256")
            != expected_ooxml_contract_sha256
        ):
            raise ValueError(f"Stale OOXML visual audit contract for {module.get('output')}")
        for occurrence_id in expected_ids:
            requirement = required_by_id[occurrence_id]
            record = completed_by_id[occurrence_id]
            if record.get("source_media_sha256") != requirement.get("ooxml_media_sha256"):
                raise ValueError(f"Stale OOXML source hash for occurrence {occurrence_id}")
            for field in ("png_sha256", "result_sha256"):
                digest = str(record.get(field) or "")
                if (
                    len(digest) != 71
                    or not digest.startswith("sha256:")
                    or any(character not in "0123456789abcdef" for character in digest[7:])
                ):
                    raise ValueError(f"Invalid {field} for OOXML occurrence {occurrence_id}")
            if type(record.get("useful_illustration")) is not bool:
                raise ValueError(f"Invalid usefulness for OOXML occurrence {occurrence_id}")
            if media_root is not None:
                asset_member = str(requirement.get("asset_member") or "")
                unresolved = media_root.resolve() / Path(asset_member)
                if unresolved.is_symlink():
                    raise ValueError(f"OOXML media may not be a symlink: {unresolved}")
                resolved = unresolved.resolve()
                try:
                    resolved.relative_to(media_root.resolve())
                except ValueError as error:
                    raise ValueError(
                        f"OOXML media escapes workspace for occurrence {occurrence_id}"
                    ) from error
                if (
                    not resolved.is_file()
                    or sha256_bytes(resolved.read_bytes()) != record["png_sha256"]
                ):
                    raise ValueError(f"OOXML PNG hash mismatch for occurrence {occurrence_id}")


def _build_one_pack(
    *,
    lecture: dict[str, Any],
    output_dir: Path,
    settings: Settings,
    pipeline_signature: str,
    resume: bool,
    content_request_limit: int | None = None,
) -> dict[str, Any]:
    workspace = Path(lecture["workspace"])
    output = output_dir / f"{lecture['lecture_id']}.dlpack"
    source = ExtractedCorpus.model_validate(read_json(workspace / "source.json"))
    curriculum = read_json(workspace / "curriculum.json")
    _visual_audit_complete(
        curriculum,
        media_root=workspace / "media",
        expected_ooxml_contract_sha256=ooxml_audit_contract_sha256(settings),
    )
    contract = build_pack_contract(
        source=source,
        curriculum=curriculum,
        media_root=workspace / "media",
    )
    expected_provenance = _expected_workspace_provenance(
        settings,
        pipeline_signature,
        workspace,
    )
    if resume and output.is_file():
        try:
            manifest = verify_pack(output)
            with zipfile.ZipFile(output, "r") as archive:
                cached_contract = PackContract.model_validate(
                    json.loads(archive.read(PACK_CONTRACT_FILENAME))
                )
            expected_contract = contract.model_copy(update={
                "outline_assignment_sha256": cached_contract.outline_assignment_sha256,
            })
            cached_provenance = read_json(workspace / BUILD_PROVENANCE_FILENAME)
            expected_cached_provenance = {
                **expected_provenance,
                "pack_sha256": sha256_bytes(output.read_bytes()),
            }
            if (
                cached_contract == expected_contract
                and cached_provenance == expected_cached_provenance
            ):
                return {
                    "lecture_id": lecture["lecture_id"],
                    "status": "CACHED",
                    "output": str(output.resolve()),
                    "content_pack_id": manifest["content_pack_id"],
                    "version": manifest["version"],
                }
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
            # A stale or interrupted pack is rebuilt below from the current
            # source/curriculum contract; it is never trusted by filename.
            pass
    write_json(workspace / PACK_CONTRACT_FILENAME, contract.model_dump(mode="json"))
    adapter = build_adapter(settings)
    pack = generate_candidate(
        corpus=source,
        workdir=workspace,
        settings=settings,
        model=settings.generator_model,
        prompt_version=settings.generator_prompt_version,
        resume=resume,
        curriculum=curriculum,
        adapter=adapter,
        content_request_limit=content_request_limit,
    )
    if settings.editorial_rewrite_enabled:
        pack, contract = _restore_frozen_editorial_input(
            workspace=workspace,
            generated=pack,
            contract=contract,
        )
    else:
        contract = freeze_outline_assignment(contract, pack)
    write_json(workspace / PACK_CONTRACT_FILENAME, contract.model_dump(mode="json"))
    if settings.editorial_rewrite_enabled:
        # This is the requested second pass over generated knowledge. It edits
        # the full learner-facing module once, but intentionally does not claim
        # to re-check facts against the source corpus.
        approved, review = run_editorial_rewrite(
            pack=pack,
            workdir=workspace,
            settings=settings,
            resume=resume,
            adapter=adapter,
            contract=contract,
            require_contract=True,
            require_frozen_outline=True,
        )
        write_json(
            workspace / "candidate.json",
            approved.model_dump(mode="json", by_alias=True),
        )
        write_json(workspace / EDITORIAL_REWRITE_REPORT_FILENAME, review)
        # Re-assert the final marker after the rewrite so a previous factual
        # AI_REVIEW_IN_PROGRESS file can never be mistaken for this pack's gate.
        write_json(
            workspace / "ai-review.json",
            {
                "status": "SKIPPED",
                "review_scope": LEARNER_REWRITE_REVIEW_SCOPE,
                "source_correctness_reviewed": False,
                "replacement": EDITORIAL_REWRITE_REPORT_FILENAME,
            },
        )
    else:
        deterministic = validate_pack(
            pack,
            contract,
            require_contract=True,
            require_frozen_outline=True,
        )
        if not deterministic.valid:
            details = "; ".join(
                f"{item.code}:{item.path}" for item in deterministic.issues
            )
            raise ValueError(f"Deterministic validation failed: {details}")
        write_json(
            workspace / "ai-review.json",
            {
                "status": "AI_REVIEW_IN_PROGRESS",
                "candidate_hash": "INVALID_UNTIL_CURRENT_REVIEW_COMPLETES",
            },
        )
        approved, review = run_ai_review(
            pack=pack,
            workdir=workspace,
            settings=settings,
            model=settings.review_model,
            prompt_version=settings.review_prompt_version,
            max_repair_rounds=settings.max_repair_rounds,
            resume=resume,
            adapter=adapter,
            contract=contract,
            require_contract=True,
            require_frozen_outline=True,
        )
        write_json(
            workspace / "candidate.json",
            approved.model_dump(mode="json", by_alias=True),
        )
        write_json(workspace / "ai-review.json", review)

    # In learner rewrite mode this is deliberately after the rewrite so a
    # model cannot change IDs, source ownership, quiz count, or frozen order.
    deterministic = validate_pack(
        approved,
        contract,
        require_contract=True,
        require_frozen_outline=True,
    )
    if not deterministic.valid:
        details = "; ".join(f"{item.code}:{item.path}" for item in deterministic.issues)
        raise ValueError(f"Deterministic validation failed: {details}")

    if settings.editorial_rewrite_enabled:
        configured_model = settings.transport_model(settings.editorial_model)
        expected_snapshot = (
            os.getenv("OPENAI_EDITORIAL_MODEL_SNAPSHOT") or configured_model
        )
        package_content(
            approved,
            review,
            output,
            LearnerRewriteGateConfig(
                editorial_model=settings.editorial_model,
                editorial_configured_model=configured_model,
                editorial_model_snapshot=expected_snapshot,
                editorial_prompt_version=settings.editorial_prompt_version,
                editorial_prompt_sha256=editorial_prompt_sha256(
                    settings.editorial_prompt_version
                ),
                editorial_schema_version=EDITORIAL_REWRITE_SCHEMA_VERSION,
                editorial_schema_sha256=editorial_schema_sha256(),
                editorial_reasoning_effort=settings.editorial_reasoning_effort,
            ),
            media_root=workspace / "media",
            contract=contract,
            require_contract=True,
            require_frozen_outline=True,
            review_scope=LEARNER_REWRITE_REVIEW_SCOPE,
        )
    else:
        configured_model = settings.transport_model(settings.review_model, reviewer=True)
        expected_snapshot = os.getenv("OPENAI_REVIEW_MODEL_SNAPSHOT") or configured_model
        package_content(
            approved,
            review,
            output,
            ReviewGateConfig(
                reviewer_model=settings.review_model,
                reviewer_configured_model=configured_model,
                reviewer_model_snapshot=expected_snapshot,
                reviewer_prompt_version=settings.review_prompt_version,
                reviewer_reasoning_effort=settings.review_reasoning_effort,
            ),
            media_root=workspace / "media",
            contract=contract,
            require_contract=True,
            require_frozen_outline=True,
        )
    manifest = verify_pack(output)
    write_json(workspace / BUILD_PROVENANCE_FILENAME, {
        **expected_provenance,
        "pack_sha256": sha256_bytes(output.read_bytes()),
    })
    return {
        "lecture_id": lecture["lecture_id"],
        "status": "READY",
        "output": str(output.resolve()),
        "content_pack_id": manifest["content_pack_id"],
        "version": manifest["version"],
        "atoms": len(approved.atoms),
        "questions": len(approved.questions),
        "source_refs": len(approved.source_refs),
        "illustrations": len(approved.illustrations),
    }


def build_catalog_packs(
    *,
    plan_path: Path,
    output_dir: Path,
    settings: Settings | None = None,
    resume: bool = True,
    content_request_limit: int | None = None,
    vision_per_lecture: bool = False,
) -> dict[str, Any]:
    """Build independent lecture packs with bounded 9router parallelism."""

    plan = read_json(plan_path)
    base_settings = settings or Settings.from_env()
    if content_request_limit is not None:
        if (
            isinstance(content_request_limit, bool)
            or not isinstance(content_request_limit, int)
            or content_request_limit < 1
        ):
            raise ValueError("content_request_limit must be a positive integer")
        if len(plan["lectures"]) != 1:
            raise ValueError(
                "A bounded content canary requires exactly one selected lecture"
            )
        if base_settings.concurrency != 1 or base_settings.max_api_attempts != 1:
            raise ValueError(
                "A bounded content canary requires concurrency=1 and max_api_attempts=1"
            )
    # Keep one explicit global request budget even when a small pilot has fewer
    # lectures than available slots. Split that budget between lecture workers
    # and their internal outline/generation/review pools so nested executors can
    # never exceed the requested provider concurrency.
    #
    # The ceiling is per provider: 20 reflects the 9router Codex account pool,
    # while a claude-* run goes to a separate relay whose limit is its own, so
    # it is allowed to use the concurrency it was actually given.
    # A gpt run used to be capped at 20, which with a 10-request budget left
    # every lecture with inner_worker_count == 1: editorial groups ran strictly
    # one at a time and took 48 minutes for 26 steps while 110 atom steps took
    # 14. Raise the ceiling so the budget can actually be spent in parallel.
    ceiling = 160 if base_settings.generator_model.startswith("claude-") else MAX_ROUTER_CONCURRENCY
    request_budget = max(1, min(ceiling, base_settings.concurrency))
    lecture_count = len(plan["lectures"])
    lecture_workers = max(1, int(os.getenv("NINEROUTER_LECTURE_WORKERS", str(request_budget))))
    worker_count = max(1, min(request_budget, lecture_count, lecture_workers))
    inner_worker_count = max(1, request_budget // worker_count)
    elastic = os.getenv("NINEROUTER_ELASTIC_REQUESTS") == "1" and not base_settings.generator_model.startswith("claude-")
    if elastic:
        # Let busy lectures borrow spare HTTP slots without creating an
        # unbounded executor for each of the sixteen lecture workers.
        inner_worker_count = max(inner_worker_count, min(32, request_budget))
    print({"phase": "request-budget", "total": request_budget, "lecture_workers": worker_count,
           "per_lecture_workers": inner_worker_count, "elastic": elastic}, flush=True)
    worker_settings = replace(
        base_settings,
        concurrency=inner_worker_count,
        review_batch_size=5,
    )
    pipeline_signature = _pipeline_signature(worker_settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    staged: list[dict[str, Any]] = []
    def build_lecture(lecture: dict[str, Any]) -> dict[str, Any]:
        if vision_per_lecture:
            from .vision_audit import audit_catalog_visual_pages

            # Each worker owns its report/cache directory and finishes vision
            # before generating this lecture. Other workers can build meanwhile.
            lecture_plan = plan_path.parent / "lecture-vision" / lecture["lecture_id"] / "catalog-plan.json"
            item = dict(lecture)
            workspace = Path(item["workspace"])
            if not workspace.is_absolute():
                workspace = (plan_path.parent / workspace).resolve()
            item["workspace"] = str(workspace)
            write_json(lecture_plan, {**plan, "lectures": [item]})
            print({"phase": "lecture-vision", "lecture_id": lecture["lecture_id"]}, flush=True)
            audit_catalog_visual_pages(plan_path=lecture_plan, settings=worker_settings, resume=resume)
            print({"phase": "lecture-build", "lecture_id": lecture["lecture_id"]}, flush=True)
        return _build_one_pack(
            lecture=lecture, output_dir=output_dir, settings=worker_settings,
            pipeline_signature=pipeline_signature, resume=resume,
            content_request_limit=content_request_limit,
        )

    with shared_request_budget(request_budget if elastic else None), ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="9router-lecture-pack",
    ) as pool:
        futures = {
            pool.submit(
                build_lecture,
                lecture,
            ): lecture
            for lecture in plan["lectures"]
        }
        for future in as_completed(futures):
            lecture = futures[future]
            try:
                result = future.result()
            except ContentGenerationStagePaused as pause:
                staged.append({
                    "lecture_id": lecture["lecture_id"],
                    "title": lecture["title"],
                    "status": "STAGED",
                    "requests_started": pause.requests_started,
                    "atoms_cached": pause.atoms_cached,
                    "message": str(pause),
                })
            except Exception as error:
                failures.append({
                    "lecture_id": lecture["lecture_id"],
                    "title": lecture["title"],
                    "error_type": type(error).__name__,
                    "error": str(error),
                })
            else:
                results.append(result)
            report = {
                "status": (
                    "BUILDING"
                    if len(results) + len(failures) + len(staged) < len(plan["lectures"])
                    else ("STAGED" if staged and not failures else "DONE")
                ),
                "expected": len(plan["lectures"]),
                "ready": len(results),
                "failed": len(failures),
                "staged": len(staged),
                "results": sorted(results, key=lambda item: item["lecture_id"]),
                "failures": sorted(failures, key=lambda item: item["lecture_id"]),
                "stages": sorted(staged, key=lambda item: item["lecture_id"]),
            }
            write_json(plan_path.parent / "catalog-build-report.json", report)
    if failures:
        raise RuntimeError(
            f"Catalog build incomplete: {len(results)}/{len(plan['lectures'])} ready, "
            f"{len(failures)} failed; rerun with resume after inspecting catalog-build-report.json"
        )
    return report
