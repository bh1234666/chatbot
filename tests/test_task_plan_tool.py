import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_task_plan_retains_constraint_boundary_and_source_assumption_facts():
    from app.core import agent_state
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "test_constraint_boundary_retention"
    agent_state.reset_trace(trace_id)

    first = json.loads(await handle_task_plan({
        "trace_id": trace_id,
        "action": "update",
        "goal": "Check whether a plan fits constraints",
        "key_points": [
            "Budget: source-provided service is $220/unit x3, so use $660 as the primary conservative cost fact",
            "Access: source_flag_supported=false in source, limited-scope-only is a workaround",
        ],
        "current_stage": "audit",
    }))

    assert first["ok"] is True
    assert "retained_constraint_boundary_facts" in first
    assert any("$220/unit x3" in item for item in first["contract"]["risks"])
    assert any("source_flag_supported=false" in item for item in first["contract"]["risks"])

    second = json.loads(await handle_task_plan({
        "trace_id": trace_id,
        "action": "update",
        "goal": "Summarize latest check",
        "key_points": [
            "Latest summary says the plan generally fits",
        ],
        "current_stage": "report",
    }))

    assert second["ok"] is True
    assert any("$220/unit x3" in item for item in second["contract"]["risks"])
    assert any("source_flag_supported=false" in item for item in second["contract"]["risks"])
