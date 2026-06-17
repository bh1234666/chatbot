from app.schemas.api import ResponsePlan


def _plan(*, key_points: list[str], intent: str = "确认无需修改") -> ResponsePlan:
    return ResponsePlan(
        intent=intent,
        key_points=key_points,
        tone="normal",
        length_hint="short",
    )


def test_recall_no_action_without_tool_or_evidence_forces_upgrade() -> None:
    from app.core.orchestrator import _enforce_round2_recall_no_action_evidence

    plan = _plan(key_points=["版本一已经包含 PCB 设计内容，无需进一步修改。"])

    changed = _enforce_round2_recall_no_action_evidence(
        plan,
        needs_recall=True,
        recall_tool_count=0,
        think=False,
    )

    assert changed is True
    assert plan.upgrade_to_hard is True
    assert plan.upgrade_to_veryhard is False
    assert plan.round2_needs_tools is True
    assert plan.round2_needs_recall is True
    assert "Missing verification" in plan.key_points[0]


def test_recall_no_action_with_compared_evidence_is_allowed() -> None:
    from app.core.orchestrator import _enforce_round2_recall_no_action_evidence

    plan = _plan(
        key_points=[
            "版本一原文摘录: 已包含 PCB 设计（板级设计）、调试与验证等内容。",
            "基于上述证据，版本一已经包含 PCB 设计内容，无需进一步修改。",
        ],
    )

    changed = _enforce_round2_recall_no_action_evidence(
        plan,
        needs_recall=True,
        recall_tool_count=0,
        think=False,
    )

    assert changed is False
    assert plan.upgrade_to_hard is False
    assert plan.round2_needs_recall is None


def test_recall_action_plan_without_no_action_claim_is_unchanged() -> None:
    from app.core.orchestrator import _enforce_round2_recall_no_action_evidence

    plan = _plan(
        intent="修改版本一",
        key_points=["需要在版本一中新增 PCB 设计（板级设计）内容。"],
    )

    changed = _enforce_round2_recall_no_action_evidence(
        plan,
        needs_recall=True,
        recall_tool_count=0,
        think=False,
    )

    assert changed is False
    assert plan.upgrade_to_hard is False
    assert plan.round2_needs_recall is None
