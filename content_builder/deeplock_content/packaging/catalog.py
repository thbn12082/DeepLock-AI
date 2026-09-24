from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..util import read_json, sha256_bytes, sha256_json, stable_id, write_json
from .pack import verify_pack


_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+\Z")
_UPGRADE_MEMBER_NAMES = (
    "course.json",
    "modules.json",
    "atoms.json",
    "lessons.json",
    "learning_pairs.json",
    "questions.json",
)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Catalog pack descriptor has no {field}")
    return text


def _reconcile_pack(
    *,
    path: Path,
    descriptor: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    before_verify_sha256 = sha256_bytes(path.read_bytes())
    manifest = verify_pack(path)
    actual_sha256 = sha256_bytes(path.read_bytes())
    if before_verify_sha256 != actual_sha256:
        raise ValueError(f"Pack changed while being verified: {path}")
    expected_sha256 = _required_text(descriptor.get("asset_sha256"), "asset_sha256")
    if not _SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError(f"Catalog has invalid pack digest: {expected_sha256}")
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Catalog pack checksum mismatch for {path.name}: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )
    expected_pack_id = _required_text(descriptor.get("pack_id"), "pack_id")
    expected_course_id = _required_text(descriptor.get("course_id"), "course_id")
    if manifest.get("content_pack_id") != expected_pack_id:
        raise ValueError(f"Catalog/manifest pack_id mismatch for {path.name}")
    if manifest.get("course_id") != expected_course_id:
        raise ValueError(f"Catalog/manifest course_id mismatch for {path.name}")
    return manifest, actual_sha256


def _publish_immutable_pack(staged: Path, destination: Path) -> bool:
    """Publish without replacing an asset that an older catalog may reference.

    Returns true when a new immutable file was installed and false when an
    already-identical content-addressed asset was safely reused.
    """

    if destination.exists():
        if not destination.is_file() or destination.is_symlink():
            raise ValueError(f"Refusing to replace non-file Android asset: {destination}")
        if sha256_bytes(destination.read_bytes()) != sha256_bytes(staged.read_bytes()):
            raise ValueError(f"Content-addressed Android asset collision: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # The staging directory is created under android_assets, so this is a
        # same-volume atomic publication on normal filesystems. It never
        # overwrites an existing path.
        os.link(staged, destination)
    except FileExistsError:
        if (
            not destination.is_file()
            or destination.is_symlink()
            or sha256_bytes(destination.read_bytes()) != sha256_bytes(staged.read_bytes())
        ):
            raise ValueError(f"Android asset changed during install: {destination}")
        return False
    except OSError:
        # Some synced/network filesystems do not support hard links. Since the
        # content-addressed path is not referenced by the old catalog, an
        # exclusive create remains transaction-safe; catalog commit still
        # happens only after the complete file is verified.
        try:
            with staged.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except FileExistsError:
            if (
                not destination.is_file()
                or destination.is_symlink()
                or sha256_bytes(destination.read_bytes()) != sha256_bytes(staged.read_bytes())
            ):
                raise ValueError(f"Android asset changed during install: {destination}")
            return False
        except Exception:
            # destination was opened with exclusive creation, so only a file
            # created by this invocation can be removed here.
            destination.unlink(missing_ok=True)
            raise
    return True


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Android catalog has invalid {field}")
    return value


def _strict_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Android catalog has invalid or blank {field}")
    return value


def _object_list(catalog: dict[str, Any], field: str, *, nonempty: bool = True) -> list[dict[str, Any]]:
    raw = catalog.get(field)
    if not isinstance(raw, list) or (nonempty and not raw):
        raise ValueError(f"Android catalog has invalid or empty {field}")
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError(f"Android catalog has a non-object {field} entry")
    return raw


def _string_list(value: Any, field: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"Android catalog has invalid {field}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"Android catalog has invalid {field}")
    return value


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Android catalog contains duplicate {label} IDs")


def _validate_catalog_content_ids(
    atoms: list[dict[str, Any]],
    *,
    pack_ids: set[str],
) -> None:
    """Mirror Android ownership checks and make identity namespaces fail closed."""

    atom_ids = {_strict_text(item.get("atom_id"), "atom_id") for item in atoms}
    identities: list[str] = []
    order_indexes: list[int] = []
    for item in atoms:
        pack_id = _strict_text(item.get("pack_id"), "pack_id")
        if pack_id not in pack_ids:
            raise ValueError(f"Android catalog atom references an unknown pack: {pack_id}")
        atom_id = _strict_text(item.get("atom_id"), "atom_id")
        pair_id = _strict_text(item.get("pair_id"), "pair_id")
        lesson_id = _strict_text(item.get("lesson_id"), "lesson_id")
        _strict_text(item.get("module_id"), "module_id")
        _strict_text(item.get("title"), "title")
        order_indexes.append(_nonnegative_integer(item.get("order_index"), "atom order_index"))
        prerequisites = _string_list(item.get("prerequisite_ids"), "prerequisite_ids")
        question_ids = _string_list(item.get("question_ids"), "question_ids", minimum=3)
        if len(prerequisites) != len(set(prerequisites)) or atom_id in prerequisites:
            raise ValueError(f"Android catalog has invalid prerequisites for {atom_id}")
        if not set(prerequisites) <= atom_ids:
            raise ValueError(f"Android catalog has unknown prerequisites for {atom_id}")
        if len(question_ids) != len(set(question_ids)):
            raise ValueError(f"Android catalog has duplicate questions for {atom_id}")
        identities.extend((atom_id, pair_id, lesson_id, *question_ids))

    _require_unique(identities, "global content")
    if set(identities) & pack_ids:
        raise ValueError("Android catalog pack/content IDs are not globally unique")
    if len(order_indexes) != len(set(order_indexes)):
        raise ValueError("Android catalog contains duplicate atom order indexes")
    if sorted(order_indexes) != list(range(len(order_indexes))):
        raise ValueError("Android catalog atom order indexes are not contiguous")
    if any(pack_id not in {str(item["pack_id"]) for item in atoms} for pack_id in pack_ids):
        raise ValueError("Android catalog contains a pack with no atoms")

    prerequisites_by_atom = {
        str(item["atom_id"]): [str(value) for value in item["prerequisite_ids"]]
        for item in atoms
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(atom_id: str) -> None:
        if atom_id in visited:
            return
        if atom_id in visiting:
            raise ValueError("Android catalog contains a cyclic prerequisite graph")
        visiting.add(atom_id)
        for prerequisite in prerequisites_by_atom[atom_id]:
            visit(prerequisite)
        visiting.remove(atom_id)
        visited.add(atom_id)

    for atom_id in prerequisites_by_atom:
        visit(atom_id)


def _validate_installed_catalog(catalog: Any) -> dict[str, Any]:
    """Validate the installed index without opening legacy pack payloads."""

    if not isinstance(catalog, dict) or catalog.get("schema_version") != "1.0":
        raise ValueError("Unsupported or invalid installed Android catalog")
    for field in ("catalog_id", "title", "language", "default_pack_id"):
        _strict_text(catalog.get(field), field)

    categories = _object_list(catalog, "categories")
    lectures = _object_list(catalog, "lectures")
    descriptors = _object_list(catalog, "packs")
    atoms = _object_list(catalog, "atoms")

    category_ids = [_strict_text(item.get("category_id"), "category_id") for item in categories]
    lecture_ids = [_strict_text(item.get("lecture_id"), "lecture_id") for item in lectures]
    pack_ids = [_strict_text(item.get("pack_id"), "pack_id") for item in descriptors]
    _require_unique(category_ids, "category")
    _require_unique(lecture_ids, "lecture")
    _require_unique(pack_ids, "pack")
    category_id_set = set(category_ids)
    lecture_id_set = set(lecture_ids)
    pack_id_set = set(pack_ids)
    if str(catalog["default_pack_id"]) not in pack_id_set:
        raise ValueError("Installed Android catalog default pack is missing")

    for item in categories:
        _strict_text(item.get("title"), "category title")
        _nonnegative_integer(item.get("order_index"), "category order_index")
        if "description" in item and not isinstance(item["description"], str):
            raise ValueError("Android catalog has invalid category description")
        if "search_terms" in item:
            _string_list(item["search_terms"], "category search_terms")
    for item in lectures:
        _strict_text(item.get("title"), "lecture title")
        _nonnegative_integer(item.get("order_index"), "lecture order_index")
        if _strict_text(item.get("category_id"), "category_id") not in category_id_set:
            raise ValueError("Installed Android catalog lecture has an unknown category")
        if "description" in item and not isinstance(item["description"], str):
            raise ValueError("Android catalog has invalid lecture description")
        if "search_terms" in item:
            _string_list(item["search_terms"], "lecture search_terms")
    if any(not any(item["category_id"] == category_id for item in lectures) for category_id in category_ids):
        raise ValueError("Installed Android catalog contains a category with no lectures")

    asset_paths: list[str] = []
    for item in descriptors:
        lecture_id = _strict_text(item.get("lecture_id"), "lecture_id")
        if lecture_id not in lecture_id_set:
            raise ValueError("Installed Android catalog pack has an unknown lecture")
        _strict_text(item.get("title"), "pack title")
        _strict_text(item.get("course_id"), "course_id")
        _nonnegative_integer(item.get("order_index"), "pack order_index")
        digest = _strict_text(item.get("asset_sha256"), "asset_sha256")
        if not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError(f"Installed Android catalog has invalid pack digest: {digest}")
        asset_path = _strict_text(item.get("asset_path"), "asset_path")
        posix = PurePosixPath(asset_path)
        if (
            posix.is_absolute()
            or not posix.parts
            or posix.parts[0] != "content"
            or ".." in posix.parts
            or "\\" in asset_path
            or posix.suffix != ".dlpack"
        ):
            raise ValueError(f"Installed Android catalog has unsafe asset_path: {asset_path}")
        asset_paths.append(posix.as_posix())
    if len(asset_paths) != len(set(asset_paths)):
        raise ValueError("Installed Android catalog contains duplicate pack asset paths")
    if any(not any(item["lecture_id"] == lecture_id for item in descriptors) for lecture_id in lecture_ids):
        raise ValueError("Installed Android catalog contains a lecture with no pack")

    _validate_catalog_content_ids(atoms, pack_ids=pack_id_set)
    return catalog


def _safe_installed_asset(assets_root: Path, asset_path: str) -> Path:
    posix = PurePosixPath(asset_path)
    candidate = assets_root.joinpath(*posix.parts)
    cursor = assets_root
    for part in posix.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Installed Android asset path contains a symlink: {asset_path}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"Installed Android asset is missing: {asset_path}") from error
    try:
        resolved.relative_to(assets_root)
    except ValueError as error:
        raise ValueError(f"Installed Android asset escapes the assets root: {asset_path}") from error
    if candidate.is_symlink() or not resolved.is_file():
        raise ValueError(f"Installed Android asset is not a regular file: {asset_path}")
    return resolved


def _verify_committed_legacy_assets(
    *,
    assets_root: Path,
    descriptors: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Authenticate legacy assets only by their catalog-committed bytes.

    Deliberately do not call verify_pack here: an older installed pack can be
    structurally valid for its app release while failing today's semantic gate.
    """

    verified: list[dict[str, str]] = []
    for descriptor in descriptors:
        asset_path = str(descriptor["asset_path"])
        path = _safe_installed_asset(assets_root, asset_path)
        expected = str(descriptor["asset_sha256"])
        actual = sha256_bytes(path.read_bytes())
        if actual != expected:
            raise ValueError(
                f"Installed legacy asset checksum mismatch for {descriptor['pack_id']}: "
                f"expected={expected}, actual={actual}"
            )
        verified.append({
            "pack_id": str(descriptor["pack_id"]),
            "asset_path": asset_path,
            "asset_sha256": actual,
        })
    return verified


def _pack_json_members(path: Path, verified_manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest != verified_manifest:
                raise ValueError(f"New pack manifest changed after verification: {path}")
            return {
                name: json.loads(archive.read(name))
                for name in _UPGRADE_MEMBER_NAMES
            }
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise ValueError(f"New pack has invalid catalog members: {path}") from error


def _new_pack_catalog_records(
    *,
    manifest: dict[str, Any],
    members: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    course = members["course.json"]
    if not isinstance(course, dict):
        raise ValueError("New pack course.json must be an object")
    course_id = _strict_text(course.get("course_id"), "course_id")
    if course_id != _strict_text(manifest.get("course_id"), "manifest course_id"):
        raise ValueError("New pack manifest/course ID mismatch")

    def member_objects(name: str) -> list[dict[str, Any]]:
        value = members[name]
        if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
            raise ValueError(f"New pack {name} must be a non-empty object array")
        return value

    modules = member_objects("modules.json")
    atoms = member_objects("atoms.json")
    lessons = member_objects("lessons.json")
    pairs = member_objects("learning_pairs.json")
    questions = member_objects("questions.json")

    module_ids = [_strict_text(item.get("module_id"), "module_id") for item in modules]
    _require_unique(module_ids, "new-pack module")
    module_order: dict[str, int] = {}
    for item in modules:
        module_id = str(item["module_id"])
        module_order[module_id] = _nonnegative_integer(item.get("order_index"), "module order_index")
    if len(set(module_order.values())) != len(module_order):
        raise ValueError("New pack contains duplicate module order indexes")

    atom_ids = [_strict_text(item.get("atom_id"), "atom_id") for item in atoms]
    _require_unique(atom_ids, "new-pack atom")
    atom_id_set = set(atom_ids)
    local_orders: set[tuple[str, int]] = set()
    for item in atoms:
        module_id = _strict_text(item.get("module_id"), "module_id")
        if module_id not in module_order:
            raise ValueError("New pack atom references an unknown module")
        local_key = (module_id, _nonnegative_integer(item.get("order_index"), "atom order_index"))
        if local_key in local_orders:
            raise ValueError("New pack contains duplicate atom order indexes in a module")
        local_orders.add(local_key)
        prerequisites = _string_list(item.get("prerequisite_ids"), "atom prerequisite_ids")
        if len(prerequisites) != len(set(prerequisites)) or str(item["atom_id"]) in prerequisites:
            raise ValueError("New pack atom has invalid prerequisites")
        if not set(prerequisites) <= atom_id_set:
            raise ValueError("New pack atom has unknown prerequisites")
        _strict_text(item.get("title"), "atom title")
    if set(module_ids) != {str(item["module_id"]) for item in atoms}:
        raise ValueError("New pack contains a module with no atoms")

    def by_atom(values: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in values:
            atom_id = _strict_text(item.get("atom_id"), f"{label} atom_id")
            if atom_id in result:
                raise ValueError(f"New pack contains duplicate {label} atom ownership")
            result[atom_id] = item
        if set(result) != atom_id_set:
            raise ValueError(f"New pack {label} coverage does not match atoms")
        return result

    lesson_by_atom = by_atom(lessons, "lesson")
    pair_by_atom = by_atom(pairs, "learning-pair")
    lesson_ids = [_strict_text(item.get("lesson_id"), "lesson_id") for item in lessons]
    pair_ids = [_strict_text(item.get("pair_id"), "pair_id") for item in pairs]
    question_ids = [_strict_text(item.get("question_id"), "question_id") for item in questions]
    _require_unique(lesson_ids, "new-pack lesson")
    _require_unique(pair_ids, "new-pack pair")
    _require_unique(question_ids, "new-pack question")
    question_by_id = {str(item["question_id"]): item for item in questions}
    owned_questions: list[str] = []

    records: list[dict[str, Any]] = []
    ordered_atoms = sorted(
        atoms,
        key=lambda item: (
            module_order[str(item["module_id"])],
            int(item["order_index"]),
            str(item["atom_id"]),
        ),
    )
    pack_id = _strict_text(manifest.get("content_pack_id"), "content_pack_id")
    for atom in ordered_atoms:
        atom_id = str(atom["atom_id"])
        lesson = lesson_by_atom[atom_id]
        pair = pair_by_atom[atom_id]
        lesson_id = str(lesson["lesson_id"])
        pair_id = str(pair["pair_id"])
        if _strict_text(lesson.get("pair_id"), "lesson pair_id") != pair_id:
            raise ValueError(f"New pack lesson/pair mismatch for {atom_id}")
        if _strict_text(pair.get("micro_lesson_id"), "micro_lesson_id") != lesson_id:
            raise ValueError(f"New pack pair/lesson mismatch for {atom_id}")
        pair_question_ids = _string_list(pair.get("question_ids"), "pair question_ids", minimum=3)
        if len(pair_question_ids) != len(set(pair_question_ids)):
            raise ValueError(f"New pack pair has duplicate question IDs for {atom_id}")
        for question_id in pair_question_ids:
            question = question_by_id.get(question_id)
            if question is None:
                raise ValueError(f"New pack pair references an unknown question: {question_id}")
            if (
                _strict_text(question.get("atom_id"), "question atom_id") != atom_id
                or _strict_text(question.get("pair_id"), "question pair_id") != pair_id
            ):
                raise ValueError(f"New pack question ownership mismatch: {question_id}")
        owned_questions.extend(pair_question_ids)
        records.append({
            "pack_id": pack_id,
            "atom_id": atom_id,
            "pair_id": pair_id,
            "lesson_id": lesson_id,
            "module_id": str(atom["module_id"]),
            "title": str(atom["title"]),
            "order_index": 0,
            "prerequisite_ids": list(atom["prerequisite_ids"]),
            "question_ids": list(pair_question_ids),
        })
    if len(owned_questions) != len(set(owned_questions)) or set(owned_questions) != set(question_ids):
        raise ValueError("New pack question ownership is incomplete or duplicated")
    return course_id, records


def _fully_verify_new_pack(
    path: Path,
    *,
    expected_digest: str | None = None,
    expected_manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str, list[dict[str, Any]]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"New pack must be a regular, non-symlink file: {path}")
    before = sha256_bytes(path.read_bytes())
    manifest = verify_pack(path)
    after_verify = sha256_bytes(path.read_bytes())
    if before != after_verify:
        raise ValueError(f"New pack changed while being fully verified: {path}")
    if expected_digest is not None and after_verify != expected_digest:
        raise ValueError(f"New pack digest changed after staging/publication: {path}")
    if expected_manifest is not None and manifest != expected_manifest:
        raise ValueError(f"New pack manifest changed after staging/publication: {path}")
    members = _pack_json_members(path, manifest)
    course_id, records = _new_pack_catalog_records(manifest=manifest, members=members)
    if sha256_bytes(path.read_bytes()) != after_verify:
        raise ValueError(f"New pack changed while catalog members were parsed: {path}")
    return manifest, after_verify, course_id, records


def _restore_catalog_bytes(catalog_path: Path, payload: bytes, stage_root: Path) -> None:
    rollback = stage_root / "catalog.rollback"
    with rollback.open("xb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    os.replace(rollback, catalog_path)


def upgrade_android_catalog_pack(
    *,
    lecture_id: str,
    new_pack: Path,
    android_assets: Path,
) -> dict[str, Any]:
    """Safely replace one lecture pack in an already-installed catalog.

    Unchanged legacy packs are authenticated by the exact SHA-256 committed in
    the installed catalog, not reinterpreted through today's semantic gates.
    The new pack receives full verification as source, staged file and final
    published file. Its immutable asset is published first; the catalog is the
    final atomic commit pointer and is restored byte-for-byte if post-commit
    verification fails.
    """

    lecture_id = _required_text(lecture_id, "lecture_id")
    if not _SAFE_ID_PATTERN.fullmatch(lecture_id):
        raise ValueError("Target lecture_id is unsafe")
    if android_assets.is_symlink() or not android_assets.is_dir():
        raise ValueError(f"Android assets directory does not exist or is unsafe: {android_assets}")
    assets_root = android_assets.resolve()
    catalog_path = assets_root / "content" / "catalog.json"
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ValueError(f"Installed Android catalog is missing or unsafe: {catalog_path}")
    original_catalog_bytes = catalog_path.read_bytes()
    original_catalog_sha256 = sha256_bytes(original_catalog_bytes)
    try:
        raw_catalog = json.loads(original_catalog_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Installed Android catalog is not valid UTF-8 JSON") from error
    _validate_installed_catalog(raw_catalog)

    raw_descriptors = raw_catalog["packs"]
    target_indexes = [
        index for index, descriptor in enumerate(raw_descriptors)
        if descriptor["lecture_id"] == lecture_id
    ]
    if len(target_indexes) != 1:
        raise ValueError(
            f"Target lecture must have exactly one installed pack descriptor: {lecture_id}"
        )
    if sum(1 for item in raw_catalog["lectures"] if item["lecture_id"] == lecture_id) != 1:
        raise ValueError(f"Target lecture is missing or duplicated: {lecture_id}")
    target_index = target_indexes[0]
    old_descriptor = raw_descriptors[target_index]
    old_pack_id = str(old_descriptor["pack_id"])
    old_target_atoms = [item for item in raw_catalog["atoms"] if item["pack_id"] == old_pack_id]
    if not old_target_atoms:
        raise ValueError("Target installed pack has no catalog atom records")

    unchanged_descriptors = [
        descriptor for index, descriptor in enumerate(raw_descriptors)
        if index != target_index
    ]
    legacy_verification = _verify_committed_legacy_assets(
        assets_root=assets_root,
        descriptors=unchanged_descriptors,
    )

    manifest, digest, course_id, new_records = _fully_verify_new_pack(new_pack)
    new_pack_id = str(manifest["content_pack_id"])
    unchanged_pack_ids = {str(item["pack_id"]) for item in unchanged_descriptors}
    if new_pack_id in unchanged_pack_ids:
        raise ValueError(f"New pack ID collides with an unchanged catalog pack: {new_pack_id}")

    digest_hex = digest.removeprefix("sha256:")
    installed_name = f"{lecture_id}.{digest_hex}.dlpack"
    relative_asset = PurePosixPath("content", "packs", installed_name).as_posix()
    destination = assets_root / Path(relative_asset)
    stage_root = assets_root / f".catalog-upgrade-{uuid.uuid4().hex}.tmp"
    stage_root.mkdir(parents=True)
    staged_pack = stage_root / installed_name
    staged_catalog = stage_root / "catalog.json"
    catalog_replaced = False
    try:
        shutil.copyfile(new_pack, staged_pack)
        staged_manifest, staged_digest, staged_course_id, staged_records = _fully_verify_new_pack(
            staged_pack,
            expected_digest=digest,
            expected_manifest=manifest,
        )
        if (
            staged_manifest != manifest
            or staged_digest != digest
            or staged_course_id != course_id
            or staged_records != new_records
        ):
            raise ValueError("Staged new pack does not exactly match its verified source")

        upgraded_catalog = copy.deepcopy(raw_catalog)
        new_descriptor = dict(old_descriptor)
        new_descriptor.update({
            "pack_id": new_pack_id,
            "asset_path": relative_asset,
            "course_id": course_id,
            "asset_sha256": digest,
        })
        upgraded_catalog["packs"][target_index] = new_descriptor
        if upgraded_catalog["categories"] != raw_catalog["categories"]:
            raise ValueError("Single-pack upgrade changed category metadata/order")
        if upgraded_catalog["lectures"] != raw_catalog["lectures"]:
            raise ValueError("Single-pack upgrade changed lecture metadata/order")
        for index, descriptor in enumerate(upgraded_catalog["packs"]):
            if index != target_index and descriptor != raw_catalog["packs"][index]:
                raise ValueError("Single-pack upgrade changed an unrelated descriptor")

        ordered_old_atoms = sorted(raw_catalog["atoms"], key=lambda item: int(item["order_index"]))
        target_positions = [
            index for index, item in enumerate(ordered_old_atoms)
            if item["pack_id"] == old_pack_id
        ]
        insertion_index = target_positions[0]
        unchanged_atoms = [item for item in ordered_old_atoms if item["pack_id"] != old_pack_id]
        removed_before = sum(1 for position in target_positions if position < insertion_index)
        insertion_index -= removed_before
        merged_atoms = (
            unchanged_atoms[:insertion_index]
            + [dict(item) for item in new_records]
            + unchanged_atoms[insertion_index:]
        )
        for order_index, item in enumerate(merged_atoms):
            item["order_index"] = order_index
        upgraded_catalog["atoms"] = merged_atoms
        upgraded_catalog["default_pack_id"] = new_pack_id
        identity_payload = copy.deepcopy(upgraded_catalog)
        identity_payload["catalog_id"] = ""
        upgraded_catalog["catalog_id"] = stable_id(
            "catalog",
            sha256_json(identity_payload),
            length=24,
        )
        _validate_installed_catalog(upgraded_catalog)

        write_json(staged_catalog, upgraded_catalog)
        if read_json(staged_catalog) != upgraded_catalog:
            raise ValueError("Staged upgraded catalog failed round-trip verification")

        newly_installed = _publish_immutable_pack(staged_pack, destination)
        published_manifest, published_digest, published_course_id, published_records = (
            _fully_verify_new_pack(
                destination,
                expected_digest=digest,
                expected_manifest=manifest,
            )
        )
        if (
            published_manifest != manifest
            or published_digest != digest
            or published_course_id != course_id
            or published_records != new_records
        ):
            raise ValueError("Published new pack does not exactly match its verified source")

        # Recheck all unchanged bytes immediately before moving the commit
        # pointer, and refuse to clobber a concurrently modified catalog.
        final_legacy_verification = _verify_committed_legacy_assets(
            assets_root=assets_root,
            descriptors=unchanged_descriptors,
        )
        if final_legacy_verification != legacy_verification:
            raise ValueError("Unchanged legacy asset set changed during upgrade")
        if (
            catalog_path.is_symlink()
            or not catalog_path.is_file()
            or sha256_bytes(catalog_path.read_bytes()) != original_catalog_sha256
        ):
            raise ValueError("Installed Android catalog changed concurrently during upgrade")

        os.replace(staged_catalog, catalog_path)
        catalog_replaced = True
        try:
            committed = read_json(catalog_path)
            _validate_installed_catalog(committed)
            if committed != upgraded_catalog:
                raise ValueError("Committed upgraded catalog failed final verification")
        except Exception:
            _restore_catalog_bytes(catalog_path, original_catalog_bytes, stage_root)
            catalog_replaced = False
            raise

        committed_sha256 = sha256_bytes(catalog_path.read_bytes())
        return {
            "status": "UPGRADED",
            "operation": "single_pack_catalog_upgrade",
            "catalog": str(catalog_path),
            "catalog_id_before": raw_catalog["catalog_id"],
            "catalog_id": upgraded_catalog["catalog_id"],
            "catalog_sha256_before": original_catalog_sha256,
            "catalog_sha256": committed_sha256,
            "lecture_id": lecture_id,
            "old_pack_id": old_pack_id,
            "new_pack_id": new_pack_id,
            "old_asset_path": old_descriptor["asset_path"],
            "new_asset_path": relative_asset,
            "new_asset_sha256": digest,
            "new_asset_publication": "INSTALLED" if newly_installed else "REUSED",
            "new_pack_verifications": ["SOURCE", "STAGED", "PUBLISHED"],
            "pack_count": len(upgraded_catalog["packs"]),
            "unchanged_pack_count": len(unchanged_descriptors),
            "unchanged_assets_sha256_verified": len(final_legacy_verification),
            "old_atom_count": len(old_target_atoms),
            "new_atom_count": len(new_records),
            "catalog_atom_count": len(merged_atoms),
            "default_pack_id": new_pack_id,
        }
    except Exception:
        # Before commit, the original catalog was never touched. If an unusual
        # exception occurs after os.replace but before final verification, put
        # its exact original bytes back before propagating the failure.
        if catalog_replaced:
            _restore_catalog_bytes(catalog_path, original_catalog_bytes, stage_root)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def install_android_catalog(
    *,
    catalog_path: Path,
    packaged_dir: Path,
    android_assets: Path,
) -> dict[str, Any]:
    """Stage, verify and catalog-last commit a complete Android content set.

    Packs are published under content-addressed names, so the currently
    installed catalog remains valid throughout the transaction. The catalog is
    the sole commit pointer and is atomically replaced only after every final
    pack byte has been re-verified. Existing unrelated assets are never
    removed or replaced.
    """

    raw_catalog = read_json(catalog_path)
    if not isinstance(raw_catalog, dict) or raw_catalog.get("schema_version") != "1.0":
        raise ValueError("Unsupported or invalid Android catalog")
    raw_descriptors = raw_catalog.get("packs")
    if not isinstance(raw_descriptors, list) or not raw_descriptors:
        raise ValueError("Android catalog contains no pack descriptors")
    if any(not isinstance(item, dict) for item in raw_descriptors):
        raise ValueError("Android catalog has a non-object pack descriptor")

    descriptors = [dict(item) for item in raw_descriptors]
    lecture_ids = [_required_text(item.get("lecture_id"), "lecture_id") for item in descriptors]
    pack_ids = [_required_text(item.get("pack_id"), "pack_id") for item in descriptors]
    if len(set(lecture_ids)) != len(lecture_ids):
        raise ValueError("Android catalog contains duplicate lecture IDs")
    if len(set(pack_ids)) != len(pack_ids):
        raise ValueError("Android catalog contains duplicate pack IDs")
    if any(not _SAFE_ID_PATTERN.fullmatch(item) for item in lecture_ids):
        raise ValueError("Android catalog lecture_id is unsafe as a pack filename")

    packaged_dir = packaged_dir.resolve()
    if not packaged_dir.is_dir():
        raise ValueError(f"Packaged directory does not exist: {packaged_dir}")
    source_paths = sorted(packaged_dir.glob("*.dlpack"), key=lambda item: item.name)
    if any(item.is_symlink() or not item.is_file() for item in source_paths):
        raise ValueError("Packaged catalog inputs must be regular, non-symlink files")
    expected_source_names = {f"{lecture_id}.dlpack" for lecture_id in lecture_ids}
    actual_source_names = {item.name for item in source_paths}
    if actual_source_names != expected_source_names:
        raise ValueError(
            "Packaged/catalog coverage mismatch: "
            f"missing={sorted(expected_source_names - actual_source_names)}, "
            f"extra={sorted(actual_source_names - expected_source_names)}"
        )
    source_by_name = {item.name: item for item in source_paths}

    android_assets.mkdir(parents=True, exist_ok=True)
    assets_root = android_assets.resolve()
    content_root = assets_root / "content"
    pack_root = content_root / "packs"
    stage_root = assets_root / f".catalog-install-{uuid.uuid4().hex}.tmp"
    stage_pack_root = stage_root / "packs"
    stage_pack_root.mkdir(parents=True)

    staged: list[dict[str, Any]] = []
    installed_catalog = copy.deepcopy(raw_catalog)
    try:
        for descriptor in descriptors:
            lecture_id = str(descriptor["lecture_id"])
            source = source_by_name[f"{lecture_id}.dlpack"]
            _manifest, digest = _reconcile_pack(path=source, descriptor=descriptor)
            digest_hex = digest.removeprefix("sha256:")
            installed_name = f"{source.stem}.{digest_hex}.dlpack"
            relative_asset = PurePosixPath("content", "packs", installed_name).as_posix()
            staged_pack = stage_pack_root / installed_name
            shutil.copyfile(source, staged_pack)
            _reconcile_pack(path=staged_pack, descriptor=descriptor)
            staged.append({
                "descriptor": descriptor,
                "source": source,
                "staged": staged_pack,
                "destination": pack_root / installed_name,
                "asset_path": relative_asset,
                "asset_sha256": digest,
            })

        rewritten_descriptors: list[dict[str, Any]] = []
        installed_path_by_pack_id = {
            str(item["descriptor"]["pack_id"]): str(item["asset_path"])
            for item in staged
        }
        if len(installed_path_by_pack_id) != len(descriptors):
            raise ValueError("Catalog pack IDs are not one-to-one with staged assets")
        for original in raw_descriptors:
            rewritten = dict(original)
            rewritten["asset_path"] = installed_path_by_pack_id[str(original["pack_id"])]
            rewritten_descriptors.append(rewritten)
        installed_catalog["packs"] = rewritten_descriptors
        if len({item["asset_path"] for item in rewritten_descriptors}) != len(staged):
            raise ValueError("Installed catalog asset paths are not unique")
        if {
            item["pack_id"] for item in rewritten_descriptors
        } != {
            item["descriptor"]["pack_id"] for item in staged
        }:
            raise ValueError("Installed catalog does not reference the exact staged pack set")

        staged_catalog_path = stage_root / "catalog.json"
        write_json(staged_catalog_path, installed_catalog)
        if read_json(staged_catalog_path) != installed_catalog:
            raise ValueError("Staged Android catalog failed round-trip verification")

        newly_installed = 0
        reused = 0
        for item in staged:
            if _publish_immutable_pack(item["staged"], item["destination"]):
                newly_installed += 1
            else:
                reused += 1

        # Final-byte verification happens after publication and before the
        # catalog commit pointer moves.
        for item in staged:
            _reconcile_pack(path=item["destination"], descriptor=item["descriptor"])
        final_paths = {
            PurePosixPath(str(item["asset_path"])).as_posix()
            for item in staged
        }
        catalog_paths = {
            PurePosixPath(str(item["asset_path"])).as_posix()
            for item in installed_catalog["packs"]
        }
        if final_paths != catalog_paths or len(final_paths) != len(source_paths):
            raise ValueError("Final Android pack count/path reconciliation failed")

        content_root.mkdir(parents=True, exist_ok=True)
        catalog_destination = content_root / "catalog.json"
        os.replace(staged_catalog_path, catalog_destination)
        if read_json(catalog_destination) != installed_catalog:
            raise ValueError("Committed Android catalog failed final verification")
        return {
            "catalog": str(catalog_destination.resolve()),
            "catalog_sha256": sha256_bytes(catalog_destination.read_bytes()),
            "pack_count": len(staged),
            "newly_installed": newly_installed,
            "reused": reused,
            "packs": [
                {
                    "pack_id": item["descriptor"]["pack_id"],
                    "lecture_id": item["descriptor"]["lecture_id"],
                    "asset_path": item["asset_path"],
                    "asset_sha256": item["asset_sha256"],
                }
                for item in staged
            ],
        }
    finally:
        # This directory is created and owned exclusively by this invocation;
        # no pre-existing Android asset is ever removed by cleanup.
        shutil.rmtree(stage_root, ignore_errors=True)
