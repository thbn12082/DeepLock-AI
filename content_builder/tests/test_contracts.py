from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from jsonschema import Draft202012Validator

from deeplock_content.util import SCHEMA_ROOT, load_schema, read_json, responses_strict_schema


def test_all_schemas_are_valid_and_closed():
    schemas = list(SCHEMA_ROOT.glob("*.schema.json"))
    assert len(schemas) >= 10
    for path in schemas:
        schema = read_json(path)
        Draft202012Validator.check_schema(schema)
        assert schema.get("additionalProperties") is False


def test_android_manifest_has_no_network_permission():
    root = ET.parse(SCHEMA_ROOT.parent / "android" / "app" / "src" / "main" / "AndroidManifest.xml").getroot()
    android_name = "{http://schemas.android.com/apk/res/android}name"
    tools_node = "{http://schemas.android.com/tools}node"
    forbidden = {"android.permission.INTERNET", "android.permission.ACCESS_NETWORK_STATE"}
    declarations = [
        item.get(android_name)
        for item in root.findall("uses-permission")
        if item.get(tools_node) != "remove"
    ]
    assert forbidden.isdisjoint(declarations)


def test_responses_schema_removes_unsupported_unique_items():
    schema = responses_strict_schema(load_schema("atom-content.schema.json"))
    assert "uniqueItems" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    lesson = schema["properties"]["lesson"]
    assert "illustration_source_ref_ids" in lesson["required"]


def test_illustration_schema_is_flat_png_or_webp_contract():
    schema = load_schema("illustration-asset.schema.json")
    assert schema["properties"]["mime_type"]["enum"] == ["image/png", "image/webp"]
    assert "/" not in schema["properties"]["asset_member"]["pattern"]
