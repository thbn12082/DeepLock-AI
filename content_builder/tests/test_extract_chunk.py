from __future__ import annotations

from deeplock_content.chunking import build_corpus
from deeplock_content.extractors import extract_inputs
from deeplock_content.util import REPO_ROOT


def test_fixture_extract_and_chunk_is_deterministic():
    path = REPO_ROOT / "fixtures" / "gradient_descent.md"
    first = build_corpus(extract_inputs(path))
    second = build_corpus(extract_inputs(path))
    assert first.source_hash == second.source_hash
    assert [item.chunk_id for item in first.chunks] == [item.chunk_id for item in second.chunks]
    assert len(first.chunks) >= 4
    assert all(item.source_ref.anchor_end == len(item.normalized_text) for item in first.chunks)

