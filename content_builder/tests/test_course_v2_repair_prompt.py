from __future__ import annotations

from deeplock_content.util import PROMPT_ROOT


def test_course_v2_repair_prompt_has_bounded_lossless_pedagogy_contract():
    prompt = (PROMPT_ROOT / "course-v2" / "repair-system.txt").read_text(
        encoding="utf-8"
    )
    lower = prompt.lower()

    assert "180-320" in prompt
    assert "hard total is\n  450 words" in prompt
    assert "entire lesson" in lower
    assert "all substantive knowledge" in lower
    assert "source boundaries exactly" in lower
    assert "preserve every required stable id exactly" in lower
    assert "one central concept or skill" in lower
    assert "fix every supplied review issue" in lower
    assert "never narrate the\n  source" in lower
    assert "process_steps must contain 2-8" in prompt
    assert "at least 2 ordered\n  reasoning steps" in lower
    assert "observable capability" in lower
    assert "sau bài này, bạn có thể <observable action>" in lower
    assert "never pad, repeat an idea" in lower


def test_course_v2_repair_prompt_keeps_quiz_payload_compact():
    prompt = (PROMPT_ROOT / "course-v2" / "repair-system.txt").read_text(
        encoding="utf-8"
    ).lower()

    assert "exactly five questions" in prompt
    assert "exactly four distinct options a-d" in prompt
    assert "stem is at most 28 words" in prompt
    assert "option at most 18 words" in prompt
    assert "option rationale\n  at most 28 words" in prompt
    assert "question explanation at most 45 words" in prompt
