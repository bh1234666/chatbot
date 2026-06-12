import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_meta_judge_state_skips_after_repeated_declines():
    from app.core.meta_judge_state import (
        record_cross_llm_outcome,
        reset_cross_llm_outcomes,
        should_skip_cross_llm,
    )

    reset_cross_llm_outcomes()
    for _ in range(4):
        record_cross_llm_outcome("normal", False)

    should_skip, fp_rate = should_skip_cross_llm("normal")

    assert should_skip
    assert fp_rate == 1.0


def test_meta_judge_state_requires_minimum_samples():
    from app.core.meta_judge_state import (
        record_cross_llm_outcome,
        reset_cross_llm_outcomes,
        should_skip_cross_llm,
    )

    reset_cross_llm_outcomes()
    for _ in range(3):
        record_cross_llm_outcome("high", False)

    assert should_skip_cross_llm("high") == (False, 0.0)


def test_fallback_plan_reports_system_error_honestly():
    from app.core.plan_helpers import fallback_plan_from_user

    plan = fallback_plan_from_user("研究压缩算法", "JSON 重写失败")

    assert "did not reliably complete" in plan.intent
    assert "本轮未可靠完成" in plan.intent
    assert any("user-facing terms" in point and "用户能理解" in point for point in plan.key_points)
    assert any("retry or continuation" in point and "重试或继续" in point for point in plan.key_points)
    assert any("neutral wording" in item and "不声称已审阅" in item for item in plan.avoid)
    assert not any("脑子短路" in point for point in plan.key_points)
    assert not any("internal rounds" in point for point in plan.key_points)
    assert not any("Implying work" in item for item in plan.avoid)


def test_build_recall_hint_prefers_configured_layer():
    from app.core.plan_helpers import build_recall_hint
    from app.schemas.api import TendencyAnalysis

    tendency = TendencyAnalysis(
        tendencies={"查历史": 0.9},
        rationale="needs recall",
        needs_recall=True,
        recall_topics=["项目X"],
        recall_layers=["cold"],
    )

    hint = build_recall_hint(tendency)

    assert "项目X" in hint
    assert "expand_cold" in hint
    assert "not a required route" in hint
    assert "first three tool calls" not in hint


def test_build_recall_hint_is_factual_without_topics():
    from app.core.plan_helpers import build_recall_hint
    from app.schemas.api import TendencyAnalysis

    tendency = TendencyAnalysis(
        tendencies={"严肃询问": 0.8},
        rationale="possibly historical",
        needs_recall=True,
        recall_topics=[],
        recall_layers=[],
    )

    hint = build_recall_hint(tendency)

    assert "coarse fact" in hint
    assert "Topic-like memory guesses are not evidence" in hint
    assert "first three tool calls" not in hint
