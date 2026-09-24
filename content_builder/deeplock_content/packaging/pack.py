from __future__ import annotations

import json
import re
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from ..ai_review.reviewer import (
    REVIEW_SCHEMA_VERSION,
    expected_review_items,
    review_prompt_sha256,
    review_schema_sha256,
)
from ..contracts import PACK_CONTRACT_FILENAME, PackContract, pack_contract_sha256
from ..generation.editorial_rewrite import (
    EDITORIAL_REWRITE_SCOPE,
    learner_rewrite_module_hash,
)
from ..models import AIReviewBatch, IllustrationAsset, Lesson, PackData, SourceRef
from ..util import canonical_json, load_schema, sha256_bytes, sha256_json, stable_id
from ..validators import validate_pack


MEMBER_BUILDERS = {
    "course.json": lambda pack: pack.course.model_dump(mode="json"),
    "modules.json": lambda pack: [item.model_dump(mode="json") for item in pack.modules],
    "full_lessons.json": lambda pack: [item.model_dump(mode="json") for item in pack.full_lessons],
    "atoms.json": lambda pack: [item.model_dump(mode="json") for item in pack.atoms],
    "lessons.json": lambda pack: [item.model_dump(mode="json") for item in pack.lessons],
    "learning_pairs.json": lambda pack: [item.model_dump(mode="json") for item in pack.learning_pairs],
    "questions.json": lambda pack: [item.model_dump(mode="json") for item in pack.questions],
    "mindmaps.json": lambda pack: [item.model_dump(mode="json", by_alias=True) for item in pack.mindmaps],
    "mini_labs.json": lambda pack: [item.model_dump(mode="json") for item in pack.mini_labs],
    "illustrations.json": lambda pack: [item.model_dump(mode="json") for item in pack.illustrations],
    "source_refs.json": lambda pack: [item.model_dump(mode="json") for item in pack.source_refs],
    "source_excerpts.json": lambda pack: [item.model_dump(mode="json") for item in pack.source_excerpts],
}

ASSET_MEMBER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:png|webp)")
MAX_PACK_ENTRIES = 512
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PACK_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_777_216
FACTUAL_REVIEW_SCOPE = "SOURCE_FACTUAL_REVIEW_V1"
LEARNER_REWRITE_REVIEW_SCOPE = EDITORIAL_REWRITE_SCOPE
EDITORIAL_REWRITE_APPROVED = "EDITORIAL_REWRITE_APPROVED"
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ReviewGateConfig:
    reviewer_model: str = "gpt-5.5"
    reviewer_configured_model: str = "cx/gpt-5.5-review"
    reviewer_model_snapshot: str = "cx/gpt-5.5-review"
    # Legacy callers/tests may still construct the gate directly. Production
    # CLI/catalog builds always pass Settings.review_prompt_version explicitly.
    reviewer_prompt_version: str = "reviewer-v1"
    reviewer_reasoning_effort: str = "high"


@dataclass(frozen=True)
class LearnerRewriteGateConfig:
    """Expected identity of the non-factual learner-facing rewrite pass.

    Prompt and schema hashes are deliberately supplied by the caller.  This
    keeps the packaging gate independent from the editor implementation while
    still preventing a report made with another prompt/schema from being used.
    """

    editorial_prompt_version: str
    editorial_prompt_sha256: str
    editorial_schema_version: str
    editorial_schema_sha256: str
    editorial_model: str = "gpt-5.5"
    editorial_configured_model: str = "cx/gpt-5.5"
    editorial_model_snapshot: str = "cx/gpt-5.5"
    editorial_reasoning_effort: str = "high"


def _review_required(message: str) -> None:
    raise PermissionError(f"AI_REVIEW_REQUIRED: {message}")


def _rewrite_required(message: str) -> None:
    raise PermissionError(f"EDITORIAL_REWRITE_REQUIRED: {message}")


def verify_learner_rewrite_gate(
    pack: PackData,
    review_report: dict[str, Any],
    config: LearnerRewriteGateConfig,
) -> None:
    """Verify a complete learner-facing rewrite without claiming source review."""

    try:
        if review_report.get("status") != EDITORIAL_REWRITE_APPROVED:
            _rewrite_required(f"report is not {EDITORIAL_REWRITE_APPROVED}")
        if review_report.get("review_scope") != LEARNER_REWRITE_REVIEW_SCOPE:
            _rewrite_required("review_scope is not LEARNER_REWRITE_ONLY_V1")
        if review_report.get("source_correctness_reviewed") is not False:
            _rewrite_required("source_correctness_reviewed must be false")

        expected_candidate_hash = sha256_json(pack.model_dump(mode="json", by_alias=True))
        if review_report.get("candidate_hash") != expected_candidate_hash:
            _rewrite_required("rewrite is stale for current candidate")
        if review_report.get("source_hash") != pack.source_hash:
            _rewrite_required("rewrite source hash is stale")

        exact_config = {
            "editorial_model": config.editorial_model,
            "editorial_configured_model": config.editorial_configured_model,
            "editorial_prompt_version": config.editorial_prompt_version,
            "editorial_prompt_sha256": config.editorial_prompt_sha256,
            "editorial_schema_version": config.editorial_schema_version,
            "editorial_schema_sha256": config.editorial_schema_sha256,
            "editorial_reasoning_effort": config.editorial_reasoning_effort,
            "store_responses": False,
        }
        for field, expected_value in exact_config.items():
            if review_report.get(field) != expected_value:
                _rewrite_required(f"rewrite config mismatch for {field}")
        for field in ("editorial_prompt_sha256", "editorial_schema_sha256"):
            if not isinstance(exact_config[field], str) or not _SHA256_PATTERN.fullmatch(exact_config[field]):
                _rewrite_required(f"invalid expected hash for {field}")
        if review_report.get("editorial_model_snapshots") != [config.editorial_model_snapshot]:
            _rewrite_required("rewrite model snapshot is missing or stale")

        raw_results = review_report.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            _rewrite_required("rewrite results are missing or empty")
        if review_report.get("results_sha256") != sha256_json(raw_results):
            _rewrite_required("rewrite results hash mismatch")
        rejected_count = review_report.get("rejected_count")
        if not isinstance(rejected_count, int) or isinstance(rejected_count, bool) or rejected_count != 0:
            _rewrite_required("rewrite report contains rejected modules")
        repaired_count = review_report.get("repaired_count")
        if not isinstance(repaired_count, int) or isinstance(repaired_count, bool) or repaired_count < 0:
            _rewrite_required("invalid rewritten module count")

        expected_ids = [item.module_id for item in pack.modules]
        if len(expected_ids) != len(set(expected_ids)):
            _rewrite_required("candidate contains duplicate module IDs")
        actual_ids: list[str] = []
        for result in raw_results:
            if not isinstance(result, dict):
                _rewrite_required("rewrite result is not an object")
            module_id = result.get("module_id")
            if not isinstance(module_id, str):
                _rewrite_required("rewrite result has no module_id")
            actual_ids.append(module_id)
            if result.get("verdict") != "APPROVE":
                _rewrite_required(f"module is not approved: {module_id}")
            if result.get("issue_codes", []) != []:
                _rewrite_required(f"module has unresolved issues: {module_id}")
            if result.get("output_hash") != learner_rewrite_module_hash(pack, module_id):
                _rewrite_required(f"module output hash mismatch: {module_id}")
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
            _rewrite_required("rewrite module coverage mismatch")
        approved_items = review_report.get("approved_items")
        if (
            not isinstance(approved_items, int)
            or isinstance(approved_items, bool)
            or approved_items != len(expected_ids)
        ):
            _rewrite_required("approved module count mismatch")

        if any(pair.ai_review_status != "AI_APPROVED" for pair in pack.learning_pairs):
            _rewrite_required("pair is not editorially AI_APPROVED")
        if any(lab.ai_review_status != "AI_APPROVED" for lab in pack.mini_labs):
            _rewrite_required("Mini Lab is not editorially AI_APPROVED")
    except PermissionError:
        raise
    except Exception as error:
        raise PermissionError(f"EDITORIAL_REWRITE_REQUIRED: malformed rewrite report: {error}") from error


def verify_ai_review_gate(
    pack: PackData,
    review_report: dict[str, Any],
    config: ReviewGateConfig,
) -> None:
    """Fail closed unless this exact candidate has a fresh, current review."""

    try:
        if review_report.get("status") != "AI_APPROVED":
            _review_required("report is not AI_APPROVED")
        expected_candidate_hash = sha256_json(pack.model_dump(mode="json", by_alias=True))
        if review_report.get("candidate_hash") != expected_candidate_hash:
            _review_required("review is stale for current candidate")
        if review_report.get("source_hash") != pack.source_hash:
            _review_required("review source hash is stale")

        exact_config = {
            "reviewer_model": config.reviewer_model,
            "reviewer_configured_model": config.reviewer_configured_model,
            "reviewer_prompt_version": config.reviewer_prompt_version,
            "reviewer_prompt_sha256": review_prompt_sha256(config.reviewer_prompt_version),
            "reviewer_schema_version": REVIEW_SCHEMA_VERSION,
            "reviewer_schema_sha256": review_schema_sha256(),
            "reviewer_reasoning_effort": config.reviewer_reasoning_effort,
            "store_responses": False,
        }
        for field, expected_value in exact_config.items():
            if review_report.get(field) != expected_value:
                _review_required(f"reviewer config mismatch for {field}")
        snapshots = review_report.get("reviewer_model_snapshots")
        if snapshots != [config.reviewer_model_snapshot]:
            _review_required("reviewer model snapshot is missing or stale")

        raw_results = review_report.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            _review_required("review results are missing or empty")
        if review_report.get("results_sha256") != sha256_json(raw_results):
            _review_required("review results hash mismatch")
        try:
            batch = AIReviewBatch.model_validate({"reviews": raw_results})
        except ValidationError as error:
            _review_required(f"review results schema invalid: {error}")

        expected_items = expected_review_items(pack)
        actual_ids = [item.item_id for item in batch.reviews]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_items):
            _review_required("review item coverage mismatch")
        if review_report.get("approved_items") != len(expected_items):
            _review_required("approved item count mismatch")
        if review_report.get("rejected_count") != 0:
            _review_required("report contains rejected items")
        repaired_count = review_report.get("repaired_count")
        if not isinstance(repaired_count, int) or isinstance(repaired_count, bool) or repaired_count < 0:
            _review_required("invalid repaired item count")

        questions = {item.question_id: item for item in pack.questions}
        for result in batch.reviews:
            expected = expected_items[result.item_id]
            metadata = {
                "item_type": expected["item_type"],
                "item_revision": expected["item_revision"],
                "candidate_hash": expected["candidate_hash"],
                "source_hash": expected["source_hash"],
            }
            for field, expected_value in metadata.items():
                if getattr(result, field) != expected_value:
                    _review_required(f"stale/tampered {field} for {result.item_id}")
            if result.verdict != "APPROVE" or result.issue_codes:
                _review_required(f"item is not cleanly approved: {result.item_id}")
            if not result.evidence_sufficient:
                _review_required(f"insufficient evidence for {result.item_id}")
            evidence = result.evidence_ref_ids
            owned = set(expected["owned_evidence_ref_ids"])
            if not evidence or len(evidence) != len(set(evidence)) or not set(evidence) <= owned:
                _review_required(f"empty, duplicate or unowned evidence for {result.item_id}")
            if result.item_type == "QUESTION":
                if result.independent_correct_option_id != questions[result.item_id].correct_option_id:
                    _review_required(f"independent answer disagreement for {result.item_id}")
            elif result.independent_correct_option_id is not None:
                _review_required(f"unexpected independent answer for {result.item_id}")

        if any(pair.ai_review_status != "AI_APPROVED" for pair in pack.learning_pairs):
            _review_required("pair is not AI_APPROVED")
        if any(lab.ai_review_status != "AI_APPROVED" for lab in pack.mini_labs):
            _review_required("Mini Lab is not AI_APPROVED")
    except PermissionError:
        raise
    except Exception as error:
        raise PermissionError(f"AI_REVIEW_REQUIRED: malformed review report: {error}") from error


def _zip_write(zip_file: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zip_file.writestr(info, payload)


def _image_dimensions(payload: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        signature = b"\x89PNG\r\n\x1a\n"
        if len(payload) < 24 or payload[:8] != signature or payload[12:16] != b"IHDR":
            raise ValueError("Illustration is not a valid PNG header")
        width = int.from_bytes(payload[16:20], "big")
        height = int.from_bytes(payload[20:24], "big")
    elif mime_type == "image/webp":
        if len(payload) < 30 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
            raise ValueError("Illustration is not a valid WebP header")
        if int.from_bytes(payload[4:8], "little") + 8 != len(payload):
            raise ValueError("WebP RIFF size does not match the asset bytes")
        kind = payload[12:16]
        if kind == b"VP8X":
            width = int.from_bytes(payload[24:27], "little") + 1
            height = int.from_bytes(payload[27:30], "little") + 1
        elif kind == b"VP8L":
            if payload[20] != 0x2F or len(payload) < 25:
                raise ValueError("Illustration has an invalid lossless WebP header")
            bits = int.from_bytes(payload[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif kind == b"VP8 ":
            if payload[23:26] != b"\x9d\x01\x2a":
                raise ValueError("Illustration has an invalid lossy WebP frame header")
            width = int.from_bytes(payload[26:28], "little") & 0x3FFF
            height = int.from_bytes(payload[28:30], "little") & 0x3FFF
        else:
            raise ValueError(f"Unsupported WebP chunk type: {kind!r}")
    else:
        raise ValueError(f"Unsupported illustration mime_type: {mime_type}")
    if width < 1 or height < 1:
        raise ValueError("Illustration dimensions must be positive")
    return width, height


def _verify_asset_payload(asset: IllustrationAsset, payload: bytes) -> None:
    expected_suffix = ".png" if asset.mime_type == "image/png" else ".webp"
    if not asset.asset_member.endswith(expected_suffix):
        raise ValueError(f"Illustration extension/MIME mismatch: {asset.asset_member}")
    if len(payload) != asset.byte_size:
        raise ValueError(f"Illustration byte_size mismatch: {asset.asset_member}")
    if sha256_bytes(payload) != asset.sha256:
        raise ValueError(f"Illustration SHA-256 mismatch: {asset.asset_member}")
    dimensions = _image_dimensions(payload, asset.mime_type)
    if (
        dimensions[0] > MAX_IMAGE_DIMENSION
        or dimensions[1] > MAX_IMAGE_DIMENSION
        or dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS
    ):
        raise ValueError(f"Illustration dimensions exceed runtime limits: {asset.asset_member}")
    if dimensions != (asset.width, asset.height):
        raise ValueError(
            f"Illustration dimensions mismatch: {asset.asset_member}; "
            f"metadata={asset.width}x{asset.height}, bytes={dimensions[0]}x{dimensions[1]}"
        )


def _load_illustration_members(pack: PackData, media_root: Path | None) -> dict[str, bytes]:
    if not pack.illustrations:
        return {}
    if media_root is None or not media_root.is_dir():
        raise ValueError("Illustrations require an existing workdir/media directory")
    root = media_root.resolve()
    members: dict[str, bytes] = {}
    for asset in sorted(pack.illustrations, key=lambda item: item.asset_member):
        name = asset.asset_member
        if not ASSET_MEMBER_PATTERN.fullmatch(name) or Path(name).name != name:
            raise ValueError(f"Illustration asset_member must be a flat PNG/WebP filename: {name}")
        if name in MEMBER_BUILDERS or name in {"manifest.json", "ai_review_manifest.json"}:
            raise ValueError(f"Illustration asset_member collides with a reserved pack member: {name}")
        if name in members:
            raise ValueError(f"Duplicate illustration asset_member: {name}")
        source = media_root / name
        if source.is_symlink() or not source.is_file() or source.resolve().parent != root:
            raise ValueError(f"Missing or unsafe illustration asset: {source}")
        if source.stat().st_size != asset.byte_size or asset.byte_size > MAX_IMAGE_BYTES:
            raise ValueError(f"Illustration byte_size mismatch: {asset.asset_member}")
        payload = source.read_bytes()
        _verify_asset_payload(asset, payload)
        members[name] = payload
    return members


def _decode_illustrations(payload: bytes) -> list[IllustrationAsset]:
    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise ValueError("illustrations.json must contain an array")
    try:
        return [IllustrationAsset.model_validate(item) for item in raw]
    except ValidationError as error:
        raise ValueError(f"Invalid illustrations.json: {error}") from error


def _verify_illustration_links(
    illustrations: list[IllustrationAsset],
    lessons_payload: bytes,
    refs_payload: bytes,
) -> None:
    try:
        lessons = [Lesson.model_validate(item) for item in json.loads(lessons_payload)]
        source_refs = [SourceRef.model_validate(item) for item in json.loads(refs_payload)]
    except (TypeError, ValidationError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot verify illustration links: {error}") from error
    ids = [item.illustration_id for item in illustrations]
    members = [item.asset_member for item in illustrations]
    if any(count > 1 for count in Counter(ids).values()):
        raise ValueError("Duplicate illustration_id in illustrations.json")
    if any(count > 1 for count in Counter(members).values()):
        raise ValueError("Duplicate illustration asset_member in illustrations.json")
    known_sources = {item.source_ref_id: item for item in source_refs}
    assets_by_source = {item.source_ref_id for item in illustrations}
    used_sources: set[str] = set()
    for lesson in lessons:
        selected = lesson.illustration_source_ref_ids
        if len(selected) != len(set(selected)):
            raise ValueError(f"Duplicate lesson illustration source: {lesson.lesson_id}")
        if not set(selected) <= set(lesson.source_ref_ids):
            raise ValueError(f"Lesson illustration is outside owned sources: {lesson.lesson_id}")
        if not set(selected) <= assets_by_source:
            raise ValueError(f"Lesson illustration asset is missing: {lesson.lesson_id}")
        used_sources.update(selected)
    for asset in illustrations:
        source_ref = known_sources.get(asset.source_ref_id)
        if source_ref is None:
            raise ValueError(f"Illustration source_ref is missing: {asset.illustration_id}")
        if not source_ref.page_start <= asset.page_number <= source_ref.page_end:
            raise ValueError(f"Illustration page is outside source_ref: {asset.illustration_id}")
        if not asset.caption.strip() or not asset.alt_text.strip():
            raise ValueError(f"Illustration caption/alt text is blank: {asset.illustration_id}")
        if asset.source_ref_id not in used_sources:
            raise ValueError(f"Illustration is not used by a lesson: {asset.illustration_id}")



def _logical_model(pack: PackData) -> str:
    """The logical model behind a pack, derived from its generator snapshot.

    The snapshot is the transport name (``cx/gpt-5.5`` on the 9router,
    ``claude-opus-5`` on the relay) and may list several comma-separated
    snapshots when a pack was assembled from more than one call.
    """

    snapshot = (pack.generator_model_snapshot or "").split(",")[0].strip()
    name = snapshot.rsplit("/", 1)[-1].removesuffix("-review")
    # cx/gpt-5.6-luna -> gpt-5.6-luna, cx/gpt-5.5 -> gpt-5.5. Returning a
    # constant here would have stamped every luna pack as gpt-5.5.
    return name or "gpt-5.5"


def package_content(
    pack: PackData,
    review_report: dict[str, Any],
    output: Path,
    review_config: ReviewGateConfig | LearnerRewriteGateConfig | None = None,
    media_root: Path | None = None,
    contract: PackContract | None = None,
    require_contract: bool = False,
    require_frozen_outline: bool = False,
    review_scope: str | None = None,
) -> Path:
    validation = validate_pack(
        pack,
        contract,
        require_contract=require_contract,
        require_frozen_outline=require_frozen_outline,
    )
    if not validation.valid:
        raise ValueError(
            "Candidate failed deterministic validation before packaging: "
            + "; ".join(f"{item.code}:{item.path}" for item in validation.issues)
        )
    if review_report.get("review_scope") == LEARNER_REWRITE_REVIEW_SCOPE and review_scope is None:
        _rewrite_required("package_content requires explicit review_scope for a learner rewrite")
    effective_scope = review_scope or FACTUAL_REVIEW_SCOPE
    if effective_scope == LEARNER_REWRITE_REVIEW_SCOPE:
        if not isinstance(review_config, LearnerRewriteGateConfig):
            _rewrite_required("LearnerRewriteGateConfig is required")
        verify_learner_rewrite_gate(pack, review_report, review_config)
        source_correctness_reviewed = False
    elif effective_scope == FACTUAL_REVIEW_SCOPE:
        if review_config is None:
            review_config = ReviewGateConfig()
        if not isinstance(review_config, ReviewGateConfig):
            _review_required("ReviewGateConfig is required for factual review")
        verify_ai_review_gate(pack, review_report, review_config)
        source_correctness_reviewed = True
    else:
        raise ValueError(f"Unsupported review_scope: {effective_scope}")
    if contract is not None:
        expected_contract_hash = pack_contract_sha256(contract)
        if review_report.get("pack_contract_sha256") != expected_contract_hash:
            if effective_scope == LEARNER_REWRITE_REVIEW_SCOPE:
                _rewrite_required("rewrite PackContract is missing or stale")
            _review_required("review PackContract is missing or stale")
    expected_candidate_hash = sha256_json(pack.model_dump(mode="json", by_alias=True))

    members = {name: canonical_json(builder(pack)) for name, builder in MEMBER_BUILDERS.items()}
    members.update(_load_illustration_members(pack, media_root))
    if contract is not None:
        members[PACK_CONTRACT_FILENAME] = canonical_json(contract.model_dump(mode="json"))
    if effective_scope == LEARNER_REWRITE_REVIEW_SCOPE:
        # Keep the legacy reviewer_* projection because the Android manifest
        # schema/runtime already understands it, while retaining the explicit
        # editorial_* identity so this pass is never presented as factual review.
        legacy_review_metadata = {
            "reviewer_model": review_report["editorial_model"],
            "reviewer_configured_model": review_report["editorial_configured_model"],
            "reviewer_model_snapshots": review_report["editorial_model_snapshots"],
            "reviewer_prompt_version": review_report["editorial_prompt_version"],
            "reviewer_prompt_sha256": review_report["editorial_prompt_sha256"],
            "reviewer_schema_version": review_report["editorial_schema_version"],
            "reviewer_schema_sha256": review_report["editorial_schema_sha256"],
            "reviewer_reasoning_effort": review_report["editorial_reasoning_effort"],
        }
        editorial_metadata = {
            field: review_report[field]
            for field in (
                "editorial_model",
                "editorial_configured_model",
                "editorial_model_snapshots",
                "editorial_prompt_version",
                "editorial_prompt_sha256",
                "editorial_schema_version",
                "editorial_schema_sha256",
                "editorial_reasoning_effort",
            )
        }
    else:
        legacy_review_metadata = {
            field: review_report[field]
            for field in (
                "reviewer_model",
                "reviewer_configured_model",
                "reviewer_model_snapshots",
                "reviewer_prompt_version",
                "reviewer_prompt_sha256",
                "reviewer_schema_version",
                "reviewer_schema_sha256",
                "reviewer_reasoning_effort",
            )
        }
        editorial_metadata = {}
    review_manifest = {
        "schema_version": "1.0",
        "status": review_report["status"],
        "review_scope": effective_scope,
        "source_correctness_reviewed": source_correctness_reviewed,
        "candidate_hash": expected_candidate_hash,
        "source_hash": pack.source_hash,
        "generator_model_snapshot": pack.generator_model_snapshot,
        **legacy_review_metadata,
        **editorial_metadata,
        "store_responses": review_report["store_responses"],
        "approved_items": review_report["approved_items"],
        "repaired_count": review_report.get("repaired_count", 0),
        "rejected_count": review_report.get("rejected_count", 0),
        "results_sha256": review_report["results_sha256"],
        "results": review_report["results"],
    }
    # A pack assembled from cache can carry synthetic APPROVE verdicts. Carry
    # the real coverage counters through so the pack discloses how much of it
    # a model actually rewrote instead of implying a full editorial pass.
    editorial_coverage = review_report.get("editorial_rewrite_coverage")
    if editorial_coverage is not None:
        review_manifest["editorial_rewrite_coverage"] = editorial_coverage
    if contract is not None:
        review_manifest["pack_contract_sha256"] = pack_contract_sha256(contract)
    members["ai_review_manifest.json"] = canonical_json(review_manifest)
    member_hashes = {name: sha256_bytes(payload) for name, payload in members.items()}
    version_hash = sha256_json({"members": member_hashes, "source_hash": pack.source_hash})
    manifest = {
        "schema_version": "1.0",
        "content_pack_id": stable_id("pack", pack.course.course_id, version_hash),
        "course_id": pack.course.course_id,
        "version": version_hash.removeprefix("sha256:")[:16],
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "prompt_version": pack.generator_prompt_version,
        # Report the logical model that actually produced this pack. Hardcoding
        # gpt-5.5 made a Claude-built pack claim the wrong provenance while its
        # own generator_model_snapshot said otherwise.
        "model": _logical_model(pack),
        "members": member_hashes,
        "review_status": review_report["status"],
        "review_scope": effective_scope,
        "source_correctness_reviewed": source_correctness_reviewed,
        "candidate_hash": expected_candidate_hash,
        "source_hash": pack.source_hash,
        "generator_model_snapshot": pack.generator_model_snapshot,
        "ai_review_results_sha256": review_report["results_sha256"],
        "ai_approved_count": review_report["approved_items"],
        "repaired_count": review_report.get("repaired_count", 0),
        "rejected_count": review_report.get("rejected_count", 0),
        **legacy_review_metadata,
        **editorial_metadata,
        "store_responses": review_report["store_responses"],
    }
    if editorial_coverage is not None:
        manifest["editorial_rewrite_coverage"] = editorial_coverage
    if contract is not None:
        manifest["pack_contract_sha256"] = pack_contract_sha256(contract)
    Draft202012Validator(load_schema("content-pack.schema.json"), format_checker=FormatChecker()).validate(manifest)
    members = {"manifest.json": canonical_json(manifest), **members}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, payload in members.items():
                _zip_write(archive, name, payload)
        verify_pack(temporary)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _pack_from_archive(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    review_manifest: dict[str, Any],
) -> PackData:
    try:
        return PackData(
            course=json.loads(archive.read("course.json")),
            modules=json.loads(archive.read("modules.json")),
            full_lessons=json.loads(archive.read("full_lessons.json")),
            atoms=json.loads(archive.read("atoms.json")),
            lessons=json.loads(archive.read("lessons.json")),
            learning_pairs=json.loads(archive.read("learning_pairs.json")),
            questions=json.loads(archive.read("questions.json")),
            mindmaps=json.loads(archive.read("mindmaps.json")),
            mini_labs=json.loads(archive.read("mini_labs.json")),
            illustrations=(
                json.loads(archive.read("illustrations.json"))
                if "illustrations.json" in archive.namelist()
                else []
            ),
            source_refs=json.loads(archive.read("source_refs.json")),
            source_excerpts=json.loads(archive.read("source_excerpts.json")),
            source_hash=review_manifest["source_hash"],
            generator_model_snapshot=review_manifest["generator_model_snapshot"],
            generator_prompt_version=manifest["prompt_version"],
        )
    except (KeyError, TypeError, ValidationError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid packaged candidate content: {error}") from error


def verify_pack(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        raw_names = [item.filename for item in infos]
        names = set(raw_names)
        if len(raw_names) != len(names):
            raise ValueError("Pack contains duplicate ZIP members")
        if len(infos) > MAX_PACK_ENTRIES:
            raise ValueError("Pack contains too many members")
        if any(item.file_size > MAX_MEMBER_BYTES for item in infos):
            raise ValueError("Pack member exceeds the runtime size limit")
        if sum(item.file_size for item in infos) > MAX_PACK_BYTES:
            raise ValueError("Pack exceeds the runtime size limit")
        required = {
            "manifest.json",
            "ai_review_manifest.json",
            *(name for name in MEMBER_BUILDERS if name != "illustrations.json"),
        }
        if not required <= names:
            raise ValueError(f"Pack members mismatch; missing={required - names}")
        manifest = json.loads(archive.read("manifest.json"))
        Draft202012Validator(load_schema("content-pack.schema.json"), format_checker=FormatChecker()).validate(manifest)
        if set(manifest["members"]) != names - {"manifest.json"}:
            raise ValueError("Manifest member set does not match archive")
        for name, expected in manifest["members"].items():
            actual = sha256_bytes(archive.read(name))
            if actual != expected:
                raise ValueError(f"Member hash mismatch: {name}")
        illustrations = (
            _decode_illustrations(archive.read("illustrations.json"))
            if "illustrations.json" in names
            else []
        )
        illustration_members = {item.asset_member for item in illustrations}
        optional_metadata = {"illustrations.json"} if "illustrations.json" in names else set()
        if PACK_CONTRACT_FILENAME in names:
            optional_metadata.add(PACK_CONTRACT_FILENAME)
        expected_names = required | optional_metadata | illustration_members
        if names != expected_names:
            raise ValueError(f"Pack members mismatch; missing={expected_names - names}, extra={names - expected_names}")
        _verify_illustration_links(
            illustrations,
            archive.read("lessons.json"),
            archive.read("source_refs.json"),
        )
        for asset in illustrations:
            if not ASSET_MEMBER_PATTERN.fullmatch(asset.asset_member):
                raise ValueError(f"Unsafe illustration asset_member: {asset.asset_member}")
            _verify_asset_payload(asset, archive.read(asset.asset_member))
        review_manifest = json.loads(archive.read("ai_review_manifest.json"))
        if review_manifest["results_sha256"] != manifest["ai_review_results_sha256"]:
            raise ValueError("AI review manifest hash mismatch")
        results = review_manifest.get("results")
        if not isinstance(results, list) or not results:
            raise ValueError("AI review results are missing")
        if sha256_json(results) != review_manifest["results_sha256"]:
            raise ValueError("AI review results payload hash mismatch")
        if review_manifest.get("approved_items") != len(results):
            raise ValueError("AI review approved count mismatch")
        if review_manifest.get("rejected_count") != 0 or any(item.get("verdict") != "APPROVE" for item in results):
            raise ValueError("AI review manifest contains rejected/unapproved items")

        declared_scope = manifest.get("review_scope")
        review_scope = review_manifest.get("review_scope")
        if declared_scope is None:
            if review_scope is not None:
                raise ValueError("Review scope exists only in AI review manifest")
            effective_scope = FACTUAL_REVIEW_SCOPE
        else:
            if review_scope != declared_scope:
                raise ValueError("Review scope mismatch")
            effective_scope = declared_scope

        if effective_scope == LEARNER_REWRITE_REVIEW_SCOPE:
            if manifest.get("review_status") != EDITORIAL_REWRITE_APPROVED:
                raise ValueError("Learner rewrite manifest has the wrong review status")
            if review_manifest.get("status") != EDITORIAL_REWRITE_APPROVED:
                raise ValueError("Learner rewrite report has the wrong review status")
            if manifest.get("source_correctness_reviewed") is not False:
                raise ValueError("Learner rewrite manifest falsely claims source review")
            if review_manifest.get("source_correctness_reviewed") is not False:
                raise ValueError("Learner rewrite report falsely claims source review")
            metadata_fields = (
                "candidate_hash",
                "source_hash",
                "generator_model_snapshot",
                "reviewer_model",
                "reviewer_configured_model",
                "reviewer_model_snapshots",
                "reviewer_prompt_version",
                "reviewer_prompt_sha256",
                "reviewer_schema_version",
                "reviewer_schema_sha256",
                "reviewer_reasoning_effort",
                "editorial_model",
                "editorial_configured_model",
                "editorial_model_snapshots",
                "editorial_prompt_version",
                "editorial_prompt_sha256",
                "editorial_schema_version",
                "editorial_schema_sha256",
                "editorial_reasoning_effort",
                "store_responses",
            )
            for field in metadata_fields:
                if manifest.get(field) != review_manifest.get(field):
                    raise ValueError(f"Learner rewrite metadata mismatch: {field}")
            if manifest.get("ai_approved_count") != review_manifest.get("approved_items"):
                raise ValueError("Learner rewrite approved count mismatch")
            snapshots = manifest.get("editorial_model_snapshots")
            if not isinstance(snapshots, list) or len(snapshots) != 1:
                raise ValueError("Learner rewrite requires exactly one model snapshot")
            legacy_projection = {
                "reviewer_model": manifest.get("editorial_model"),
                "reviewer_configured_model": manifest.get("editorial_configured_model"),
                "reviewer_model_snapshots": manifest.get("editorial_model_snapshots"),
                "reviewer_prompt_version": manifest.get("editorial_prompt_version"),
                "reviewer_prompt_sha256": manifest.get("editorial_prompt_sha256"),
                "reviewer_schema_version": manifest.get("editorial_schema_version"),
                "reviewer_schema_sha256": manifest.get("editorial_schema_sha256"),
                "reviewer_reasoning_effort": manifest.get("editorial_reasoning_effort"),
            }
            for field, expected_value in legacy_projection.items():
                if manifest.get(field) != expected_value:
                    raise ValueError(f"Learner rewrite legacy metadata mismatch: {field}")
            packaged_pack = _pack_from_archive(archive, manifest, review_manifest)
            rewrite_config = LearnerRewriteGateConfig(
                editorial_model=manifest["editorial_model"],
                editorial_configured_model=manifest["editorial_configured_model"],
                editorial_model_snapshot=snapshots[0],
                editorial_prompt_version=manifest["editorial_prompt_version"],
                editorial_prompt_sha256=manifest["editorial_prompt_sha256"],
                editorial_schema_version=manifest["editorial_schema_version"],
                editorial_schema_sha256=manifest["editorial_schema_sha256"],
                editorial_reasoning_effort=manifest["editorial_reasoning_effort"],
            )
            verify_learner_rewrite_gate(packaged_pack, review_manifest, rewrite_config)
        elif effective_scope == FACTUAL_REVIEW_SCOPE:
            if manifest.get("review_status") not in (None, "AI_APPROVED"):
                raise ValueError("Factual manifest has the wrong review status")
            if review_manifest.get("status") not in (None, "AI_APPROVED"):
                raise ValueError("Factual report has the wrong review status")
            manifest_source_flag = manifest.get("source_correctness_reviewed")
            if manifest_source_flag is not None and manifest_source_flag is not True:
                raise ValueError("Factual manifest has a false source-review flag")
            report_source_flag = review_manifest.get("source_correctness_reviewed")
            if report_source_flag is not None and report_source_flag is not True:
                raise ValueError("Factual report has a false source-review flag")
        else:
            raise ValueError(f"Unsupported review scope: {effective_scope}")

        contract_hash = manifest.get("pack_contract_sha256")
        review_contract_hash = review_manifest.get("pack_contract_sha256")
        if PACK_CONTRACT_FILENAME in names:
            try:
                contract = PackContract.model_validate(json.loads(archive.read(PACK_CONTRACT_FILENAME)))
                packaged_pack = PackData(
                    course=json.loads(archive.read("course.json")),
                    modules=json.loads(archive.read("modules.json")),
                    full_lessons=json.loads(archive.read("full_lessons.json")),
                    atoms=json.loads(archive.read("atoms.json")),
                    lessons=json.loads(archive.read("lessons.json")),
                    learning_pairs=json.loads(archive.read("learning_pairs.json")),
                    questions=json.loads(archive.read("questions.json")),
                    mindmaps=json.loads(archive.read("mindmaps.json")),
                    mini_labs=json.loads(archive.read("mini_labs.json")),
                    illustrations=json.loads(archive.read("illustrations.json")),
                    source_refs=json.loads(archive.read("source_refs.json")),
                    source_excerpts=json.loads(archive.read("source_excerpts.json")),
                    source_hash=review_manifest["source_hash"],
                    generator_model_snapshot="PACKAGED_VERIFICATION",
                    generator_prompt_version=manifest["prompt_version"],
                )
            except (KeyError, TypeError, ValidationError, json.JSONDecodeError) as error:
                raise ValueError(f"Invalid packaged PackContract content: {error}") from error
            expected_contract_hash = pack_contract_sha256(contract)
            if contract_hash != expected_contract_hash or review_contract_hash != expected_contract_hash:
                raise ValueError("PackContract hash is missing or stale")
            contract_validation = validate_pack(
                packaged_pack,
                contract,
                require_contract=True,
                require_frozen_outline=True,
            )
            if not contract_validation.valid:
                details = "; ".join(
                    f"{item.code}:{item.path}" for item in contract_validation.issues
                )
                raise ValueError(f"Packaged content violates PackContract: {details}")
        elif contract_hash is not None or review_contract_hash is not None:
            raise ValueError("PackContract hash exists without pack-contract.json")
        return manifest


def install_android(pack: Path, assets: Path) -> Path:
    verify_pack(pack)
    assets.mkdir(parents=True, exist_ok=True)
    destination = assets / "default.dlpack"
    temporary = destination.with_suffix(".dlpack.tmp")
    shutil.copyfile(pack, temporary)
    temporary.replace(destination)
    return destination
