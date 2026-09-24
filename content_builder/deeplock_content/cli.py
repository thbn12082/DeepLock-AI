from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Annotated

import typer

from .ai_review import run_ai_review
from .chunking import build_corpus
from .contracts import (
    PACK_CONTRACT_FILENAME,
    build_pack_contract,
    freeze_outline_assignment,
    pack_contract_sha256,
    resolve_pack_contract,
)
from .extractors import (
    ArchiveInventory,
    build_archive_inventory,
    export_ancillary,
    extract_inputs,
    extract_inventory_documents,
    export_ooxml_media,
)
from .extractors.documents import OcrRequiredError
from .generation import generate_candidate
from .models import ExtractedCorpus, PackData
from .packaging import install_android as install_pack_android
from .packaging import install_android_catalog as install_catalog_android
from .packaging import package_content, verify_pack
from .packaging.pack import ReviewGateConfig
from .settings import Settings
from .util import read_json, write_json
from .validators import validate_pack


app = typer.Typer(no_args_is_help=True, help="Build immutable DeepLock AI content packs.")


def event(status: str, **fields: object) -> None:
    typer.echo(json.dumps({"status": status, **fields}, ensure_ascii=False, sort_keys=True))


def fail(code: int, error_code: str, message: str, *, retryable: bool = False, **details: object) -> None:
    event(
        "FAILED",
        error={
            "code": error_code,
            "message": message,
            "retryable": retryable,
            "details": details,
        },
    )
    raise typer.Exit(code)


@app.command()
def doctor() -> None:
    """Verify local 9router without printing credentials."""
    settings = Settings.from_env()
    try:
        with urllib.request.urlopen(settings.base_url.removesuffix("/v1") + "/api/health", timeout=3) as response:
            health = json.loads(response.read())
        with urllib.request.urlopen(settings.base_url + "/models", timeout=3) as response:
            models = json.loads(response.read())
    except Exception as error:
        fail(4, "NINEROUTER_UNAVAILABLE", str(error), retryable=True)
    ids = {item["id"] for item in models.get("data", [])}
    required = {settings.transport_model("gpt-5.5"), settings.transport_model("gpt-5.5", reviewer=True)}
    if not settings.api_key:
        fail(2, "NINEROUTER_API_KEY_MISSING", "No active 9router API key was found; set OPENAI_API_KEY or activate a local key")
    if not health.get("ok") or not required <= ids:
        fail(2, "NINEROUTER_MODEL_MISSING", "9router does not expose required GPT-5.5 routes", required=sorted(required))
    event("READY", base_url=settings.base_url, routes=sorted(required), concurrency=settings.concurrency, credential="available")


@app.command()
def inspect(
    input: Annotated[list[Path], typer.Option("--input", exists=True, readable=True)],
    workdir: Annotated[Path, typer.Option("--workdir")],
) -> None:
    """Extract PDF/Markdown/TXT and create stable source chunks without calling GPT."""
    event("EXTRACTING", inputs=[str(item) for item in input])
    try:
        extracted = [entry for path in input for entry in extract_inputs(path)]
        corpus = build_corpus(extracted)
        write_json(workdir / "source.json", corpus.model_dump(mode="json"))
    except OcrRequiredError as error:
        fail(3, "OCR_REQUIRED", str(error))
    except Exception as error:
        fail(2, "INVALID_INPUT", str(error))
    event("READY", documents=len(corpus.documents), chunks=len(corpus.chunks), source_hash=corpus.source_hash)


@app.command("ingest-archives")
def ingest_archives(
    archive: Annotated[
        list[Path],
        typer.Option("--archive", exists=True, readable=True, help="ZIP archive containing lecture documents"),
    ],
    workdir: Annotated[Path, typer.Option("--workdir")],
    allow_visual_pages: Annotated[
        bool,
        typer.Option(
            "--allow-visual-pages/--require-text-layer",
            help="Keep low-text PDF pages for the later cached vision-audit stage",
        ),
    ] = True,
) -> None:
    """Inventory, exact-deduplicate and extract all PDF/PPTX/DOCX lecture documents."""

    event("INVENTORY", archives=[str(item) for item in archive])
    try:
        inventory = build_archive_inventory(
            archive,
            extract_dir=workdir / "documents",
        )
        write_json(workdir / "inventory.json", inventory.model_dump(mode="json"))
        extracted = extract_inventory_documents(
            inventory,
            workdir=workdir,
            require_pdf_text_layer=not allow_visual_pages,
        )
        corpus = build_corpus(extracted)
        write_json(workdir / "source.json", corpus.model_dump(mode="json"))
        media_manifest = export_ooxml_media(
            corpus.documents,
            output_dir=workdir / "ooxml-media",
        )
        write_json(
            workdir / "ooxml-media.json",
            media_manifest.model_dump(mode="json"),
        )
    except OcrRequiredError as error:
        fail(3, "OCR_REQUIRED", str(error))
    except Exception as error:
        fail(2, "ARCHIVE_INGEST_FAILED", str(error))
    event(
        "READY",
        inventory=str(workdir / "inventory.json"),
        inventory_sha256=inventory.inventory_sha256,
        document_occurrences=inventory.counts.document_occurrences,
        unique_documents=inventory.counts.unique_documents,
        exact_duplicates=inventory.counts.exact_duplicate_occurrences,
        ancillary=inventory.counts.ancillary_occurrences,
        chunks=len(corpus.chunks),
        source_hash=corpus.source_hash,
        ooxml_media_manifest=str(workdir / "ooxml-media.json"),
        ooxml_media_assets=media_manifest.counts.unique_assets,
        ooxml_media_occurrences=media_manifest.counts.media_occurrences,
    )


@app.command("export-ooxml-media")
def export_ooxml_media_command(
    workdir: Annotated[Path, typer.Option("--workdir")],
) -> None:
    """Resume OOXML media export from an existing source corpus without re-reading PDFs."""

    source_path = workdir / "source.json"
    if not source_path.is_file():
        fail(2, "SOURCE_NOT_INSPECTED", f"Missing {source_path}")
    event("EXPORTING_OOXML_MEDIA", source=str(source_path))
    try:
        corpus = ExtractedCorpus.model_validate(read_json(source_path))
        media_manifest = export_ooxml_media(
            corpus.documents,
            output_dir=workdir / "ooxml-media",
        )
        destination = workdir / "ooxml-media.json"
        write_json(destination, media_manifest.model_dump(mode="json"))
    except Exception as error:
        fail(2, "OOXML_MEDIA_EXPORT_FAILED", str(error))
    event(
        "READY",
        ooxml_media_manifest=str(destination),
        manifest_sha256=media_manifest.manifest_sha256,
        unique_assets=media_manifest.counts.unique_assets,
        occurrences=media_manifest.counts.media_occurrences,
        unreferenced_media_parts=media_manifest.counts.unreferenced_media_parts,
    )


@app.command("export-ancillary")
def export_ancillary_command(
    workdir: Annotated[Path, typer.Option("--workdir")],
) -> None:
    """Export exact-deduplicated ancillary metadata without scheduling it as study content."""

    inventory_path = workdir / "inventory.json"
    if not inventory_path.is_file():
        fail(2, "ARCHIVE_NOT_INVENTORIED", f"Missing {inventory_path}")
    event("EXPORTING_ANCILLARY", inventory=str(inventory_path))
    try:
        inventory = ArchiveInventory.model_validate(read_json(inventory_path))
        ancillary_manifest = export_ancillary(
            inventory,
            output_dir=workdir / "ancillary",
        )
        destination = workdir / "ancillary.json"
        write_json(destination, ancillary_manifest.model_dump(mode="json"))
    except Exception as error:
        fail(2, "ANCILLARY_EXPORT_FAILED", str(error))
    event(
        "READY",
        ancillary_manifest=str(destination),
        manifest_sha256=ancillary_manifest.manifest_sha256,
        occurrences=ancillary_manifest.counts.ancillary_occurrences,
        unique_assets=ancillary_manifest.counts.unique_assets,
        exact_duplicates=ancillary_manifest.counts.exact_duplicate_occurrences,
        classifications=ancillary_manifest.counts.occurrences_by_classification,
        schedulable_assets=ancillary_manifest.counts.schedulable_assets,
        quiz_payload_assets=ancillary_manifest.counts.quiz_payload_assets,
    )


@app.command("freeze-contract")
def freeze_contract_command(
    workdir: Annotated[Path, typer.Option("--workdir")],
    curriculum: Annotated[Path, typer.Option("--curriculum", exists=True, readable=True)],
    candidate: Annotated[
        Path | None,
        typer.Option(
            "--candidate",
            exists=True,
            readable=True,
            help="Optional completed candidate used to freeze outline source assignments",
        ),
    ] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Freeze an independent curriculum PackContract without calling GPT."""
    source_path = workdir / "source.json"
    if not source_path.is_file():
        fail(2, "SOURCE_NOT_INSPECTED", f"Missing {source_path}")
    destination = output or workdir / PACK_CONTRACT_FILENAME
    candidate_path = candidate
    if candidate_path is None and (workdir / "candidate.json").is_file():
        candidate_path = workdir / "candidate.json"
    event(
        "FREEZING_CONTRACT",
        curriculum=str(curriculum),
        candidate=str(candidate_path) if candidate_path else None,
    )
    try:
        source = ExtractedCorpus.model_validate(read_json(source_path))
        frozen = build_pack_contract(
            source=source,
            curriculum=read_json(curriculum),
            media_root=workdir / "media",
        )
        if candidate_path is not None:
            pack = PackData.model_validate(read_json(candidate_path))
            frozen = freeze_outline_assignment(frozen, pack)
        write_json(destination, frozen.model_dump(mode="json"))
    except Exception as error:
        fail(5, "PACK_CONTRACT_INVALID", str(error))
    event(
        "READY",
        contract=str(destination),
        contract_sha256=pack_contract_sha256(frozen),
        expected_counts=frozen.expected_counts.model_dump(mode="json"),
        outline_frozen=frozen.outline_assignment_sha256 is not None,
    )


@app.command()
def generate(
    workdir: Annotated[Path, typer.Option("--workdir")],
    model: Annotated[str, typer.Option("--model")] = "gpt-5.5",
    prompt_version: Annotated[str, typer.Option("--prompt-version")] = "course-v2",
    curriculum: Annotated[
        Path | None,
        typer.Option("--curriculum", exists=True, readable=True, help="Optional real-lecture curriculum manifest"),
    ] = None,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Generate outline/atoms/quiz/labs/map via GPT-5.5 Responses through 9router."""
    source = workdir / "source.json"
    if not source.is_file():
        fail(2, "SOURCE_NOT_INSPECTED", f"Missing {source}")
    if force:
        resume = False
    event(
        "GENERATING",
        model=model,
        route="9router",
        concurrency=Settings.from_env().concurrency,
        curriculum=str(curriculum) if curriculum else None,
    )
    try:
        corpus = ExtractedCorpus.model_validate(read_json(source))
        pack = generate_candidate(
            corpus=corpus,
            workdir=workdir,
            settings=Settings.from_env(),
            model=model,
            prompt_version=prompt_version,
            resume=resume,
            curriculum=read_json(curriculum) if curriculum else None,
        )
    except ValueError as error:
        fail(5, "GENERATION_SCHEMA_INVALID", str(error), retryable=True)
    except Exception as error:
        fail(4, "OPENAI_REQUEST_FAILED", str(error), retryable=True)
    event("READY", atoms=len(pack.atoms), questions=len(pack.questions), candidate=str(workdir / "candidate.json"))


@app.command()
def validate(
    workdir: Annotated[Path, typer.Option("--workdir")],
    report: Annotated[Path, typer.Option("--report")],
    contract: Annotated[Path | None, typer.Option("--contract", exists=True, readable=True)] = None,
    allow_legacy_no_contract: Annotated[
        bool,
        typer.Option("--allow-legacy-no-contract", help="Explicitly validate a legacy non-curriculum candidate"),
    ] = False,
) -> None:
    """Run deterministic schema, grounding, pair, graph and Mini Lab validators."""
    candidate = workdir / "candidate.json"
    if not candidate.is_file():
        fail(2, "CANDIDATE_MISSING", f"Missing {candidate}")
    event("VALIDATING")
    try:
        pack = PackData.model_validate(read_json(candidate))
        pack_contract = resolve_pack_contract(
            explicit_path=contract,
            workdir=workdir,
            candidate_path=candidate,
            required=not allow_legacy_no_contract,
        )
        result = validate_pack(
            pack,
            pack_contract,
            require_contract=not allow_legacy_no_contract,
            require_frozen_outline=pack_contract is not None,
        )
        write_json(report, result.model_dump(mode="json"))
    except Exception as error:
        fail(5, "VALIDATION_FAILED", str(error))
    if not result.valid:
        fail(5, "VALIDATION_FAILED", "Deterministic content validation failed", issues=[item.model_dump() for item in result.issues])
    event("READY", report=str(report), stats=result.stats)


@app.command("ai-review")
def ai_review(
    workdir: Annotated[Path, typer.Option("--workdir")],
    model: Annotated[str, typer.Option("--model")] = "gpt-5.5",
    prompt_version: Annotated[str, typer.Option("--prompt-version")] = "reviewer-v2",
    max_repair_rounds: Annotated[int, typer.Option("--max-repair-rounds", min=0, max=2)] = 2,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    report: Annotated[Path, typer.Option("--report")] = Path(".build/reports/ai-review.json"),
    contract: Annotated[Path | None, typer.Option("--contract", exists=True, readable=True)] = None,
    allow_legacy_no_contract: Annotated[
        bool,
        typer.Option("--allow-legacy-no-contract", help="Explicitly review a legacy non-curriculum candidate"),
    ] = False,
) -> None:
    """Run fresh-context independent GPT-5.5 review and materialize AI_APPROVED."""
    candidate = workdir / "candidate.json"
    if not candidate.is_file():
        fail(2, "CANDIDATE_MISSING", f"Missing {candidate}")
    event("VALIDATING", gate="AI_REVIEW")
    try:
        pack = PackData.model_validate(read_json(candidate))
        pack_contract = resolve_pack_contract(
            explicit_path=contract,
            workdir=workdir,
            candidate_path=candidate,
            required=not allow_legacy_no_contract,
        )
        deterministic = validate_pack(
            pack,
            pack_contract,
            require_contract=not allow_legacy_no_contract,
            require_frozen_outline=pack_contract is not None,
        )
        if not deterministic.valid:
            raise ValueError("Deterministic validation must pass before AI review")
        # A failed fresh attempt must not leave a previously approved decision
        # file available to a later package command.
        write_json(
            workdir / "ai-review.json",
            {
                "status": "AI_REVIEW_IN_PROGRESS",
                "candidate_hash": "INVALID_UNTIL_CURRENT_REVIEW_COMPLETES",
            },
        )
        updated, result = run_ai_review(
            pack=pack,
            workdir=workdir,
            settings=Settings.from_env(),
            model=model,
            prompt_version=prompt_version,
            max_repair_rounds=max_repair_rounds,
            resume=resume,
            contract=pack_contract,
            require_contract=not allow_legacy_no_contract,
            require_frozen_outline=pack_contract is not None,
        )
        write_json(candidate, updated.model_dump(mode="json", by_alias=True))
        write_json(report, result)
        write_json(workdir / "ai-review.json", result)
    except PermissionError as error:
        fail(8, "AI_REVIEW_REQUIRED", str(error))
    except ValueError as error:
        fail(5, "VALIDATION_FAILED", str(error))
    except Exception as error:
        fail(4, "OPENAI_REVIEW_FAILED", str(error), retryable=True)
    event("READY", gate="AI_APPROVED", approved_items=result["approved_items"], report=str(report))


@app.command()
def package(
    workdir: Annotated[Path, typer.Option("--workdir")],
    output: Annotated[Path, typer.Option("--output")],
    contract: Annotated[Path | None, typer.Option("--contract", exists=True, readable=True)] = None,
    allow_legacy_no_contract: Annotated[
        bool,
        typer.Option("--allow-legacy-no-contract", help="Explicitly package a legacy non-curriculum candidate"),
    ] = False,
) -> None:
    """Package only a current, deterministic-valid, AI-approved candidate."""
    event("PACKAGING")
    try:
        candidate = workdir / "candidate.json"
        pack = PackData.model_validate(read_json(candidate))
        pack_contract = resolve_pack_contract(
            explicit_path=contract,
            workdir=workdir,
            candidate_path=candidate,
            required=not allow_legacy_no_contract,
        )
        validation = validate_pack(
            pack,
            pack_contract,
            require_contract=not allow_legacy_no_contract,
            require_frozen_outline=pack_contract is not None,
        )
        if not validation.valid:
            raise ValueError("Candidate no longer passes deterministic validation")
        review_path = workdir / "ai-review.json"
        try:
            review = read_json(review_path)
        except Exception as error:
            raise PermissionError(f"AI_REVIEW_REQUIRED: missing or unreadable {review_path}: {error}") from error
        settings = Settings.from_env()
        configured_model = settings.transport_model(settings.review_model, reviewer=True)
        expected_snapshot = os.getenv("OPENAI_REVIEW_MODEL_SNAPSHOT") or configured_model
        path = package_content(
            pack,
            review,
            output,
            ReviewGateConfig(
                reviewer_model=settings.review_model,
                reviewer_configured_model=configured_model,
                reviewer_model_snapshot=expected_snapshot,
                reviewer_prompt_version=settings.review_prompt_version,
                reviewer_reasoning_effort=settings.review_reasoning_effort,
            ),
            media_root=workdir / "media",
            contract=pack_contract,
            require_contract=not allow_legacy_no_contract,
            require_frozen_outline=pack_contract is not None,
        )
    except PermissionError as error:
        fail(8, "AI_REVIEW_REQUIRED", str(error))
    except Exception as error:
        fail(7, "PACKAGE_FAILED", str(error))
    event("READY", output=str(path), manifest=verify_pack(path))


@app.command("install-android")
def install_android(
    pack: Annotated[Path, typer.Option("--pack", exists=True, readable=True)],
    android_assets: Annotated[Path, typer.Option("--android-assets")],
) -> None:
    """Copy a verified AI-approved pack into Android assets."""
    try:
        destination = install_pack_android(pack, android_assets)
    except Exception as error:
        fail(7, "INSTALL_ANDROID_FAILED", str(error))
    event("READY", destination=str(destination))


@app.command("install-android-catalog")
def install_android_catalog(
    catalog: Annotated[
        Path,
        typer.Option("--catalog", exists=True, readable=True, help="Finalized catalog.json"),
    ],
    packaged_dir: Annotated[
        Path,
        typer.Option(
            "--packaged-dir",
            exists=True,
            file_okay=False,
            readable=True,
            help="Directory containing exactly one <lecture_id>.dlpack per catalog lecture",
        ),
    ],
    android_assets: Annotated[Path, typer.Option("--android-assets")],
) -> None:
    """Transactionally install every verified catalog pack into Android assets."""

    try:
        report = install_catalog_android(
            catalog_path=catalog,
            packaged_dir=packaged_dir,
            android_assets=android_assets,
        )
    except Exception as error:
        fail(7, "INSTALL_ANDROID_CATALOG_FAILED", str(error))
    event("READY", **report)


if __name__ == "__main__":
    app()
