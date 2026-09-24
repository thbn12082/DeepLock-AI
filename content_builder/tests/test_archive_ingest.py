from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

from deeplock_content import cli
from deeplock_content.chunking import build_corpus
from deeplock_content.extractors import (
    build_archive_inventory,
    extract_inventory_documents,
    export_ooxml_media,
    inventory_sha256,
    ooxml_media_manifest_sha256,
)
from deeplock_content.extractors.ooxml import extract_docx, extract_pptx
from deeplock_content.models import ExtractedCorpus
from deeplock_content.util import read_json


PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WORD_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PICTURE_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def pptx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/presentation.xml",
            f'''<p:presentation xmlns:p="{PRESENTATION_NS}" xmlns:r="{REL_NS}">
                <p:sldIdLst>
                  <p:sldId id="256" r:id="rIdSecond"/>
                  <p:sldId id="257" r:id="rIdFirst"/>
                </p:sldIdLst>
              </p:presentation>''',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            f'''<Relationships xmlns="{PACKAGE_REL_NS}">
                <Relationship Id="rIdFirst" Target="/ppt/slides/slide1.xml"/>
                <Relationship Id="rIdSecond" Target="slides/slide2.xml"/>
              </Relationships>''',
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            f'''<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}" xmlns:r="{REL_NS}">
                <p:cSld><p:spTree><p:sp><p:nvSpPr>
                  <p:cNvPr id="1" name="Diagram" descr="Luồng CI/CD"/>
                </p:nvSpPr><p:txBody><a:p><a:r><a:t>Slide thứ nhất</a:t></a:r></a:p>
                </p:txBody></p:sp><p:pic><p:nvPicPr>
                  <p:cNvPr id="2" name="Pipeline" descr="Luồng CI/CD"/>
                </p:nvPicPr><p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill>
                </p:pic></p:spTree></p:cSld>
              </p:sld>''',
        )
        archive.writestr(
            "ppt/slides/slide2.xml",
            f'''<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}">
                <p:cSld><p:spTree><p:sp><p:txBody>
                  <a:p><a:r><a:t>Slide thứ hai</a:t></a:r></a:p>
                </p:txBody></p:sp></p:spTree></p:cSld>
              </p:sld>''',
        )
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            f'''<Relationships xmlns="{PACKAGE_REL_NS}">
                <Relationship Id="rIdImage" Target="../media/pipeline.png"/>
              </Relationships>''',
        )
        archive.writestr("ppt/media/pipeline.png", PNG_BYTES)
    return output.getvalue()


def docx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            f'''<w:document xmlns:w="{WORD_NS}" xmlns:r="{REL_NS}"
                xmlns:wp="{WORD_DRAWING_NS}" xmlns:a="{DRAWING_NS}" xmlns:pic="{PICTURE_NS}"><w:body>
                <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
                  <w:r><w:t>MLOps căn bản</w:t></w:r></w:p>
                <w:p><w:r><w:t>Data versioning và model registry.</w:t></w:r></w:p>
                <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
                  <w:r><w:t>Triển khai</w:t></w:r><w:r><w:drawing><wp:inline>
                    <wp:docPr id="1" name="Architecture" descr="Sơ đồ triển khai"/>
                    <a:graphic><a:graphicData><pic:pic><pic:blipFill>
                      <a:blip r:embed="rIdDiagram"/>
                    </pic:blipFill></pic:pic></a:graphicData></a:graphic>
                  </wp:inline></w:drawing></w:r></w:p>
                <w:tbl><w:tr><w:tc><w:p><w:r><w:t>CI</w:t></w:r></w:p></w:tc>
                  <w:tc><w:p><w:r><w:t>CD</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
              </w:body></w:document>''',
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            f'''<Relationships xmlns="{PACKAGE_REL_NS}">
                <Relationship Id="rIdDiagram" Target="media/deployment.png"/>
              </Relationships>''',
        )
        archive.writestr("word/media/deployment.png", PNG_BYTES)
    return output.getvalue()


def write_lecture_zip(path: Path) -> None:
    pptx = pptx_bytes()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Course/Day 1/Monitoring.pptx", pptx)
        archive.writestr("Course/Duplicate/Monitoring-copy.pptx", pptx)
        archive.writestr("Course/MLOps notes.docx", docx_bytes())
        archive.writestr("Course/assignment.json", json.dumps({"kind": "assignment"}))


def test_pptx_uses_actual_presentation_order_and_keeps_alt_text():
    pages = extract_pptx(pptx_bytes(), "fixture.pptx")
    assert [text.splitlines()[0] for _, text in pages] == [
        "Slide thứ hai",
        "Slide thứ nhất",
    ]
    assert "Mô tả hình: Luồng CI/CD" in pages[1][1]


def test_docx_is_split_on_heading_and_preserves_table_text():
    pages = extract_docx(docx_bytes(), "fixture.docx")
    assert len(pages) == 2
    assert "Data versioning" in pages[0][1]
    assert "CI | CD" in pages[1][1]


def test_inventory_deduplicates_bytes_but_preserves_every_occurrence(tmp_path):
    archive = tmp_path / "lectures.zip"
    write_lecture_zip(archive)
    inventory = build_archive_inventory(
        [archive],
        extract_dir=tmp_path / "documents",
    )

    assert inventory.counts.document_occurrences == 3
    assert inventory.counts.unique_documents == 2
    assert inventory.counts.exact_duplicate_occurrences == 1
    assert inventory.counts.ancillary_occurrences == 1
    assert inventory.counts.by_suffix == {".docx": 1, ".pptx": 2}
    assert inventory.inventory_sha256 == inventory_sha256(inventory)
    assert len(list((tmp_path / "documents").glob("*"))) == 2

    duplicated = next(item for item in inventory.documents if len(item.occurrence_ids) == 2)
    assert len(duplicated.logical_paths) == 2
    extracted = extract_inventory_documents(inventory, workdir=tmp_path)
    corpus = build_corpus(extracted)
    assert len(corpus.documents) == 2
    assert {item.source_format for item in corpus.documents} == {"PPTX", "DOCX"}
    pptx_document = next(item for item in corpus.documents if item.source_format == "PPTX")
    assert len(pptx_document.source_aliases) == 2


def test_ooxml_media_is_deduplicated_with_exact_slide_and_section_provenance(tmp_path):
    archive = tmp_path / "lectures.zip"
    write_lecture_zip(archive)
    inventory = build_archive_inventory(
        [archive],
        extract_dir=tmp_path / "documents",
    )
    corpus = build_corpus(extract_inventory_documents(inventory, workdir=tmp_path))

    manifest = export_ooxml_media(
        corpus.documents,
        output_dir=tmp_path / "ooxml-media",
    )

    assert manifest.manifest_sha256 == ooxml_media_manifest_sha256(manifest)
    assert manifest.counts.documents_scanned == 2
    assert manifest.counts.package_media_parts == 2
    assert manifest.counts.unique_assets == 1
    assert manifest.counts.exact_duplicate_media_parts == 1
    assert manifest.counts.media_occurrences == 2
    assert len(list((tmp_path / "ooxml-media").glob("media_*"))) == 1

    pptx = next(item for item in manifest.occurrences if item.source_format == "PPTX")
    assert pptx.mapping_kind == "PPTX_SLIDE_EMBED"
    assert pptx.page_number == 2
    assert pptx.alt_text == "Luồng CI/CD"
    docx = next(item for item in manifest.occurrences if item.source_format == "DOCX")
    assert docx.mapping_kind == "DOCX_BODY_EMBED"
    assert docx.page_number == 2
    assert docx.section_title == "Triển khai"
    assert docx.alt_text == "Sơ đồ triển khai"


def test_archive_rejects_path_traversal_before_extraction(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "unsafe")
    with pytest.raises(ValueError, match="unsafe"):
        build_archive_inventory([archive], extract_dir=tmp_path / "documents")
    assert not (tmp_path / "escape.txt").exists()


def test_archive_rejects_zip_bomb_ratio(tmp_path):
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("lecture.txt", b"0" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="compression ratio"):
        build_archive_inventory([archive], extract_dir=tmp_path / "documents")


def test_ingest_archives_cli_writes_inventory_and_source(tmp_path, capsys):
    archive = tmp_path / "lectures.zip"
    workdir = tmp_path / "build"
    write_lecture_zip(archive)

    cli.ingest_archives([archive], workdir, True)

    inventory = read_json(workdir / "inventory.json")
    source = ExtractedCorpus.model_validate(read_json(workdir / "source.json"))
    media = read_json(workdir / "ooxml-media.json")
    assert inventory["counts"]["document_occurrences"] == 3
    assert len(source.documents) == 2
    assert media["counts"]["unique_assets"] == 1
    ready = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert ready["status"] == "READY"
    assert ready["unique_documents"] == 2
    assert ready["ooxml_media_assets"] == 1
