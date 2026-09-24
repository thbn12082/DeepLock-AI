from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .util import sha256_bytes


_NON_CONTENT_RE = re.compile(
    r"\b(?:thank\s*you|quiz\s*time|questions?|mục\s*lục|agenda|table\s+of\s+contents)\b",
    re.IGNORECASE,
)
_VISUAL_HINT_RE = re.compile(
    r"\b(?:figure|fig\.?|diagram|architecture|pipeline|workflow|flow|chart|graph|"
    r"dashboard|matrix|hình|sơ\s*đồ|kiến\s*trúc|quy\s*trình|biểu\s*đồ)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RenderedVisual:
    page_number: int
    asset_member: str
    caption: str
    byte_size: int
    sha256: str
    width: int
    height: int
    score: float

    def curriculum_spec(self) -> dict[str, object]:
        return {
            "page": self.page_number,
            "asset_member": self.asset_member,
            "caption": self.caption,
        }


def _page_visual_score(page: object) -> float:
    try:
        text = page.get_text("text").strip()
    except Exception:
        text = ""
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    image_area = 0.0
    try:
        image_info = page.get_image_info()
    except Exception:
        image_info = []
    for info in image_info:
        bbox = info.get("bbox")
        if bbox is None:
            continue
        image_area += max(0.0, float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])))
    image_ratio = min(1.0, image_area / page_area)
    try:
        drawings = len(page.get_drawings())
    except Exception:
        drawings = 0
    try:
        images = len(page.get_images(full=True))
    except Exception:
        images = len(image_info)
    score = image_ratio * 75.0 + math.log1p(drawings) * 7.0 + min(images, 8) * 4.0
    if _VISUAL_HINT_RE.search(text):
        score += 12.0
    if _NON_CONTENT_RE.search(text) and len(text) < 260:
        # Closing/agenda/quiz-divider slides are still preserved as source
        # text, but are not useful as the one illustrative image for a part.
        return -math.inf
    if len(text) < 10 and image_ratio < 0.05 and drawings < 3:
        score -= 30.0
    return score


def select_pdf_visual_pages(
    path: Path,
    *,
    pages_per_part: int = 24,
    minimum_score: float = 9.0,
) -> list[tuple[int, float]]:
    """Pick at most one useful visual page per learning part."""

    import pymupdf

    if not 1 <= pages_per_part <= 48:
        raise ValueError("pages_per_part must be between 1 and 48")
    selected: list[tuple[int, float]] = []
    # Some source decks contain malformed optional xref objects. MuPDF can
    # still read/render their valid pages, but otherwise emits one diagnostic
    # per missing object and can flood a multi-document build log.
    previous_error_display = bool(pymupdf.TOOLS.mupdf_display_errors())
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        with pymupdf.open(path) as document:
            for start in range(0, len(document), pages_per_part):
                candidates = []
                for index in range(start, min(len(document), start + pages_per_part)):
                    try:
                        score = _page_visual_score(document[index])
                    except Exception:
                        continue
                    candidates.append((score, index))
                score, index = max(candidates, default=(-math.inf, -1))
                if index >= 0 and score >= minimum_score:
                    selected.append((index + 1, round(score, 4)))
    finally:
        pymupdf.TOOLS.mupdf_display_errors(previous_error_display)
    return selected


def render_pdf_visuals(
    path: Path,
    *,
    output_dir: Path,
    asset_prefix: str,
    document_title: str,
    pages_per_part: int = 24,
    target_width: int = 960,
) -> list[RenderedVisual]:
    """Render selected lecture pages as bounded PNG assets for offline Android use."""

    import pymupdf

    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", asset_prefix).strip("._-")[:80]
    if not safe_prefix:
        raise ValueError("asset_prefix must contain a safe character")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        selected = select_pdf_visual_pages(path, pages_per_part=pages_per_part)
    except Exception:
        # One malformed source must not abort planning for the remaining
        # hundreds of lectures. Its text/vision-audit path remains available.
        return []
    result: list[RenderedVisual] = []
    previous_error_display = bool(pymupdf.TOOLS.mupdf_display_errors())
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        document = pymupdf.open(path)
    except Exception:
        pymupdf.TOOLS.mupdf_display_errors(previous_error_display)
        return result
    try:
        for page_number, score in selected:
            try:
                page = document[page_number - 1]
                scale = min(2.0, max(0.5, target_width / max(1.0, float(page.rect.width))))
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                payload = pixmap.tobytes("png")
            except Exception:
                continue
            member = f"illustration_{safe_prefix}_p{page_number:04d}.png"
            target = output_dir / member
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(target)
            result.append(RenderedVisual(
                page_number=page_number,
                asset_member=member,
                caption=f"Minh họa từ {document_title}, trang {page_number}.",
                byte_size=len(payload),
                sha256=sha256_bytes(payload),
                width=pixmap.width,
                height=pixmap.height,
                score=score,
            ))
    finally:
        document.close()
        pymupdf.TOOLS.mupdf_display_errors(previous_error_display)
    return result
