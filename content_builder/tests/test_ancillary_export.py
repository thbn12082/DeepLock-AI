from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from deeplock_content import cli
from deeplock_content.extractors import (
    AncillaryManifest,
    ancillary_manifest_sha256,
    build_archive_inventory,
    export_ancillary,
)
from deeplock_content.util import read_json, write_json


JPEG_BYTES = b"\xff\xd8\xff\xe0JFIF\x00\xff\xd9"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _assessment(*, with_questions: bool = False) -> bytes:
    metadata: dict[str, object] = {
        "object": {
            "title": "Lab Assignment",
            "data": {"assessmentId": "assessment-source", "default": True},
            "questionsCount": 1,
        },
        "assessment": {"isGraded": True},
    }
    if with_questions:
        metadata["questions"] = [
            {
                "prompt": "What is data drift?",
                "options": ["A", "B"],
                "correctAnswer": "A",
            }
        ]
    return _json_bytes({
        "kind": "assessment",
        "courseId": "course-1",
        "courseTitle": "Course One",
        "sectionId": "day-1",
        "sectionTitle": "Day 1",
        "unitId": "lab-1",
        "unitTitle": "Lab Assignment",
        "sourcePath": "assessment-source",
        "metadata": metadata,
    })


def _course_metadata() -> bytes:
    return _json_bytes({
        "kind": "course-metadata",
        "courseId": "course-1",
        "courseTitle": "Course One",
        "sectionId": "course",
        "sectionTitle": "Course metadata",
        "unitId": "course",
        "unitTitle": "Course One",
        "sourcePath": "course-1.json",
        "metadata": {"courseImage": {"name": "/cover.jpg"}},
    })


def _external_link(*, attendance: bool) -> bytes:
    if attendance:
        title = "Daily Attendance Check"
        object_type = "lti"
        url = "https://vinuni.edu.vn"
    else:
        title = "MLOps deployment tutorial"
        object_type = "youtube"
        url = "https://www.youtube.com/embed/example"
    return _json_bytes({
        "kind": "external-link",
        "courseId": "course-1",
        "courseTitle": "Course One",
        "sectionId": "day-1",
        "sectionTitle": "Day 1",
        "unitId": title.casefold().replace(" ", "-"),
        "unitTitle": title,
        "sourcePath": url,
        "metadata": {
            "objectType": object_type,
            "title": title,
            "url": url,
            "contentLink": url if attendance else None,
        },
    })


def _write_ancillary_archive(path: Path, *, with_questions: bool = False) -> bytes:
    assessment = _assessment(with_questions=with_questions)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("course/00 - metadata/01 - course-metadata.json", _course_metadata())
        archive.writestr("course/00 - metadata/02 - cover.jpg", JPEG_BYTES)
        archive.writestr("course/01 - attendance/01 - Check.link.json", _external_link(attendance=True))
        archive.writestr("course/02 - day/01 - Tutorial.link.json", _external_link(attendance=False))
        archive.writestr("course/02 - day/02 - Lab.assessment.json", assessment)
        archive.writestr("course/02 - day/03 - Lab copy.assessment.json", assessment)
    return assessment


def test_ancillary_export_exact_deduplicates_and_never_schedules_shells(tmp_path):
    archive = tmp_path / "course.zip"
    assessment_raw = _write_ancillary_archive(archive)
    inventory = build_archive_inventory([archive])

    manifest = export_ancillary(inventory, output_dir=tmp_path / "ancillary")

    assert manifest.manifest_sha256 == ancillary_manifest_sha256(manifest)
    assert manifest.counts.ancillary_occurrences == 6
    assert manifest.counts.unique_assets == 5
    assert manifest.counts.exact_duplicate_occurrences == 1
    assert manifest.counts.occurrences_by_classification == {
        "ASSESSMENT_SHELL": 2,
        "COURSE_COVER": 1,
        "COURSE_METADATA": 1,
        "EXTERNAL_LEARNING_LINK": 1,
        "NONLEARNING_LINK": 1,
    }
    assert manifest.counts.assets_by_classification["ASSESSMENT_SHELL"] == 1
    assert manifest.counts.assessment_declared_question_count == 2
    assert manifest.counts.schedulable_assets == 0
    assert manifest.counts.quiz_payload_assets == 0
    assert all(not item.schedulable for item in manifest.assets)
    assert all(not item.quiz_payload_available for item in manifest.assets)

    assessment = next(item for item in manifest.assets if item.classification == "ASSESSMENT_SHELL")
    assert len(assessment.occurrence_ids) == 2
    assert len(assessment.logical_paths) == 2
    assert assessment.metadata.activity_kind == "LAB_ASSIGNMENT"
    assert assessment.metadata.declared_question_count == 1
    assert (tmp_path / assessment.extracted_path).read_bytes() == assessment_raw
    assert len(list((tmp_path / "ancillary").glob("ancillary_*"))) == 5

    resumed = export_ancillary(inventory, output_dir=tmp_path / "ancillary")
    assert resumed.manifest_sha256 == manifest.manifest_sha256
    assert AncillaryManifest.model_validate(resumed.model_dump(mode="json")) == resumed


def test_ancillary_export_rejects_an_assessment_with_embedded_questions(tmp_path):
    archive = tmp_path / "unsafe-course.zip"
    _write_ancillary_archive(archive, with_questions=True)
    inventory = build_archive_inventory([archive])

    with pytest.raises(ValueError, match="not a shell"):
        export_ancillary(inventory, output_dir=tmp_path / "ancillary")


def test_export_ancillary_cli_resumes_from_inventory(tmp_path, capsys):
    archive = tmp_path / "course.zip"
    _write_ancillary_archive(archive)
    workdir = tmp_path / "build"
    inventory = build_archive_inventory([archive])
    write_json(workdir / "inventory.json", inventory.model_dump(mode="json"))

    cli.export_ancillary_command(workdir)

    manifest = AncillaryManifest.model_validate(read_json(workdir / "ancillary.json"))
    ready = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert ready["status"] == "READY"
    assert ready["manifest_sha256"] == manifest.manifest_sha256
    assert ready["occurrences"] == 6
    assert ready["unique_assets"] == 5
    assert ready["schedulable_assets"] == 0
    assert ready["quiz_payload_assets"] == 0
