from __future__ import annotations

import pytest
import typer

from deeplock_content import cli
from deeplock_content.util import write_json


def test_package_missing_review_uses_ai_review_required_exit_code(valid_pack, tmp_path):
    write_json(tmp_path / "candidate.json", valid_pack.model_dump(mode="json", by_alias=True))
    try:
        cli.package(tmp_path, tmp_path / "never.dlpack", allow_legacy_no_contract=True)
    except typer.Exit as error:
        assert error.exit_code == 8
    else:
        raise AssertionError("package unexpectedly accepted a missing review")
    assert not (tmp_path / "never.dlpack").exists()


def test_package_requires_contract_by_default(valid_pack, tmp_path):
    write_json(tmp_path / "candidate.json", valid_pack.model_dump(mode="json", by_alias=True))
    with pytest.raises(typer.Exit) as caught:
        cli.package(tmp_path, tmp_path / "never.dlpack")
    assert caught.value.exit_code == 7
    assert not (tmp_path / "never.dlpack").exists()
