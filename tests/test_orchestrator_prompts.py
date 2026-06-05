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


def test_round2_common_layers_precede_task_specific_layers_for_cache_reuse():
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

    common_prefix = [message["content"] for message in variants[0][:3]]
    assert common_prefix[0].startswith("## You are the orchestrator, not the worker")
    assert common_prefix[1].startswith("## Data, experiment, and visual evidence discipline")
    assert common_prefix[2].startswith("## Recall and indexed evidence discipline")

    for messages in variants[1:]:
        assert [message["content"] for message in messages[:3]] == common_prefix

    assert "## Coding task routing" in variants[0][3]["content"]
    assert "## Document task routing" in variants[1][3]["content"]
    assert all("task routing" not in message["content"] for message in variants[2][:3])


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


def test_round2_prompt_treats_create_request_as_imperative_not_lookup():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=False, parallelizable=False, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "create, generate, write, save, export, or deliver an artifact" in visible_text
    assert "missing targets imply creation or helper dispatch" in visible_text
    assert "Main-thread execution boundary" in visible_text
    assert "short read-only inspection" in visible_text
    assert "Read-only orientation or explanation closes once the requested facts are evidenced" in visible_text
    assert "task_ok=true; no deliverable files; evidence sufficient; no upgrade" in visible_text
    assert "Source implementation, iterative debugging, compilation loops, benchmarks, and multi-file edits belong to helpers" in visible_text
    assert "expected outputs, verification commands" in visible_text
    assert "主线程负责派发" in visible_text


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
    assert "Preserve technical task ids" in prompt_src
    assert "判断是否需要中途回复" in prompt_src


def test_debug_report_prompt_avoids_internal_status_jargon():
    from app.core import debug

    assert "Return one short Chinese phrase" in debug.DEBUG_REPORT_SYSTEM
    assert "Use user-friendly process wording" in debug.DEBUG_REPORT_SYSTEM
    assert "If a clean user-facing phrase is not possible" in debug.DEBUG_REPORT_SYSTEM
    assert "把内部事件压缩成一句用户可见的中文状态" in debug.DEBUG_REPORT_SYSTEM


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
        helper_reports_excerpt=[{"task_id": "ocr#1", "excerpt": "可确认内容: alpha"}],
    )
    system_text = msgs[0]["content"]
    assert "Internal terms such as OCR, TTS, helper" in system_text
    assert "outcome-level language" in system_text and "image text" in system_text
    assert "Concept questions" in system_text and "OCR" in system_text
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")
    assert "avoid copying internal labels" in user_text or "Tool results are factual sources" in user_text
    assert "helper 摘录和主线程工具结果是证据来源" in user_text


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
    assert "History, shared conversation, and helper reports are evidence sources" in system_text
    assert "not automatic current deliverables" in system_text


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


def test_round2_prompt_keeps_main_thread_as_contract_manager_for_long_source_material():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE

    assert "Preserve the task contract throughout the toolchain" in prompt
    assert "record that contract with `agent_state` before fan-out" in prompt
    assert "keep full extracted content in helper-owned evidence files" in prompt
    assert "compact coverage summaries, counts, section maps, line ranges" in prompt
    assert "For source-driven organization or expansion, preserve the user's coverage contract" in prompt
    assert "材料驱动整理或扩写时保留用户覆盖契约" in prompt


def test_round2_prompt_keeps_framework_contracts_structural():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE

    assert "It defines slots and acceptance, not the substantive content of those slots" in prompt
    assert "research claims, citations, conclusions, tables with final values" in prompt
    assert "正文、引用、结论、实验和最终文件交给后续分片 helper" in prompt


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

    assert "## Toolchain Continuation" in src
    assert "## Current Request Contract Anchor" in src
    assert "_append_round2_dynamic_user_tail" in src
    assert "Current user request:" in src
    assert "full extracts in helper evidence files" in src
    assert "当前用户需求是本轮工具链和最终计划的验收锚点" in src

    dynamic_tail_src = inspect.getsource(orchestrator._append_round2_dynamic_user_tail)
    assert "without mutating the system prefix" in dynamic_tail_src
    assert '"role": "user"' in dynamic_tail_src


def test_env_run_python_stdout_can_reach_round3_as_authoritative_evidence():
    from app.core import orchestrator

    src = inspect.getsource(orchestrator._round2)
    assert 'tool_name in _DATA_TOOLS_FIELDS or tool_name in {"workspace", "env_run"}' in src
    assert '_parsed.get("python_code") is True' in src
    assert "This is authoritative internal output from a main-thread read/vision tool" in src


def test_round2_prompt_requires_new_tts_for_audio_generation_requests():
    from app.core import context

    prompt = context.ROUND2_SYSTEM_TEMPLATE
    assert "Generate wav/mp3/TTS/audio attachment" in prompt
    assert "create a fresh file for this turn" in prompt
    assert "voice identity and delivery configuration are controlled outside the LLM" in prompt


def test_round2_prompt_keeps_main_process_in_charge_of_resource_helpers():
    msgs = _build_round2_system_prompts(
        is_coding=False, is_document=True, parallelizable=True, needs_recall=False
    )
    visible_text = "\n".join(m["content"] for m in msgs)

    assert "Check existing/same-batch outputs first" in visible_text
    assert "resume with resource paths when satisfied" in visible_text
    assert "otherwise create or refuse the resource and wake/terminate" in visible_text
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
