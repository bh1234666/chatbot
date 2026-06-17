from app.schemas.api import ResponsePlan


def test_round2_route_corrections_never_downgrade_to_easy() -> None:
    from app.core.orchestrator_entry import _apply_round2_route_corrections

    plan = ResponsePlan(
        intent="x",
        key_points=[],
        tone="normal",
        length_hint="short",
        round2_complexity="medium",
        round2_needs_tools=True,
        round2_needs_recall=False,
    )

    complexity, needs_tools, needs_recall, changed = _apply_round2_route_corrections(
        plan,
        current_complexity="hard",
        current_needs_tools=False,
        current_needs_recall=True,
    )

    assert complexity == "medium"
    assert needs_tools is True
    assert needs_recall is False
    assert changed["complexity"] is True
    assert changed["needs_tools"] is True
    assert changed["needs_recall"] is True


def test_round2_route_corrections_ignore_easy() -> None:
    from app.core.orchestrator_entry import _apply_round2_route_corrections

    plan = ResponsePlan(
        intent="x",
        key_points=[],
        tone="normal",
        length_hint="short",
        round2_complexity=None,
        round2_needs_tools=None,
        round2_needs_recall=None,
    )
    setattr(plan, "round2_complexity", "easy")

    complexity, needs_tools, needs_recall, changed = _apply_round2_route_corrections(
        plan,
        current_complexity="hard",
        current_needs_tools=False,
        current_needs_recall=False,
    )

    assert complexity == "hard"
    assert needs_tools is False
    assert needs_recall is False
    assert changed == {"complexity": False, "needs_tools": False, "needs_recall": False}
