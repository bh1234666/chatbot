"""
orchestrator_prompts 特征测试。从 orchestrator.py 抽出的 round2 系统提示词构建。

注:`_inject_dynamic_session_info` 内部惰性 import `app.config`(需运行时依赖),
本离线测试只覆盖纯函数 `_build_round2_system_prompts`;本地装好依赖后可补前者。
"""
import sys
import inspect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.orchestrator_prompts import _build_round2_system_prompts


def test_build_round2_returns_system_messages():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=True, needs_recall=False
    )
    assert isinstance(msgs, list) and len(msgs) > 0
    assert all(isinstance(m, dict) for m in msgs)
    assert msgs[0].get("role") == "system"


def test_build_round2_flag_combinations_dont_crash():
    for ic in (True, False):
        for doc in (True, False):
            for par in (True, False):
                for rec in (True, False):
                    out = _build_round2_system_prompts(
                        is_coding=ic, is_document=doc,
                        parallelizable=par, needs_recall=rec,
                    )
                    assert isinstance(out, list) and len(out) > 0


def test_round2_system_prompt_is_task_signal_independent_for_cache_reuse():
    variants = [
        _build_round2_system_prompts(
            is_coding=True, is_document=False, parallelizable=True, needs_recall=False
        ),
        _build_round2_system_prompts(
            is_coding=False, is_document=True, parallelizable=False, needs_recall=False
        ),
        _build_round2_system_prompts(
            is_coding=False, is_document=False, parallelizable=True, needs_recall=True
        ),
    ]

    for messages in variants[1:]:
        assert messages == variants[0]

    visible_text = "\n".join(message["content"] for message in variants[0])
    assert "## You are the orchestrator, not the worker" in visible_text
    assert "## Data, experiment, and visual evidence discipline" in visible_text
    assert "## Recall and indexed evidence discipline" in visible_text
    assert "## Coding task routing" in visible_text
    assert "## Document task routing" in visible_text
    assert "## Delegate API details" in visible_text
    assert "Apply this section when the dynamic routing facts" in visible_text


def test_round2_system_prompt_all_flag_combinations_are_identical():
    baseline = _build_round2_system_prompts(
        is_coding=False,
        is_document=False,
        parallelizable=False,
        needs_recall=False,
    )

    for ic in (True, False):
        for doc in (True, False):
            for par in (True, False):
                for rec in (True, False):
                    assert _build_round2_system_prompts(
                        is_coding=ic,
                        is_document=doc,
                        parallelizable=par,
                        needs_recall=rec,
                    ) == baseline


def test_document_round2_prompt_requires_csv_backed_document_qa():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=True, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Calculation-heavy documents" in visible_text
    assert "Numbers, labels, units, seeds, distributions" in visible_text
    assert "CSV/JSON/stdout" in visible_text
    assert "edit" in visible_text
    assert "charts/images" in visible_text
    assert "generate those resources first" in visible_text
    assert "request_resource" in visible_text
    assert "Weak dependencies" in visible_text
    assert "文档任务由 edit helper 完成" in visible_text


def test_round1_prompt_distinguishes_data_tasks_from_code_deliverables():
    from app.core.round_prompts import ROUND1_SYSTEM

    prompt = ROUND1_SYSTEM

    assert "core deliverable is reusable code" in prompt
    assert "Data querying, database inspection, calculations" in prompt
    assert "CSV/JSON/table/report generation" in prompt
    assert "temporary SQL/Python inspection probes are tool/data tasks rather than coding tasks" in prompt
    assert "unless the user asks to create or modify runnable code" in prompt


def test_round1_prompt_keeps_current_file_schema_as_tool_evidence_not_recall():
    from app.core.round_prompts import ROUND1_SYSTEM

    prompt = ROUND1_SYSTEM

    assert "Use the exact Chinese label strings above in `tendencies`" in prompt
    assert "Concrete current-workspace files, databases, logs, and verifier scripts are tool evidence, not memory" in prompt
    assert "Do not set `needs_recall=true` merely to learn the schema or contents of a named current file" in prompt
    assert "`.db`, `.csv`, `.json`, source file, or test script" in prompt


def test_round1_prompt_routes_followup_feasibility_as_evidence_check():
    from app.core.round_prompts import ROUND1_SYSTEM

    prompt = ROUND1_SYSTEM

    assert "really fits constraints such as budget, mobility, feasibility, risk" in prompt
    assert "These are evidence checks, not mere reassurance" in prompt
    assert "Follow-up feasibility or honesty checks about a recent task" in prompt


def test_round2_prompt_treats_create_request_as_imperative_not_lookup():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "create, generate, write, save, export, or deliver an artifact" in visible_text
    assert "missing targets imply creation or helper dispatch" in visible_text
    assert "Main-thread execution boundary" in visible_text
    assert "compact non-source inspection" in visible_text
    assert "Read-only orientation or explanation closes once the requested facts are evidenced" in visible_text
    assert "task_ok=true; no deliverable files; evidence sufficient; no upgrade" in visible_text
    assert "narrow mechanical apply/transfer/accounting after helper production" in visible_text
    assert "Source implementation, debugging, compilation loops, benchmarks, tests, and project-file edits belong to helpers" in visible_text
    assert "expected outputs, verification commands" in visible_text
    assert "Pass likely project paths through helper `input_files`" in visible_text
    assert "主线程负责派发" in visible_text


def test_round2_prompt_routes_ultra_large_file_coverage_to_helper_slices():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=True, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "one ultra-large file, long log, or long source material" in visible_text
    assert "fan out focused `read` or `file_summary` helpers" in visible_text
    assert "line ranges, chapters, pages, or natural sections" in visible_text
    assert "the main thread merges concise facts rather than absorbing the full body" in visible_text


def test_round2_prompt_preserves_explicit_user_evidence_constraints():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Explicit user-requested tools, evidence types, order constraints" in visible_text
    assert "validation commands" in visible_text
    assert "validation actions are acceptance facts" in visible_text
    assert "executable name, arguments, working directory" in visible_text
    assert "Run that exact command when available" in visible_text
    assert "ordinary stdout checks are text-output checks" in visible_text
    assert "helper acceptance checks" in visible_text
    assert "用户显式要求的工具、证据、顺序、验收命令和验收动作是契约事实" in visible_text


def test_round2_prompt_treats_assurance_followups_as_evidence_first():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=False, needs_recall=True
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Follow-up assurance wording" in visible_text
    assert "not automatically an edit request" in visible_text
    assert "flag, mark, classify, note, leave alone, leave untouched" in visible_text
    assert "first a comparison against existing plan, artifact, and verified evidence" in visible_text
    assert "If current evidence already satisfies the requirement" in visible_text
    assert "deliverables=[]" in visible_text
    assert "Modify or delegate edits only when evidence shows" in visible_text


def test_round2_prompt_preserves_constraint_fit_boundaries_before_reassurance():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=False, needs_recall=True
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "For feasibility, honesty, requirement-fit, budget, mobility, risk, or blocker checks" in visible_text
    assert "`key_points` must preserve the evidence boundary before reassurance" in visible_text
    assert "tight margin" in visible_text
    assert "source-level false/non-friendly/risky flag" in visible_text
    assert "workaround" in visible_text
    assert "fits but tight" in visible_text
    assert "partial fit" in visible_text
    assert "does not fit the full version" in visible_text
    assert "do not summarize the same situation as \"no blocker\", \"basically fine\", or \"nothing was hidden\"" in visible_text
    assert "source-level false/non-friendly/risky flag remains a partial-fit fact" in visible_text
    assert "do not rewrite it as fully friendly/safe" in visible_text
    assert "Use source-provided cost, duration, count, and scope assumptions as the primary conservative facts" in visible_text
    assert "optimistic reinterpretations or alternatives may be listed separately" in visible_text
    assert "must not replace the main fit verdict" in visible_text
    assert "约束/预算/风险审查先保留紧余量、变通、部分符合和不符合的事实边界" in visible_text
    assert "不能用乐观重解释替代源数据主结论" in visible_text


def test_round2_prompt_preserves_cost_units_with_duration_counts():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=False, needs_recall=True
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "source cost fields paired with counts, durations, quantities, nights, days" in visible_text
    assert "do not assume the note makes the cost a total" in visible_text
    assert "explicitly says total, package, included, or all-inclusive" in visible_text
    assert "conservative unit-price × count/duration calculation" in visible_text
    assert "Do not use outside plausibility claims to override project/source data" in visible_text
    assert "费用字段遇到数量/时长时保守乘算或说明歧义" in visible_text


def test_round2_prompt_keeps_ambiguous_source_facts_raw_in_helper_envelopes():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=True, needs_recall=True
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Helper envelopes should carry raw source facts and acceptance constraints" in visible_text
    assert "not unsupported main-thread interpretations" in visible_text
    assert "costs, units, quantities, durations, counts, booleans, or risk flags ambiguously" in visible_text
    assert "pass the raw field/value/note" in visible_text
    assert "ask the helper to compute or flag the ambiguity from evidence" in visible_text
    assert "do not pre-resolve it as total, per-unit, safe, friendly, or fully satisfied" in visible_text
    assert "helper envelope 传原始字段和验收约束" in visible_text


def test_round2_prompt_uses_search_evidence_for_symbol_migrations():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "identifier, API, schema, field, import, or contract migrations" in visible_text
    assert "use search/index evidence to discover impacted references" in visible_text
    assert "verify remaining old/new references after edits" in visible_text


def test_round2_prompt_does_not_close_readonly_when_verifier_reports_missing_artifact():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "read-only closure does not apply" in visible_text
    assert "verifier/check scripts" in visible_text
    assert "missing workspace/project content" in visible_text
    assert "do not mark `task_ok=true`" in visible_text
    assert "Verifier scripts and check commands are acceptance facts" in visible_text
    assert "where they read from" in visible_text
    assert "a chat-only answer cannot satisfy that check" in visible_text


def test_round2_prompt_prefers_verifier_over_repeated_helper_artifact_reads():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=True, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "helper-produced text/project artifact exactly matches" in visible_text
    assert "prefer apply/diff plus helper-run verifier evidence" in visible_text
    assert "over repeated main-thread reads or searches" in visible_text
    assert "Do not read or search the produced file merely to re-check helper-owned content" in visible_text
    assert "final intended applied state" in visible_text
    assert "a check before later applies is earlier-state evidence" in visible_text


def test_round2_prompt_distinguishes_data_aliases_from_source_fields():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=False, needs_recall=True
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "distinguish source fields, joined fields, derived values, and output aliases" in visible_text
    assert "SQL `AS` alias does not need to exist as a source-table column" in visible_text
    assert "claiming the whole schema is clean" in visible_text


def test_environment_prompt_mentions_reusable_written_project_state():
    import inspect
    from app.core import environment_prompt

    visible_text = (
        environment_prompt.ENVIRONMENT_PROMPT_ADDON
        + "\n"
        + inspect.getsource(environment_prompt.environment_round2_system_prompt)
    )

    assert "Reusable written deliverables" in visible_text
    assert "reports, explainers, guides, briefs, cited syntheses" in visible_text
    assert "not an automatic decision" in visible_text
    assert "compare inline-chat sufficiency against project/verifier visibility" in visible_text


def test_progress_message_prompt_hides_internal_ocr_by_default():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._gen_progress_message)
    assert "generate_intermediate_feedback" in src

    from app.core import intermediate_feedback

    prompt_src = intermediate_feedback.INTERMEDIATE_FEEDBACK_SYSTEM
    payload_src = inspect.getsource(intermediate_feedback._intermediate_feedback_user_payload)
    assert "mid-task update" in prompt_src
    assert "visible to the user" in prompt_src
    assert "persona feedback preference" in payload_src
    assert "Use outcome-level wording" in prompt_src
    assert "turn internal workflow into user-facing progress wording" in prompt_src
    assert "Do not expose internal process labels" in prompt_src
    assert "private orchestration names" in prompt_src
    assert "helper, delegate, toolchain, agent_state" not in prompt_src
    assert "Preserve technical task labels" in prompt_src
    assert "判断是否需要中途回复" in prompt_src


def test_debug_report_prompt_avoids_internal_status_jargon():
    from app.core import debug

    assert "Return one short Chinese phrase" in debug.DEBUG_REPORT_SYSTEM
    assert "Use user-friendly process wording" in debug.DEBUG_REPORT_SYSTEM
    assert "If a clean user-facing phrase is not possible" in debug.DEBUG_REPORT_SYSTEM
    assert "把内部事件压缩成一句用户可见的中文状态" in debug.DEBUG_REPORT_SYSTEM


def test_p85_ocr_guard_reads_current_tool_result_marker_case_insensitively():
    src = Path("app/core/orchestrator.py").read_text(encoding="utf-8")

    assert "str(_key).lower()" in src
    assert '"ocr" not in _key_l' in src
    assert "--- Raw result begins ---" in src
    assert "--- Raw result ends ---" in src
    assert "权威图像/文件识别文本摘录" in src
    assert "OCR 真实内容(必须如实引用" not in src


def test_round3_prompt_rewrites_internal_process_terms_to_deliverable_language():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="回答图片内容",
        key_points=["图片里的文字是 alpha, 疑似 beta 不确定"],
        tone="自然",
        length_hint="短",
    )
    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "图里写了什么",
        helper_reports_excerpt=[
            {"task_id": "ocr#1", "excerpt": "可确认内容: alpha"},
            {
                "task_id": "read_page",
                "excerpt": (
                    "helper report from _helpers_shared/fetch/page.txt says beta is uncertain; "
                    "producer-owned output from main process is producer_self_verified"
                ),
            },
        ],
    )
    system_text = msgs[0]["content"]
    assert "Internal terms such as OCR, TTS, routing labels" in system_text
    assert "outcome-level language" in system_text and "image text" in system_text
    assert "private evidence and transform them into natural persona-consistent wording" in system_text
    assert "Concept questions" in system_text and "OCR" in system_text
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")
    assert "Real work/tool evidence for this reply" in user_text
    assert "工作证据和工具结果是证据来源" in user_text
    assert "Current tool evidence source 1" in user_text
    assert "Work evidence source 2" in user_text
    assert "Evidence excerpt: read_page" not in user_text
    assert "read_page" not in user_text
    assert "ocr#1" not in user_text
    assert "Producer evidence" not in user_text
    assert "producer" not in user_text.lower()
    assert "main process" not in user_text.lower()
    assert "main thread" not in user_text.lower()
    assert "output_self_verified" in user_text
    assert "helper" not in user_text.lower()
    assert "_helpers_shared" not in user_text


def test_round3_plan_rendering_sanitizes_voice_internal_status_terms():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="tts_persona_guard blocked Round3 helper delivery",
        key_points=[
            "json.voice_delivery_final_review said helper/delegate voice_guard resource_required",
            "push=true is unsupported by the tts tool; do not automatically send voice",
        ],
        tone="自然",
        length_hint="短",
        avoid=["Do not expose task-quality guard or _helpers_shared/voice/report.txt"],
        callbacks=["Round2 delegate returned persona_guard"],
    )

    msgs = context.round3_messages(
        "你是一个自然说话的测试人设。",
        plan,
        "用户",
        "用语音回复我",
    )
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")

    forbidden = [
        "tts_persona_guard",
        "voice_delivery_final_review",
        "helper/delegate",
        "voice_guard",
        "persona_guard",
        "task-quality guard",
        "_helpers_shared",
        "json.",
        "Round2",
        "Round3",
    ]
    for term in forbidden:
        assert term not in user_text
    assert "voice/style check" in user_text or "voice delivery check" in user_text


def test_round3_prompt_distinguishes_data_aliases_from_schema_errors():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="Double-check data schema",
        key_points=["CSV verified"],
        tone="自然",
        length_hint="短",
    )
    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "If anything in the schema is weird, double-check before assuming.",
    )
    system_text = msgs[0]["content"]

    assert "distinguish source fields, joined fields, derived values, and output aliases" in system_text
    assert "Do not call an output alias a nonexistent source column error" in system_text
    assert "partially checked" in system_text


def test_round3_prompt_requires_explicit_constraint_feasibility_wording():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="Explain whether requested constraints fit",
        key_points=["Budget fits only if optional add-ons are excluded", "Mobility risk remains for the full route"],
        tone="自然",
        length_hint="短",
    )
    msgs = context.round3_messages(
        "You are a concise assistant.",
        plan,
        "User",
        "If anything doesn't actually fit the budget or constraints, tell me up front.",
    )
    system_text = msgs[0]["content"]

    assert "answer up front before details" in system_text
    assert "first substantive paragraph should state the verdict and boundary" in system_text
    assert "what fits, what does not fit or only fits partially" in system_text
    assert "remaining tradeoff/blocker" in system_text
    assert "include an explicit status phrase in the first substantive paragraph" in system_text
    assert "Cannot satisfy as written" in system_text
    assert "约束/预算/风险可行性先给正面判定和边界" in system_text


def test_round3_prompt_names_intentional_no_action_boundaries():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="Report suspicious or uncertain items without acting on them",
        key_points=[
            "A suspicious artifact was flagged from available evidence",
            "The artifact was left unchanged because direct action was not authorized",
        ],
        tone="自然",
        length_hint="短",
    )
    msgs = context.round3_messages(
        "You are a concise assistant.",
        plan,
        "User",
        "If anything looks suspicious, flag it and leave it alone.",
    )
    system_text = msgs[0]["content"]

    assert "intentionally leaves something untouched" in system_text
    assert "authorization boundary" in system_text
    assert "Will not modify/send it" in system_text
    assert "Left unchanged" in system_text
    assert "Nothing is blocked; no further action was needed" in system_text
    assert "existing evidence already satisfied the request" in system_text
    assert "按授权不处理、无需继续或缺验证时保留明确状态词" in system_text


def test_round3_dynamic_context_surfaces_no_action_boundary_facts():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="Confirm that the existing artifact already satisfies the follow-up constraint",
        key_points=[
            "The suspicious item was already flagged from verified evidence",
            "No new deliverables needed; existing work already satisfies this constraint",
            "No links were followed and nothing was sent",
        ],
        tone="concise",
        length_hint="short",
        deliverables=["triage_report.md"],
    )
    msgs = context.round3_messages(
        "You are a concise assistant.",
        plan,
        "User",
        "Anything that looks fishy, just flag it and don't touch it.",
    )
    system_text = msgs[0]["content"]
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")

    assert "Current Response Boundary Facts" not in system_text
    assert "Current Response Boundary Facts" in user_text
    assert "No new deliverables needed; existing work already satisfies this constraint" in user_text
    assert "No links were followed and nothing was sent" in user_text
    assert "no remaining blocker or missing item" in user_text
    assert "not an automatic instruction to refuse, continue, edit, or re-deliver" in user_text
    assert "最终回复显式说明阻塞/缺失状态和证据" in user_text


def test_round3_tool_evidence_strips_repeated_preamble_but_keeps_metadata():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="回答读取结果",
        key_points=["基于工具证据回答"],
        tone="准确",
        length_hint="短",
    )
    raw = (
        "This is an authoritative excerpt from a main-thread read/vision tool. Use the excerpt and metadata "
        "as evidence; do not guess beyond them or expose internal tool names to the user.\n\n"
        "主线程工具摘录是事实依据；截断部分视为未知，可按路径/行号恢复读取。\n\n"
        "Metadata:\n- path: app/core/context.py\n- start_line: 1\n- end_line: 80\n\n"
        "--- Raw result begins ---\n"
        + ("abcdef\n" * 400)
        + "--- Raw result ends ---"
    )

    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "总结读取结果",
        helper_reports_excerpt=[
            {"task_id": "🔍 主线程工具结果(权威) env_read #1", "excerpt": raw}
        ],
    )
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")

    assert "This is an authoritative excerpt from a main-thread" not in user_text
    assert "主线程工具摘录是事实依据" not in user_text
    assert "Metadata:" in user_text
    assert "- path: app/core/context.py" in user_text
    assert "--- Excerpt begins ---" in user_text
    assert "--- Raw result begins ---" not in user_text
    assert "...[截]" in user_text


def test_round3_no_delivery_does_not_hide_workspace_write_facts():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="汇报审计结果",
        key_points=["完成分析"],
        tone="准确",
        length_hint="短",
    )
    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "给出结果",
        helper_reports_excerpt=[
            {
                "task_id": "workspace_write#1",
                "excerpt": (
                    "Metadata:\n- action: workspace.write\n- path: evidence.txt\n- size: 12\n\n"
                    "--- Raw result begins ---\nA workspace file was written in this turn: evidence.txt.\n--- Raw result ends ---"
                ),
            }
        ],
        files=None,
    )
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")

    assert "Current tool evidence source 1" in user_text
    assert "workspace_write#1" not in user_text
    assert "A workspace file was written in this turn: evidence.txt" in user_text
    assert "No file will be sent to the user" in user_text
    assert "do not claim no files were modified" in user_text


def test_round3_prompt_preserves_structured_rankings_from_evidence():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="回答工程文件排行",
        key_points=[
            "Top files: app/llm/tools/delegate.py 190865; app/core/context.py 112871"
        ],
        tone="准确",
        length_hint="中",
    )
    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "输出最大的几个文件",
    )
    system_text = msgs[0]["content"]

    assert "For rankings, tables, or top-N lists" in system_text
    assert "project-relative paths" in system_text
    assert "keep every item identity and number intact" in system_text
    assert "排行表格保留相对路径" in system_text


def test_round3_prompt_separates_bot_identity_from_user_name():
    from datetime import datetime, timezone

    from app.core import context
    from app.schemas.api import HotMessage, ResponsePlan

    now = datetime.now(timezone.utc)
    plan = ResponsePlan(
        intent="回答身份问题",
        key_points=["用户问你是谁时按人设回答"],
        tone="自然",
        length_hint="短",
    )
    hot = [
        HotMessage(role="user", content="[CQ:at,qq=1] 你是谁", turn_id="t1", created_at=now),
        HotMessage(role="assistant", content="我是包涵呀，你忘了？", turn_id="t1", created_at=now),
    ]
    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "包涵",
        "[CQ:at,qq=1] 你是谁",
        hot_user=hot,
        light=False,
    )
    system_text = msgs[0]["content"]
    user_text = msgs[1]["content"]

    assert "The name before the current message is the speaker/user name, not your name" in system_text
    assert "If the user asks who you are, answer as your persona" in system_text
    assert "If the user asks who they are, answer about the user only when evidence supports it" in system_text
    assert "Historical slips where you used the user's name as your own are not identity facts" in system_text
    assert "包涵：[CQ:at,qq=1] 你是谁" in user_text


def test_round3_prompt_anchors_to_current_user_request_over_old_topic():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="回答当前新问题",
        key_points=["按当前发言回答，不延续旧主题"],
        tone="自然",
        length_hint="短",
    )
    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "别继续上个话题，只回答当前问题",
    )
    system_text = msgs[0]["content"]

    assert "Topic Anchor" in system_text
    assert "The current request has priority" in system_text
    assert "别继续上个话题" in msgs[1]["content"]
    assert "History, shared conversation, and background evidence are evidence sources" in system_text
    assert "not automatic current deliverables" in system_text


def test_round3_recent_group_messages_only_in_direct_light_false_path():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="回答当前问题",
        key_points=["当前问题优先"],
        tone="自然",
        length_hint="短",
    )
    recent_group = [
        {
            "created_at": "2026-06-07T10:00:00+08:00",
            "id": "m1",
            "user_id": "u1",
            "user_name": "旧用户",
            "content": "旧的很长任务输出，不应进入已走 Round2 的 Round3",
        }
    ]

    medium_msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "当前问题",
        light=True,
        recent_group_messages=recent_group,
    )
    direct_msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "当前问题",
        light=False,
        recent_group_messages=recent_group,
    )

    assert "## Recent Messages" not in medium_msgs[1]["content"]
    assert "旧的很长任务输出" not in medium_msgs[1]["content"]
    assert "## Recent Messages" in direct_msgs[1]["content"]
    assert "旧的很长任务输出" in direct_msgs[1]["content"]


def test_round3_completion_wording_keeps_coverage_gap_facts_visible():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="审计分析已完成并输出报告",
        key_points=[
            "已输出 evidence_cache_audit_v2.txt",
            "本次仅读取生产代码，未读取测试文件，未验证长测结果",
        ],
        tone="准确",
        length_hint="中",
    )

    msgs = context.round3_messages(
        "你是测试人格",
        plan,
        "用户",
        "继续",
    )
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")

    assert "Coverage Gap Facts" in user_text
    assert "本次仅读取生产代码" in user_text
    assert "Completion-state opening" not in user_text


def test_all_text_personas_have_identity_core():
    persona_dir = Path(__file__).parent.parent / "personas"
    persona_files = sorted(persona_dir.glob("*.md"))
    assert persona_files

    for path in persona_files:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("name:"), path.name
        if path.name == "environment.md":
            assert "## Identity" in text, path.name
            assert "If the user asks who you are" in text, path.name
            assert "you are bot" in text, path.name
            assert "local project" in text, path.name
            assert "真实目录" in text, path.name
        else:
            assert "## Identity" in text, path.name
            assert "If the user asks who you are" in text, path.name
            assert "If the user asks who they are" in text, path.name
            assert "Keep your own identity separate from the user's display name" in text, path.name
            assert "## Chinese Summary" in text, path.name


def test_digest_turn_prompt_keeps_internal_tool_terms_out_of_memory_summaries():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._digest_turn)
    assert "internal implementation terms" in src
    assert "outcome-level wording such as image text, audio file" in src
    assert "Preserve technical terms only for concept or troubleshooting questions" in src
    assert "keep unresolved requests as requests" in src
    assert "only state voice/audio delivery when the bot reply explicitly says it was sent or generated" in src


def test_round2_prompt_routes_file_reading_to_read_not_draw_or_edit():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=True, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "image clarity, visible text, screenshots, or visual document content" in visible_text
    assert "use `kind='read'`" in visible_text
    assert "`draw` is for generating or redrawing images from data" in visible_text


def test_round2_prompt_distinguishes_reading_concept_from_action():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE
    assert "First distinguish concept/troubleshooting questions from practical file reading" in prompt
    assert "Practical reading from a concrete text/image/PDF/Office file uses a `read` helper" in prompt
    assert "minimum evidence standard" in prompt
    assert "tier/cache/engine details" in prompt


def test_round2_prompt_preserves_structured_evidence_in_key_points():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE

    assert "Evidence in key_points" in prompt
    assert "For rankings, tables, or top-N lists" in prompt
    assert "preserve every requested item in evidence order" in prompt
    assert "project-relative paths, labels, and numeric values" in prompt
    assert "keep intermediate items as well as the first and last items" in prompt
    assert "keep paths at their evidence granularity" in prompt
    assert "user-facing plan inputs for Round3" in prompt
    assert "without internal routing labels" in prompt
    assert "helper/delegate/producer/background work" in prompt


def test_round2_prompt_does_not_force_weak_audit_findings():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE

    assert "helpers report text rather than writing evidence files" in prompt
    assert "key_points` must carry the actual answer facts" in prompt
    assert "Keep answer facts in `key_points` rather than completion checklist items" in prompt
    assert "For audits, reviews, optimizations, risks, or root-cause claims" in prompt
    assert "separate directly evidenced findings from hypotheses" in prompt
    assert "treat requested counts as ceilings" in prompt
    assert "direct caller/callee" in prompt
    assert "unchecked modules are only leads" in prompt
    assert "low-confidence hypotheses with missing verification" in prompt


def test_round3_prompt_preserves_audit_evidence_strength():
    from app.core.round_prompts import ROUND3_EVIDENCE_PRESENTATION_RULES

    prompt = ROUND3_EVIDENCE_PRESENTATION_RULES

    assert "preserve the plan's evidence strength" in prompt
    assert "Keep hypotheses and weak clues labeled" in prompt
    assert "direct implementation, caller/callee, or data-flow link" in prompt
    assert "label the rest as hypotheses" in prompt


def test_round3_prompt_preserves_source_proper_nouns_when_localizing():
    from app.core.round_prompts import ROUND3_EVIDENCE_PRESENTATION_RULES

    prompt = ROUND3_EVIDENCE_PRESENTATION_RULES

    assert "summarizing verified artifacts in another language" in prompt
    assert "keep source proper nouns" in prompt
    assert "labels, IDs, quoted strings, command names, and numeric fields" in prompt
    assert "Use a localized label only when the evidence provides that localized label" in prompt


def test_round3_prompt_rewrites_tts_guard_status_to_user_visible_result():
    from app.core.round_prompts import ROUND3_EVIDENCE_PRESENTATION_RULES

    prompt = ROUND3_EVIDENCE_PRESENTATION_RULES

    assert "persona_guard/voice_guard" in prompt
    assert "resource_required" in prompt
    assert "Use them only as hidden evidence" in prompt
    assert "was not sent as voice" in prompt
    assert "do not render the internal mechanism name" in prompt


def test_round3_prompt_requires_persona_consistent_internal_fact_rendering():
    from app.core.round_prompts import ROUND3_EVIDENCE_PRESENTATION_RULES

    prompt = ROUND3_EVIDENCE_PRESENTATION_RULES

    assert "would make sense for the assistant's persona" in prompt
    assert "helper, guard, Round, candidate, push flag, schema, JSON field" in prompt
    assert "voice_reply_file" in prompt
    assert "Replace routing/system words" in prompt
    assert "If voice generation failed or was skipped" in prompt


def test_round2_prompt_keeps_main_thread_as_contract_manager_for_long_source_material():
    from app.core import context

    # Slice mechanics were deduplicated into the orchestrator layer (both are
    # visible in the same Round2 context); the template keeps a pointer.
    prompt = context.ROUND2_SYSTEM_TEMPLATE
    layers = "\n".join(
        m["content"] for m in _build_round2_system_prompts(
            is_coding=True, is_document=True, parallelizable=True, needs_recall=True
        )
    )
    combined = prompt + "\n" + layers

    assert "Preserve the task contract throughout the toolchain" in prompt
    assert "Use `task_plan` for active-task goal/plan changes" in prompt
    assert "use `agent_state` for long or multi-helper work" in prompt
    assert "keep full extracted content in helper-owned evidence files" in prompt
    assert "compact coverage summaries, counts, section maps, line ranges" in prompt
    assert "ultra-large file, long log, or long source material" in prompt
    assert "line ranges, chapters, pages, or natural sections" in combined
    assert "coverage, gaps, evidence paths" in combined
    assert "For source-driven organization or expansion, preserve the user's coverage contract" in prompt
    assert "材料驱动整理或扩写时保留用户覆盖契约" in prompt


def test_round2_template_preserves_exact_validation_command_semantics():
    from app.core import context

    # Exact-command semantics were deduplicated into the orchestrator layer;
    # the template keeps acceptance-fact framing plus a pointer.
    prompt = context.ROUND2_SYSTEM_TEMPLATE
    layers = "\n".join(
        m["content"] for m in _build_round2_system_prompts(
            is_coding=True, is_document=True, parallelizable=True, needs_recall=True
        )
    )
    combined = prompt + "\n" + layers
    assert "validation actions are acceptance facts" in prompt
    assert "executable, arguments, working directory" in prompt
    assert "stdout/stderr behavior" in combined
    assert "Run that exact command when available" in combined
    assert "ordinary stdout checks are text-output checks" in combined
    assert "unless the contract explicitly says bytes, binary, or byte-for-byte" in combined


def test_round2_prompt_keeps_framework_contracts_structural():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE

    assert "It defines slots and acceptance, not the substantive content of those slots" in prompt
    assert "research claims, citations, conclusions, tables with final values" in prompt
    assert "正文、引用、结论、实验和最终文件交给后续分片 helper" in prompt


def test_round2_prompt_separates_history_from_current_mainline():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE

    assert "Maintain a current-task mainline" in prompt
    assert "Conversation history, recent activity, workspace listings" in prompt
    assert "historical filenames as old/context files" in prompt
    assert "task_plan(action=\"update\")" in prompt
    assert "historical task outputs" in prompt
    assert "framework contracts" in prompt


def test_round2_keeps_tool_schema_stable_instead_of_trimming_by_task_signal():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._round2)

    assert "_stable_round2_tools = _runtime_tools" in src
    assert "round2.tool_schema_stable" in src
    assert "_trimmed_tools" not in src
    assert "round2.tool_trim" not in src


def test_round2_keeps_toolchain_policy_in_system_and_request_anchor_in_user_tail():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._round2)
    anchor_src = inspect.getsource(orchestrator._build_active_task_contract_anchor)

    assert "## Toolchain Continuation" in src
    assert "_build_active_task_contract_anchor" in src
    assert "## Active Task Contract Anchor" in anchor_src
    assert "_append_round2_dynamic_user_tail" in src
    assert "Current user turn:" in anchor_src
    assert "Resolve the active task from the current user turn plus maintained task_plan" in anchor_src
    assert "latest user turn is a strong task fact" in anchor_src
    assert "prior plan snapshots" in anchor_src
    assert "当前主线任务不总等于最后一句话" in anchor_src

    dynamic_tail_src = inspect.getsource(orchestrator._append_round2_dynamic_user_tail)
    assert "without mutating the system prefix" in dynamic_tail_src
    assert '"role": "user"' in dynamic_tail_src


def test_active_task_anchor_preserves_explicit_current_turn_constraints():
    from app.core import orchestrator

    user_turn = (
        "There is a broken newsletter signup page. Use the browser tool to reproduce "
        "the bug in the host browser, fix the frontend code, and verify the form succeeds."
    )
    facts = orchestrator._explicit_current_turn_constraint_facts(user_turn)
    anchor = orchestrator._build_active_task_contract_anchor(current_user_turn=user_turn)

    assert facts
    assert any("Use the browser tool to reproduce" in fact for fact in facts)
    assert "Explicit current-turn constraint facts" in anchor
    assert "Current user turn explicit constraint:" in anchor
    assert "helper envelopes" in anchor
    assert "unless later verified evidence shows" in anchor


def test_active_task_anchor_preserves_feasibility_constraint_followup():
    from app.core import orchestrator

    user_turn = "If anything doesn't actually fit in the budget or my mobility, tell me up front."
    facts = orchestrator._explicit_current_turn_constraint_facts(user_turn)

    assert facts
    assert "budget" in facts[0]
    assert "mobility" in facts[0]


def test_active_task_anchor_preserves_assurance_followup_as_check_not_edit():
    from app.core import orchestrator

    user_turn = "Make sure the existing artifact includes the required section."
    anchor = orchestrator._build_active_task_contract_anchor(current_user_turn=user_turn)

    assert orchestrator._has_current_turn_assurance_followup_language(user_turn) is True
    assert "Current-turn assurance/check wording fact" in anchor
    assert "Compare existing task_plan, artifact, and verified evidence" in anchor
    assert "does not name a workspace mutation" in anchor
    assert "keep deliverables empty" in anchor


def test_active_task_anchor_preserves_marking_followup_as_check_not_edit():
    from app.core import orchestrator

    user_turn = "Anything that looks suspicious, flag it and don't touch it."
    anchor = orchestrator._build_active_task_contract_anchor(current_user_turn=user_turn)

    assert orchestrator._has_current_turn_assurance_followup_language(user_turn) is True
    assert "Current-turn assurance/check wording fact" in anchor
    assert "marking, classification, or leave-untouched wording" in anchor
    assert "If the existing evidence satisfies the requested state" in anchor
    assert "keep deliverables empty" in anchor


def test_constraint_review_followup_routes_to_round2_when_prior_evidence_exists():
    from app.core import orchestrator

    user_turn = "If anything doesn't actually fit in the budget or my mobility, tell me up front."
    base_messages = [
        {
            "role": "user",
            "content": (
                "Conversation History\n"
                "[Assistant] artifact.md was produced. <bot_log_brief>plan_key_points=[Budget $785; "
                "required section has limited-scope accessibility only]</bot_log_brief>"
            ),
        },
        {"role": "user", "content": "Current Message To Answer\n" + user_turn},
    ]

    assert orchestrator._has_current_turn_constraint_review_language(user_turn) is True
    assert orchestrator._should_route_constraint_review_to_round2(user_turn, base_messages) is True


def test_constraint_review_followup_does_not_force_round2_without_prior_evidence():
    from app.core import orchestrator

    user_turn = "If anything doesn't actually fit in the budget or my mobility, tell me up front."
    base_messages = [{"role": "user", "content": "Current Message To Answer\n" + user_turn}]

    assert orchestrator._should_route_constraint_review_to_round2(user_turn, base_messages) is False


def test_round2_dynamic_tail_keeps_coding_task_contract_near_current_request():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._round2)
    contract_src = inspect.getsource(orchestrator._build_coding_task_contract_anchor)

    assert "_build_coding_task_contract_anchor" in src
    assert "if is_coding:" in src
    assert "_append_round2_dynamic_user_tail(msgs, _build_coding_task_contract_anchor())" in src
    assert "Current coding task contract" in contract_src
    assert "project path discovery facts from env_list_tree" in contract_src
    assert "env_inventory" in contract_src
    assert "env_search" in contract_src
    assert "workspace.locate are chat/staged-workspace facts" in contract_src
    assert "use env_* facts for project source, test, config, and data paths" in contract_src
    assert "input_files plus acceptance checks" in contract_src
    assert "optional baseline failure reproduction" in contract_src
    assert "parallel batch of main-thread source/test env_read calls" in contract_src
    assert "duplicates helper-owned reading" in contract_src
    assert "baseline test failure reproduction can be delegated" in contract_src
    assert "source-body env_read/read_file calls" in contract_src
    assert "before delegation add value mainly" in contract_src
    assert "project-relative paths already carry the routing fact" in contract_src
    assert "expected_hash, diff, or apply evidence" in contract_src
    assert "project files, tests, helper reports, diffs, and run outputs are primary evidence" in contract_src
    assert "concrete memory IDs" in contract_src
    assert "Pre-helper env_read or env_run is mainly useful" in contract_src
    assert "already-running service URL" in contract_src
    assert "has not changed that URL until" in contract_src
    assert "post-fix URL acceptance belongs after main-thread apply" in contract_src
    assert "source reading or static diagnosis is not the same evidence" in contract_src
    assert "first evidence milestone before source/project edits" in contract_src
    assert "当前代码任务契约" in contract_src


def test_env_run_python_stdout_can_reach_round3_as_authoritative_evidence():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._round2)
    assert '"env_apply_replace", "env_apply_create"' in src
    assert '_parsed.get("python_code") is True' in src
    assert "This is an authoritative fact or excerpt from a main-thread tool" in src
    assert "read the recorded path/range again if exact omitted content is needed" in src
    assert "The command produced no stdout or stderr." in src


def test_round3_evidence_intro_prefers_later_main_thread_facts():
    from app.core.round_prompts import round3_helper_evidence_intro

    prompt = round3_helper_evidence_intro()
    assert "later apply, run, or verification facts" in prompt
    assert "以后续事实表示当前状态" in prompt


def test_round2_prompt_requires_new_tts_for_audio_generation_requests():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE
    assert "Speech/narration/TTS/persona-voice requests" in prompt
    assert "create a fresh file for this turn" in prompt
    assert "built-in/system TTS route" in prompt
    assert "external TTS engines such as gTTS, edge-tts, pyttsx3" in prompt
    assert "Non-speech audio generation such as white noise" in prompt
    assert "remains code/signal work, not TTS" in prompt
    assert "Voice identity, timbre, reference audio" in prompt


def test_round2_prompt_keeps_main_process_in_charge_of_resource_helpers():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=True, parallelizable=True, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "compare existing/same-batch outputs first" in visible_text
    assert "resource paths can satisfy the resume condition" in visible_text
    assert "absent resources can justify creating/refusing the resource" in visible_text
    assert "suggested_helper_kind" not in visible_text


def test_round2_prompt_routes_validation_to_verify_not_draw():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=True, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Use `verify` for high-risk artifacts" in visible_text
    assert "benchmark data" in visible_text
    assert "mathematical claims" in visible_text


def test_round2_prompt_preserves_statistic_units():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Characters, bytes, file size, line count, and file count are" in visible_text
    assert "do not relabel one as another" in visible_text
    assert "统计指标要保留单位和含义" in visible_text


def test_model_visible_prompts_use_generic_environment_terms():
    from datetime import datetime, timezone

    from app.core import context
    from app.llm import voice_output
    from app.llm.tools import delegate
    from app.llm.tools.tool_schemas import (
        DELEGATE_TOOL_SCHEMA,
        EXPAND_KB_SCHEMA,
        FETCH_GROUP_FILE_SCHEMA,
        OCR_TOOL_SCHEMA,
        TTS_TOOL_SCHEMA,
    )
    from app.llm.tools.skills import _build_skills_listing, get_skill, list_skills
    from app.schemas.api import GroupEvent, ResponsePlan

    base = context.build_base_context(
        user_name="User",
        current_message="检查文件",
        hot_user=[],
        hot_group=[
            GroupEvent(
                actor_user_id="u2",
                actor_name="Other",
                narration="Other asked a question.",
                created_at=datetime.now(timezone.utc),
            )
        ],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=[],
        in_flight_others=[("u2", "Other")],
    )[0]["content"]
    round2 = context.ROUND2_SYSTEM_TEMPLATE
    round3 = context.round3_messages(
        "You are bot.",
        ResponsePlan(intent="answer", key_points=["use evidence"], tone="plain", length_hint="short"),
        "User",
        "检查文件",
        recent_group_messages=[{"actor_name": "Other", "narration": "Other said hello."}],
    )[0]["content"]
    schema_text = "\n".join(
        str(s["function"].get("description", ""))
        for s in [
            EXPAND_KB_SCHEMA,
            FETCH_GROUP_FILE_SCHEMA,
            DELEGATE_TOOL_SCHEMA,
            OCR_TOOL_SCHEMA,
            TTS_TOOL_SCHEMA,
        ]
    )
    helper_text = "\n".join([
        delegate._HELPER_SYSTEM_READ,
        delegate._SHARED_HONESTY,
        voice_output.decide_voice_intent_from_user.__doc__ or "",
        _build_skills_listing(),
        "\n".join(get_skill(name) or "" for name in list_skills()),
    ])

    visible = "\n".join([base, round2, round3, schema_text, helper_text])
    forbidden = [
        "QQ",
        "NapCat",
        "CQ:image",
        "CQ码",
        "群聊",
        "群历史",
        "群文件",
        "群组文件",
        "群里上传",
        "群里发",
        "group chat",
        "group members",
        "group messages",
        "group activity",
        "group files",
        "group history",
        "group-file",
        "group_files",
        "group-files-warning",
    ]
    for term in forbidden:
        assert term not in visible


def test_round2_layers_encourage_parallel_dispatch_and_turn_economy():
    msgs = _build_round2_system_prompts(
        is_coding=True, is_document=False, parallelizable=True, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "### Turn economy" in visible_text
    assert "Batch independent tool calls into one turn" in visible_text
    assert "Dispatch early and overlap coordination" in visible_text
    assert "Independent helpers go in one delegate call" in visible_text
    assert "total time tracks the slowest branch, not the sum" in visible_text
    assert "spawn_async" in visible_text
    assert "独立任务合一次 delegate 并行跑" in visible_text


def test_round2_template_deduped_against_layers():
    """Boundary rules live once in the orchestrator layers; the template keeps
    one-line pointers. Guard against re-growing the duplicated paragraphs."""
    from app.core.round_prompts import ROUND2_SYSTEM_TEMPLATE

    layers = "\n".join(
        m["content"] for m in _build_round2_system_prompts(
            is_coding=True, is_document=True, parallelizable=True, needs_recall=True
        )
    )
    # These long boundary sentences must exist in layers but not in the template.
    for sentence in (
        "Follow-up assurance wording such as make sure, confirm, check that",
        "For feasibility, honesty, requirement-fit, budget, mobility, risk, or blocker checks",
        "Helper envelopes should carry raw source facts and acceptance constraints, not unsupported main-thread interpretations",
    ):
        assert sentence in layers
        assert sentence not in ROUND2_SYSTEM_TEMPLATE
