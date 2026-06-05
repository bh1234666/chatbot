from app.core.orchestrator import _should_continue_incomplete_complex_plan
from app.schemas.api import ResponsePlan


def test_incomplete_complex_plan_does_not_override_closed_helper_evidence():
    plan = ResponsePlan(
        intent="Identify cache gate functions",
        key_points=[
            "All requested functions confirmed",
            "Tests include strings such as missing cache stats but the task is complete",
        ],
        tone="rigorous-controlled",
        length_hint="short",
        internal_note="read helper PASS; no deliverable files; contract closed",
        deliverables=[],
    )
    helper_excerpts = {
        "cache_probe": (
            '{"task_ok": true, "helpers_still_running": 0, '
            '"terminal_reason": "completed", "report": "VERDICT: PASS; examples mention missing"}'
        )
    }

    should_continue, reason = _should_continue_incomplete_complex_plan(
        plan,
        user_message="Analyze the files and report the relevant functions",
        helper_excerpts=helper_excerpts,
        main_tool_results={},
        final_msgs=[{"role": "tool", "content": "missing appears inside a test fixture"}] * 8,
    )

    assert should_continue is False
    assert reason == ""
