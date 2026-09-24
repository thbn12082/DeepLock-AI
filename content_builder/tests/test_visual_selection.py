from __future__ import annotations

import pymupdf

from deeplock_content.visuals import render_pdf_visuals, select_pdf_visual_pages


def test_selects_and_renders_visual_page_per_part(tmp_path):
    pdf = tmp_path / "mlops.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Trang thuần văn bản về triển khai mô hình.")
    page = document.new_page()
    page.insert_text((72, 72), "Architecture pipeline diagram")
    page.draw_rect(pymupdf.Rect(70, 100, 250, 220), color=(0, 0, 0), fill=(0.8, 0.9, 1))
    page.draw_rect(pymupdf.Rect(330, 100, 510, 220), color=(0, 0, 0), fill=(0.8, 1, 0.9))
    page.draw_line((250, 160), (330, 160), color=(0, 0, 0), width=3)
    document.save(pdf)
    document.close()

    selected = select_pdf_visual_pages(pdf, pages_per_part=24)
    assert selected and selected[0][0] == 2
    rendered = render_pdf_visuals(
        pdf,
        output_dir=tmp_path / "media",
        asset_prefix="mlflow",
        document_title="MLflow",
    )
    assert len(rendered) == 1
    assert rendered[0].page_number == 2
    assert rendered[0].asset_member.endswith(".png")
    assert (tmp_path / "media" / rendered[0].asset_member).read_bytes().startswith(b"\x89PNG")


def test_does_not_select_decorative_closing_slide(tmp_path):
    pdf = tmp_path / "closing.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Thank You!")
    page.draw_rect(
        pymupdf.Rect(20, 20, 570, 800),
        color=(1, 0.2, 0.5),
        fill=(1, 0.7, 0.8),
    )
    document.save(pdf)
    document.close()

    assert select_pdf_visual_pages(pdf, pages_per_part=24) == []
