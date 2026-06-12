from app.core.orchestrator import (
    _plan_has_closed_completion_evidence,
    _should_continue_incomplete_complex_plan,
)
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


def test_closed_completion_evidence_detects_complete_delegate_outputs():
    plan = ResponsePlan(
        intent="Complete slice_rb_tree artifacts",
        key_points=[
            "slice_rb_tree/rb_tree.c created",
            "slice_rb_tree/rb_tree.h created",
            "slice_rb_tree/benchmark_rb.csv created",
            "slice_rb_tree/analysis_rb.md created",
        ],
        tone="rigorous-controlled",
        length_hint="medium",
        internal_note=(
            "slice_rb_tree completed and verified: 4 artifacts applied to project. "
            "task_ok=true."
        ),
        deliverables=[
            "slice_rb_tree/rb_tree.c",
            "slice_rb_tree/rb_tree.h",
            "slice_rb_tree/benchmark_rb.csv",
            "slice_rb_tree/analysis_rb.md",
        ],
    )
    tool_results = {
        "task_plan": (
            '{"terminal_reason": "completed", "ok": true, '
            '"outputs_complete": true, "outputs_missing": []}'
        ),
        "delegate": (
            '{"delivered_count": 4, "outputs_complete": true, '
            '"quality_warnings": []}'
        ),
    }

    assert _plan_has_closed_completion_evidence(
        plan,
        helper_excerpts={},
        main_tool_results=tool_results,
    )


def test_read_only_analysis_with_concrete_findings_does_not_upgrade_on_gap_words():
    plan = ResponsePlan(
        intent="对 14 个核心文件的缓存与上下文机制做只读审计，给出优化点和风险。",
        key_points=[
            "M1 Stable Prefix: app/core/orchestrator_prompts.py L120-L307 构建固定 system 前缀。",
            "M2 Dynamic Context: app/core/context.py L1240-L1330 将动态证据放入 user tail。",
            "M3 Tool Schema Slimming: app/core/toolchain_cache.py L18 限制工具描述长度。",
            "M4 Round3 Evidence Passing: app/core/context.py L1850-L1910 传递 Round2 证据。",
            "14/14 文件全覆盖，包含 app/core/context.py 与 tests/test_prompt_cache_observer.py。",
            "O1: tool schema 描述可继续压缩，但需验证语义保留。",
            "R1: 仅有被动观察，缓存破坏需要事后通过报告发现。",
        ],
        tone="rigorous-controlled",
        length_hint="long",
        internal_note=(
            "task_ok=true; 14 files all covered via env_read/env_search with line evidence; "
            "4 mechanisms + 3 opts + 3 risks; no deliverable files; read-only complete"
        ),
        deliverables=[],
    )

    should_continue, reason = _should_continue_incomplete_complex_plan(
        plan,
        user_message=(
            "Read-only long toolchain cache probe. Do not modify files. "
            "Summarize in Chinese: 1) how stable prefix, dynamic context, "
            "tool schema slimming, and Round3 evidence passing work; "
            "2) three likely remaining cache/context optimization points; "
            "3) any warnings or correctness risks. This is analysis only; no code changes."
        ),
        helper_excerpts={},
        main_tool_results={
            "env_read": (
                "Test fixtures mention partial coverage, missing files, and blocked helpers, "
                "but these are evidence examples rather than current task gaps."
            )
        },
        final_msgs=[{"role": "tool", "content": "partial coverage example"}] * 5,
    )

    assert should_continue is False
    assert reason == ""


def test_completed_triage_plan_with_uppercase_pass_does_not_continue_upgrade():
    """Regression for t3-msg-inbox-triage 20260610_112337.

    A finished plan saying "All 3 verifiers PASS ... draft text prepared for
    review" was treated as preparatory because marker matching was
    case-sensitive and "prepared" matched the bare "prepare" marker, forcing a
    hard-round rerun of a completed task (~200s wasted).
    """
    from app.core.orchestrator import _plan_looks_preparatory

    plan = ResponsePlan(
        intent="Classify all 8 inbox emails and draft urgent replies without sending",
        key_points=[
            "All 8 emails classified: 2 urgent, 1 phishing flag, 2 can-wait, 3 noise",
            "All 3 verifiers PASS: verify_all_classified.py ✓, verify_drafts_for_urgent.py ✓, verify_phishing_flagged.py ✓",
            "Nothing sent — only draft text prepared for review",
        ],
        tone="warm-curious",
        length_hint="long",
        internal_note="All 3 verifiers PASS; triage_report.md applied to project root",
        deliverables=["triage_report.md"],
    )

    assert _plan_looks_preparatory(plan) is False

    should_continue, reason = _should_continue_incomplete_complex_plan(
        plan,
        user_message="go through my inbox, classify each message and draft replies for urgent ones",
        helper_excerpts={},
        main_tool_results={},
        final_msgs=[{"role": "tool", "content": "..."}] * 10,
    )
    assert should_continue is False
    assert reason == ""


def test_genuinely_preparatory_plan_still_detected():
    from app.core.orchestrator import _plan_looks_preparatory

    plan = ResponsePlan(
        intent="Fix the build",
        key_points=[
            "Next step: inspect the failing module and run the test suite",
            "尚未修改任何文件",
        ],
        tone="rigorous-controlled",
        length_hint="short",
        internal_note="preparing the change plan",
        deliverables=[],
    )

    assert _plan_looks_preparatory(plan) is True


def test_verifier_pass_counts_as_closed_completion_evidence():
    plan = ResponsePlan(
        intent="Email triage",
        key_points=["All 3 verifier scripts pass"],
        tone="warm-curious",
        length_hint="short",
        internal_note="triage_report.md applied",
        deliverables=["triage_report.md"],
    )

    assert _plan_has_closed_completion_evidence(
        plan, helper_excerpts={}, main_tool_results={}
    ) is True
