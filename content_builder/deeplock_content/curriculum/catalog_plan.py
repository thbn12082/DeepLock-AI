from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..extractors.ooxml_media import (
    OoxmlMediaAsset,
    OoxmlMediaManifest,
    OoxmlMediaOccurrence,
    ooxml_media_manifest_sha256,
)
from ..models import ExtractedCorpus
from ..util import normalize_text, read_json, sha256_bytes, sha256_json, stable_id, write_json
from ..visuals import render_pdf_visuals


@dataclass(frozen=True)
class CategorySpec:
    category_id: str
    title: str
    description: str
    order_index: int
    search_terms: tuple[str, ...] = ()


_FULL_CATEGORIES: tuple[tuple[str, CategorySpec], ...] = (
    ("0.", CategorySpec("cat_skills", "Kỹ năng", "Kỹ năng học tập, code, Git và giải quyết vấn đề.", 0)),
    ("1.", CategorySpec("cat_python", "Python", "Lập trình Python từ nền tảng đến cấu trúc dữ liệu.", 1)),
    ("2.", CategorySpec("cat_database", "Cơ sở dữ liệu", "SQL, NoSQL, MongoDB, Firebase và Vector DB.", 2)),
    ("3.", CategorySpec("cat_math", "Toán cho AI", "Đại số, xác suất và thống kê cho AI.", 3)),
    ("4.", CategorySpec("cat_data_libraries", "Thư viện dữ liệu", "NumPy, Pandas và trực quan hóa dữ liệu.", 4)),
    ("5.", CategorySpec("cat_machine_learning", "Machine Learning", "Toàn bộ bài giảng Machine Learning.", 5, ("ML",))),
    ("6.", CategorySpec("cat_deep_learning", "Deep Learning", "Toàn bộ bài giảng Deep Learning.", 6, ("DL",))),
    ("7.", CategorySpec("cat_generative_ai", "Generative AI", "LLM, RAG, Agent và Generative AI.", 7, ("GenAI", "LLM", "RAG", "Agent"))),
    ("extra 1.", CategorySpec("cat_data_analysis", "Data Analysis", "Phân tích dữ liệu và hệ sinh thái dữ liệu.", 8)),
    ("extra 2.", CategorySpec("cat_operating_system", "Hệ điều hành", "Kiến thức hệ điều hành.", 9)),
    ("extra 3.", CategorySpec("cat_mlops", "MLOps", "DVC, serving, MLflow, Airflow, monitoring, CI/CD, Kubernetes và tối ưu triển khai.", 10, ("LLMOps", "MLflow", "Kubeflow", "DVC"))),
    ("extra 4.", CategorySpec("cat_cloud_aws", "Cloud & AWS", "Cloud, AWS và hạ tầng triển khai.", 11, ("AWS", "Cloud"))),
    ("extra 5.", CategorySpec("cat_xai", "Explainable AI", "XAI và diễn giải mô hình.", 12, ("XAI",))),
    ("extra 6.", CategorySpec("cat_recommender", "Recommender System", "Hệ gợi ý.", 13)),
    ("nguồn - internet", CategorySpec("cat_internet", "Nguồn Internet", "Tài liệu nguồn Internet.", 14)),
    ("seminar", CategorySpec("cat_seminar", "Seminar", "Seminar và chia sẻ kinh nghiệm.", 15)),
)
_OVERVIEW = CategorySpec("cat_overview", "Tổng quan", "Sơ đồ và hệ thống kiến thức tổng quan.", 16)
_AIVIN = CategorySpec(
    "cat_aivin_ai20k",
    "Aivin AI20K · 28 ngày",
    "Toàn bộ lecture, handbook và lab của khóa Aivin AI20K.",
    17,
    ("Aivin", "AI20K", "LLMOps", "AI Product", "MCP", "A2A", "FinOps"),
)


def _logical_path(document: Any) -> str:
    return str(getattr(document, "logical_path", None) or document.filename).replace("\\", "/")


def category_for_document(document: Any) -> CategorySpec:
    logical_path = _logical_path(document)
    lowered = logical_path.casefold()
    if "aivin.zip/" in lowered or "/aivin/" in lowered:
        return _AIVIN
    parts = [part.strip() for part in logical_path.split("/") if part.strip()]
    root_index = next(
        (index for index, part in enumerate(parts) if part.casefold().startswith("aio - sao ch")),
        -1,
    )
    group = parts[root_index + 1] if 0 <= root_index < len(parts) - 1 else ""
    folded = group.casefold()
    for prefix, category in _FULL_CATEGORIES:
        if folded.startswith(prefix):
            return category
    return _OVERVIEW


def _display_name(document: Any) -> str:
    raw = str(getattr(document, "display_name", None) or Path(_logical_path(document)).name)
    return re.sub(r"\.(?:pdf|pptx|docx|md|markdown|txt)$", "", raw, flags=re.IGNORECASE).strip()


def _search_terms(document: Any, category: CategorySpec) -> list[str]:
    path = _logical_path(document)
    terms = [*category.search_terms]
    terms.extend(
        re.sub(r"^\d+(?:\.\d+)*\s*[-_.]?\s*", "", part).strip()
        for part in path.split("/")
        if part.strip()
    )
    lowered = path.casefold()
    tag_rules = {
        "MLOps": ("mlops", "mlflow", "airflow", "kubeflow", "prometheus", "grafana", "serving", "cicd", "ci cd", "finops", "observability"),
        "LLMOps": ("llmops", "prompt-versioning"),
        "RAG": ("rag", "retrieval"),
        "AI Agent": ("agent", "mcp", "a2a", "langgraph", "react"),
        "AI Product": ("ai-product", "ai product", "product thinking", "prototype"),
        "Cloud": ("cloud", "aws", "kubernetes"),
    }
    for tag, needles in tag_rules.items():
        if any(needle in lowered for needle in needles):
            terms.append(tag)
    return list(dict.fromkeys(term for term in terms if term))


def _filtered_corpus(corpus: ExtractedCorpus, document_id: str) -> ExtractedCorpus:
    documents = [item for item in corpus.documents if item.document_id == document_id]
    if len(documents) != 1:
        raise ValueError(f"Expected exactly one unique document {document_id}")
    chunks = [item for item in corpus.chunks if item.document_id == document_id]
    return ExtractedCorpus(
        documents=documents,
        chunks=chunks,
        source_hash=sha256_json([item.model_dump(mode="json") for item in chunks]),
    )


def _discover_ooxml_media_manifest(
    corpus: ExtractedCorpus,
    requested: Path | None,
) -> Path | None:
    if requested is not None:
        if not requested.is_file():
            raise ValueError(f"OOXML media manifest is unavailable: {requested}")
        return requested.resolve()
    candidates = {
        Path(document.source_path).resolve().parent.parent / "ooxml-media.json"
        for document in corpus.documents
        if document.source_format in {"PPTX", "DOCX"} and document.source_path
    }
    existing = sorted(path for path in candidates if path.is_file())
    if len(existing) > 1:
        raise ValueError(
            "OOXML sources resolve to multiple media manifests; pass ooxml_media_manifest explicitly"
        )
    return existing[0] if existing else None


def _load_ooxml_media_manifest(
    corpus: ExtractedCorpus,
    requested: Path | None,
) -> tuple[OoxmlMediaManifest | None, Path | None]:
    path = _discover_ooxml_media_manifest(corpus, requested)
    if path is None:
        return None, None
    manifest = OoxmlMediaManifest.model_validate(read_json(path))
    if manifest.manifest_sha256 != ooxml_media_manifest_sha256(manifest):
        raise ValueError(f"OOXML media manifest hash mismatch: {path}")
    known_document_ids = {document.document_id for document in corpus.documents}
    unknown = {
        occurrence.document_id
        for occurrence in manifest.occurrences
        if occurrence.document_id not in known_document_ids
    }
    if unknown:
        raise ValueError(f"OOXML media manifest references unknown documents: {sorted(unknown)}")
    return manifest, path.parent.resolve()


def _ooxml_asset_path(asset: OoxmlMediaAsset, manifest_root: Path) -> Path:
    unresolved = manifest_root / Path(asset.extracted_path)
    if unresolved.is_symlink():
        raise ValueError(f"OOXML media asset may not be a symlink: {unresolved}")
    source = unresolved.resolve()
    try:
        source.relative_to(manifest_root)
    except ValueError as error:
        raise ValueError(
            f"OOXML media asset escapes its manifest root: {asset.extracted_path}"
        ) from error
    if not source.is_file():
        raise ValueError(f"OOXML media asset is unavailable: {source}")
    payload = source.read_bytes()
    if len(payload) != asset.byte_size or sha256_bytes(payload) != asset.sha256:
        raise ValueError(f"OOXML media asset hash mismatch: {source}")
    return source


def _png_payload(payload: bytes) -> bytes:
    if len(payload) >= 24 and payload[:8] == b"\x89PNG\r\n\x1a\n" and payload[12:16] == b"IHDR":
        return payload
    import pymupdf

    try:
        pixmap = pymupdf.Pixmap(payload)
        if pixmap.colorspace is None:
            raise ValueError("image has no raster colorspace")
        if pixmap.n - pixmap.alpha > 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        converted = pixmap.tobytes("png")
    except Exception as error:
        raise ValueError(f"OOXML media cannot be deterministically converted to PNG: {error}") from error
    if len(converted) < 24 or converted[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("OOXML media conversion did not produce PNG")
    return converted


def _write_occurrence_asset(
    *,
    source: Path,
    destination: Path,
) -> None:
    payload = _png_payload(source.read_bytes())
    if destination.is_symlink():
        raise ValueError(f"OOXML workspace media may not be a symlink: {destination}")
    if destination.is_file() and destination.read_bytes() == payload:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"OOXML workspace media temporary path exists: {temporary}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()


def _bounded_text(value: str, *, limit: int = 500) -> str:
    cleaned = normalize_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _ooxml_caption(
    occurrence: OoxmlMediaOccurrence,
    *,
    document_title: str,
) -> tuple[str, str]:
    page_label = "slide" if occurrence.source_format == "PPTX" else "phần"
    context = occurrence.section_title or document_title
    fallback = (
        f"Minh họa từ {document_title}, {page_label} {occurrence.page_number}: {context}."
    )
    caption = _bounded_text(occurrence.alt_text or fallback)
    alt_text = _bounded_text(
        occurrence.alt_text
        or f"{caption} Nguồn: {document_title}, {page_label} {occurrence.page_number}."
    )
    return caption, alt_text


def _materialize_ooxml_illustrations(
    *,
    document: Any,
    title: str,
    source: ExtractedCorpus,
    media_dir: Path,
    manifest: OoxmlMediaManifest | None,
    manifest_root: Path | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[int]]:
    if manifest is None or manifest_root is None:
        return [], [], []
    assets = {item.media_id: item for item in manifest.assets}
    occurrences = sorted(
        (
            item
            for item in manifest.occurrences
            if item.document_id == document.document_id and item.page_number is not None
        ),
        key=lambda item: (item.page_number or 0, item.occurrence_id),
    )
    text_length_by_page: dict[int, int] = {}
    for chunk in source.chunks:
        text_length_by_page[chunk.page_start] = (
            text_length_by_page.get(chunk.page_start, 0) + len(chunk.normalized_text)
        )
    illustrations: list[dict[str, object]] = []
    audit_occurrences: list[dict[str, object]] = []
    image_only_pages: set[int] = set()
    for occurrence in occurrences:
        page_number = int(occurrence.page_number or 0)
        if not 1 <= page_number <= document.page_count:
            raise ValueError(
                f"OOXML media page is outside its document: {document.document_id} page {page_number}"
            )
        asset = assets.get(occurrence.media_id)
        if asset is None:
            raise ValueError(f"OOXML occurrence references missing asset: {occurrence.occurrence_id}")
        source_asset = _ooxml_asset_path(asset, manifest_root)
        asset_member = f"illustration_ooxml_{occurrence.occurrence_id}.png"
        _write_occurrence_asset(
            source=source_asset,
            destination=media_dir / asset_member,
        )
        caption, alt_text = _ooxml_caption(occurrence, document_title=title)
        visual: dict[str, object] = {
            "page": page_number,
            "asset_member": asset_member,
            "caption": caption,
            "alt_text": alt_text,
            "occurrence_id": occurrence.occurrence_id,
            "ooxml_media_id": occurrence.media_id,
            "ooxml_media_sha256": asset.sha256,
            "mapping_kind": occurrence.mapping_kind,
            "package_part": occurrence.package_part,
            "relationship_source_part": occurrence.relationship_source_part,
            "relationship_id": occurrence.relationship_id,
        }
        illustrations.append(visual)
        reasons: list[str] = []
        page_text_length = text_length_by_page.get(page_number, 0)
        if page_text_length == 0:
            reasons.append("IMAGE_ONLY_PAGE")
            image_only_pages.add(page_number)
        elif page_text_length < 40:
            reasons.append("LOW_TEXT_PAGE")
        if not occurrence.alt_text:
            reasons.append("MISSING_ALT_TEXT")
        if reasons:
            audit_occurrences.append({
                **visual,
                "reasons": reasons,
                "page_text_length": page_text_length,
            })
    return illustrations, audit_occurrences, sorted(image_only_pages)


def plan_lecture_packs(
    *,
    corpus: ExtractedCorpus,
    output_root: Path,
    render_visual_assets: bool = True,
    pages_per_part: int = 20,
    target_pages_per_atom: int = 2,
    max_atoms_per_part: int = 10,
    ooxml_media_manifest: Path | None = None,
) -> dict[str, Any]:
    """Create one resumable build workspace for every exact-byte-unique lecture."""

    output_root.mkdir(parents=True, exist_ok=True)
    media_manifest, media_manifest_root = _load_ooxml_media_manifest(
        corpus,
        ooxml_media_manifest,
    )
    category_map: dict[str, CategorySpec] = {}
    lectures: list[dict[str, Any]] = []
    for global_index, document in enumerate(corpus.documents):
        category = category_for_document(document)
        category_map[category.category_id] = category
        lecture_id = stable_id("lec", document.document_id, length=20)
        planned_pack_id = stable_id("planned_pack", document.document_id, length=20)
        workspace = output_root / "work" / lecture_id
        media = workspace / "media"
        source = _filtered_corpus(corpus, document.document_id)
        write_json(workspace / "source.json", source.model_dump(mode="json"))
        title = _display_name(document)
        illustrations: list[dict[str, object]] = []
        source_path = getattr(document, "source_path", None)
        source_format = str(getattr(document, "source_format", "")).upper()
        is_pdf = bool(
            source_path
            and Path(source_path).is_file()
            and (source_format == "PDF" or Path(source_path).suffix.casefold() == ".pdf")
        )
        if (
            render_visual_assets
            and is_pdf
        ):
            illustrations = [
                item.curriculum_spec()
                for item in render_pdf_visuals(
                    Path(source_path),
                    output_dir=media,
                    asset_prefix=lecture_id,
                    document_title=title,
                    pages_per_part=pages_per_part,
                )
            ]
        ooxml_audit_occurrences: list[dict[str, object]] = []
        ooxml_image_only_pages: list[int] = []
        if render_visual_assets and source_format in {"PPTX", "DOCX"}:
            (
                ooxml_illustrations,
                ooxml_audit_occurrences,
                ooxml_image_only_pages,
            ) = _materialize_ooxml_illustrations(
                document=document,
                title=title,
                source=source,
                media_dir=media,
                manifest=media_manifest,
                manifest_root=media_manifest_root,
            )
            illustrations.extend(ooxml_illustrations)
        pages_with_text = {chunk.page_start for chunk in source.chunks}
        text_length_by_page: dict[int, int] = {}
        for chunk in source.chunks:
            text_length_by_page[chunk.page_start] = (
                text_length_by_page.get(chunk.page_start, 0) + len(chunk.normalized_text)
            )
        visual_audit_pages = [
            page
            for page in range(1, document.page_count + 1)
            if is_pdf and (page not in pages_with_text or text_length_by_page.get(page, 0) < 40)
        ]
        module_id = stable_id("lecture", document.document_id, length=20)
        curriculum = {
            "course_id": stable_id("course", document.document_id, length=20),
            "title": title,
            "description": f"Bài giảng nguyên bản: {_logical_path(document)}",
            "stable_content_id_seed": document.sha256,
            "outline_mode": "SEMANTIC_V2",
            "content_identity_namespace": "v3",
            "batch_atoms_by_module": True,
            "pages_per_part": pages_per_part,
            "target_pages_per_atom": target_pages_per_atom,
            "max_atoms_per_part": max_atoms_per_part,
            "allowed_mini_lab_atom_ids": [],
            "allowed_mini_lab_types": {},
            "modules": [{
                "module_id": module_id,
                "title": title,
                "output": document.filename,
                "illustrations": illustrations,
                "visual_audit_required_pages": visual_audit_pages,
                "ooxml_visual_audit_occurrences": ooxml_audit_occurrences,
            }],
        }
        write_json(workspace / "curriculum.json", curriculum)
        aliases = list(getattr(document, "source_aliases", []) or [])
        lectures.append({
            "lecture_id": lecture_id,
            "planned_pack_id": planned_pack_id,
            "category_id": category.category_id,
            "title": title,
            "description": f"Nguồn: {_logical_path(document)}",
            "order_index": global_index,
            "search_terms": _search_terms(document, category),
            "document_id": document.document_id,
            "document_sha256": document.sha256,
            "logical_path": _logical_path(document),
            "source_aliases": aliases,
            "workspace": str(workspace.resolve()),
            "visual_audit_required_pages": visual_audit_pages,
            "ooxml_media_occurrences": len([
                item for item in illustrations if item.get("occurrence_id")
            ]),
            "ooxml_visual_audit_occurrences": ooxml_audit_occurrences,
            "ooxml_image_only_pages": ooxml_image_only_pages,
        })
    category_order = {item.category_id: item.order_index for item in category_map.values()}
    lectures.sort(key=lambda item: (category_order[item["category_id"]], item["order_index"]))
    next_lecture_order: dict[str, int] = {}
    for lecture in lectures:
        local_order = next_lecture_order.get(lecture["category_id"], 0)
        lecture["order_index"] = local_order
        next_lecture_order[lecture["category_id"]] = local_order + 1
    categories = [
        {
            "category_id": item.category_id,
            "title": item.title,
            "description": item.description,
            "order_index": item.order_index,
            "search_terms": list(item.search_terms),
        }
        for item in sorted(category_map.values(), key=lambda value: value.order_index)
    ]
    occurrence_count = sum(max(1, len(item["source_aliases"])) for item in lectures)
    plan = {
        "schema_version": "1.0",
        "plan_id": stable_id(
            "catalog_plan",
            corpus.source_hash,
            media_manifest.manifest_sha256 if media_manifest else "no-ooxml-media",
            length=24,
        ),
        "source_hash": corpus.source_hash,
        "ooxml_media_manifest_sha256": (
            media_manifest.manifest_sha256 if media_manifest else None
        ),
        "categories": categories,
        "lectures": lectures,
        "counts": {
            "categories": len(categories),
            "unique_lectures": len(lectures),
            "document_occurrences": occurrence_count,
            "exact_duplicate_occurrences": occurrence_count - len(lectures),
            "visual_audit_pages": sum(len(item["visual_audit_required_pages"]) for item in lectures),
            "ooxml_media_occurrences": sum(item["ooxml_media_occurrences"] for item in lectures),
            "ooxml_visual_audit_occurrences": sum(
                len(item["ooxml_visual_audit_occurrences"]) for item in lectures
            ),
            "ooxml_image_only_pages": sum(
                len(item["ooxml_image_only_pages"]) for item in lectures
            ),
        },
    }
    write_json(output_root / "catalog-plan.json", plan)
    return plan


def _read_pack_members(
    path: Path,
    *,
    verified_manifest: dict[str, Any],
) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest != verified_manifest:
            raise ValueError(f"Pack manifest changed after verification: {path}")
        values = {
            name: json.loads(archive.read(name))
            for name in (
                "course.json",
                "modules.json",
                "atoms.json",
                "lessons.json",
                "learning_pairs.json",
                "questions.json",
            )
        }
    return values


def finalize_android_catalog(
    *,
    plan_path: Path,
    packaged_dir: Path,
    output: Path,
    asset_prefix: str = "content/packs",
    default_category_id: str = "cat_deep_learning",
    default_lecture_id: str | None = None,
) -> dict[str, Any]:
    # Lazy import avoids the generation -> curriculum -> packaging -> review ->
    # generation cycle during ordinary builder module import.
    from ..packaging import verify_pack

    plan = read_json(plan_path)
    lecture_by_id = {item["lecture_id"]: item for item in plan["lectures"]}
    if len(lecture_by_id) != len(plan["lectures"]):
        raise ValueError("Catalog plan contains duplicate lecture IDs")
    pack_by_lecture_id = {path.stem: path for path in packaged_dir.glob("*.dlpack")}
    if len(pack_by_lecture_id) != len(lecture_by_id):
        raise ValueError(
            f"Pack coverage mismatch: expected {len(lecture_by_id)}, found {len(pack_by_lecture_id)}"
        )
    descriptors: list[dict[str, Any]] = []
    atoms: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_pack_ids: set[str] = set()
    global_order = 0
    category_order = {item["category_id"]: item["order_index"] for item in plan["categories"]}
    ordered_lectures = sorted(
        plan["lectures"],
        key=lambda item: (category_order[item["category_id"]], item["order_index"]),
    )
    for lecture in ordered_lectures:
        lecture_id = lecture["lecture_id"]
        path = pack_by_lecture_id.get(lecture_id)
        if path is None:
            raise ValueError(f"Missing pack for planned lecture: {lecture_id}")
        # Never derive a catalog from a ZIP that merely looks like a pack.
        # verify_pack authenticates the full member set, every member digest,
        # the AI-review gate and the frozen PackContract before we read data.
        preverify_pack_sha256 = sha256_bytes(path.read_bytes())
        manifest = verify_pack(path)
        verified_pack_sha256 = sha256_bytes(path.read_bytes())
        if preverify_pack_sha256 != verified_pack_sha256:
            raise ValueError(f"Pack changed while being verified: {path}")
        members = _read_pack_members(path, verified_manifest=manifest)
        if sha256_bytes(path.read_bytes()) != verified_pack_sha256:
            raise ValueError(f"Pack changed while finalizing Android catalog: {path}")
        pack_id = str(manifest["content_pack_id"])
        if pack_id in seen_pack_ids:
            raise ValueError(f"Duplicate content_pack_id across catalog packs: {pack_id}")
        seen_pack_ids.add(pack_id)
        course = members["course.json"]
        if str(manifest["course_id"]) != str(course.get("course_id") or ""):
            raise ValueError(f"Manifest/course mismatch in verified pack: {path}")
        descriptors.append({
            "pack_id": pack_id,
            "lecture_id": lecture_id,
            "asset_path": f"{asset_prefix.rstrip('/')}/{path.name}",
            "title": lecture["title"],
            "order_index": 0,
            "course_id": manifest["course_id"],
            "asset_sha256": verified_pack_sha256,
        })
        module_order = {item["module_id"]: item["order_index"] for item in members["modules.json"]}
        lesson_by_atom = {item["atom_id"]: item for item in members["lessons.json"]}
        pair_by_atom = {item["atom_id"]: item for item in members["learning_pairs.json"]}
        for atom in sorted(
            members["atoms.json"],
            key=lambda item: (module_order[item["module_id"]], item["order_index"]),
        ):
            pair = pair_by_atom[atom["atom_id"]]
            lesson = lesson_by_atom[atom["atom_id"]]
            record = {
                "pack_id": pack_id,
                "atom_id": atom["atom_id"],
                "pair_id": pair["pair_id"],
                "lesson_id": lesson["lesson_id"],
                "module_id": atom["module_id"],
                "title": atom["title"],
                "order_index": global_order,
                "prerequisite_ids": atom["prerequisite_ids"],
                "question_ids": pair["question_ids"],
            }
            identities = {
                record["atom_id"], record["pair_id"], record["lesson_id"], *record["question_ids"]
            }
            duplicates = identities & seen_ids
            if duplicates:
                raise ValueError(f"Global content IDs are not unique: {sorted(duplicates)}")
            seen_ids.update(identities)
            atoms.append(record)
            global_order += 1
    descriptor_order = {item["lecture_id"]: index for index, item in enumerate(ordered_lectures)}
    descriptors.sort(key=lambda item: descriptor_order[item["lecture_id"]])
    if default_lecture_id is not None:
        if default_lecture_id not in lecture_by_id:
            raise ValueError(f"Unknown default lecture ID: {default_lecture_id}")
        default_descriptor = next(
            item for item in descriptors if item["lecture_id"] == default_lecture_id
        )
    else:
        default_descriptor = next(
            (
                item
                for item in descriptors
                if lecture_by_id[item["lecture_id"]]["category_id"] == default_category_id
            ),
            descriptors[0],
        )
    catalog = {
        "schema_version": "1.0",
        "catalog_id": stable_id(
            "catalog",
            plan["source_hash"],
            sha256_json([item["asset_sha256"] for item in descriptors]),
            length=24,
        ),
        "title": "Toàn bộ Lecture",
        "language": "vi",
        "default_pack_id": default_descriptor["pack_id"],
        "categories": plan["categories"],
        "lectures": [
            {
                key: item[key]
                for key in (
                    "lecture_id", "category_id", "title", "description", "order_index", "search_terms"
                )
            }
            for item in ordered_lectures
        ],
        "packs": descriptors,
        "atoms": atoms,
    }
    write_json(output, catalog)
    return catalog
