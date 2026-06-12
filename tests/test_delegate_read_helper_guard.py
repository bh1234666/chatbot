from __future__ import annotations

import pytest

from app.llm.tools.delegate import _detect_helper_produced_inputs, _sanitize_and_validate_tasks


@pytest.mark.parametrize(
    "prompt, input_files, expected_outputs, expected_match",
    [
        (
            "Read the staged framework_contract.md and rbt_analysis.md",
            ["framework_contract.md", "rbt_analysis.md", "rbt.csv"],
            [],
            ["framework_contract.md", "rbt_analysis.md"],
        ),
        (
            "Inspect _env/docs/algorithm_inventory.md and _env/docs/section_summary.md",
            [],
            [],
            ["_env/docs/algorithm_inventory.md", "_env/docs/section_summary.md"],
        ),
        (
            "Read user-uploaded report.pdf",
            ["report.pdf"],
            [],
            [],
        ),
        (
            "Produce framework_contract.md from this spec",
            [],
            ["framework_contract.md"],
            [],
        ),
    ],
)
def test_detect_helper_produced_inputs(prompt, input_files, expected_outputs, expected_match):
    out = _detect_helper_produced_inputs(prompt, input_files, expected_outputs)
    for tok in expected_match:
        assert tok in out
    if not expected_match:
        assert out == []


@pytest.mark.asyncio
async def test_sanitize_reports_read_helper_against_helper_produced_to_guard(tmp_path):
    args = {
        "tasks": [
            {
                "task_id": "read_analyses",
                "kind": "read",
                "prompt": "Read framework_contract.md and rbt_analysis.md so we can decide.",
                "input_files": ["framework_contract.md", "rbt_analysis.md"],
                "expected_outputs": ["read_evidence.txt"],
            }
        ]
    }
    result = await _sanitize_and_validate_tasks(
        args,
        main_workspace=str(tmp_path),
        archive_id="arch_test",
        group_id="grp_test",
        user_id="usr_test",
    )
    assert not isinstance(result, str)
    observations = result[0].get("guard_observations") or []
    fact = next(
        item for item in observations
        if item.get("issue") == "read_helper_targets_helper_produced_artifacts"
    )
    blocked = fact.get("inputs") or []
    assert any("framework_contract" in b for b in blocked)
    assert any("rbt_analysis" in b for b in blocked)
    assert "guard should decide" in fact.get("details", "")


@pytest.mark.asyncio
async def test_sanitize_allows_resume_for_read_helper(tmp_path):
    """resume=true means the read helper is continuing prior work; do not block."""
    args = {
        "tasks": [
            {
                "task_id": "read_user_pdf",
                "kind": "read",
                "resume": True,
                "prompt": "Continue reading framework_contract.md if needed.",
                "input_files": ["framework_contract.md"],
                "expected_outputs": ["read_evidence.txt"],
            }
        ]
    }
    result = await _sanitize_and_validate_tasks(
        args,
        main_workspace=str(tmp_path),
        archive_id="arch_test",
        group_id="grp_test",
        user_id="usr_test",
    )
    if isinstance(result, str):
        parsed = json.loads(result)
        assert parsed.get("error") != "read_helper_targets_helper_produced_artifacts"
