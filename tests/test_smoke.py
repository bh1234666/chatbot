"""
不依赖外部服务的本地烟雾测试：
- 所有模块能 import
- 上下文构造逻辑正确
- 三轮 messages 结构正确
"""
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.api import HotMessage, GroupEvent, ResponsePlan, TendencyAnalysis
from app.core import context as ctx_build


def test_imports():
    """所有模块能正常 import"""
    from app import config
    from app.api import archives, personas, chat
    from app.core import orchestrator
    from app.memory import hot, warm, cold, kb, archive
    from app.llm import client
    print("[OK] all imports successful")


def test_base_context_minimal():
    """空记忆下也能构造出合法 messages"""
    msgs = ctx_build.build_base_context(
        user_name="Alice",
        current_message="你好",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
    )
    assert len(msgs) == 2, f"expected [system, user], got {len(msgs)}"
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "Alice：你好" in msgs[1]["content"]
    print("[OK] base context minimal")


def test_base_context_full():
    now = datetime.now(timezone.utc)
    msgs = ctx_build.build_base_context(
        user_name="Bob",
        current_message="还记得我们昨天聊的项目吗？",
        hot_user=[
            HotMessage(role="user", content="你好", turn_id="t1", created_at=now),
            HotMessage(role="assistant", content="嗨", turn_id="t1", created_at=now),
        ],
        hot_group=[
            GroupEvent(
                actor_user_id="u1", actor_name="Alice",
                narration="Alice询问了机器人关于天气的问题。",
                created_at=now,
            ),
        ],
        warm_user_index=[
            {"id": "w_001", "headline": "讨论了X项目方案",
             "timestamp": "2026-04-27", "tendencies": {"严肃询问": 0.9}},
        ],
        warm_group_index=[
            {"id": "w_g1", "headline": "群组关于Y的讨论"},
        ],
        cold_user_topk=[
            {"id": "c_001", "headline": "Bob关注效率工具"},
        ],
        cold_group_topk=[
            {"id": "c_g1", "headline": "本群关注AI工具"},
        ],
        kb_topk=[
            {"id": "kb_001", "headline": "项目X的官方介绍"},
        ],
    )
    # 期望结构: system, user(合并: 温记忆 + 对话历史 + 当前发言)
    assert msgs[0]["role"] == "system"
    sys_text = msgs[0]["content"]
    assert "Recent Activity" in sys_text
    assert "Shared Warm Memory Index" in sys_text
    assert "Shared Long-Term Memory" in sys_text
    assert "知识库" in sys_text
    assert "[c_001]" in sys_text
    assert "SYSTEM_MEMORY_INJECTION" in sys_text  # 安全约定也声明了
    assert "Current Time" not in sys_text

    # 合并后的 user 消息包含温记忆 + 对话历史 + 当前发言
    assert len(msgs) == 2, f"expected [system, user], got {len(msgs)}"
    user_text = msgs[1]["content"]
    assert msgs[1]["role"] == "user"
    assert "Current Time" in user_text
    assert "UTC" in user_text
    assert "[SYSTEM_MEMORY_INJECTION/v1]" in user_text
    assert "w_001" in user_text
    assert "Conversation History" in user_text
    assert "你好" in user_text
    assert "嗨" in user_text
    assert "Bob：" in user_text
    print("[OK] base context full")


def test_round1_messages():
    base = ctx_build.build_base_context(
        user_name="A", current_message="hi",
        hot_user=[], hot_group=[],
        warm_user_index=[], warm_group_index=[],
        cold_user_topk=[], cold_group_topk=[], kb_topk=[],
    )
    msgs = ctx_build.round1_messages(base)
    assert msgs[0]["role"] == "system"
    assert "tendencies" in msgs[0]["content"]
    assert "background conversation router" in msgs[0]["content"]
    print("[OK] round1 messages")


def test_round1_messages_light():
    """轻量版不应包含群组动态/长期记忆/安全约定。"""
    now = datetime.now(timezone.utc)
    hot = [
        HotMessage(role="user", content="你好", turn_id="t1", created_at=now),
        HotMessage(role="assistant", content="嗨", turn_id="t1", created_at=now),
    ]
    msgs = ctx_build.round1_messages_light("Bob", "讲个笑话", hot)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    sys_text = msgs[0]["content"]
    assert "tendencies" in sys_text
    assert "background conversation router" in sys_text
    assert "Recent Activity" not in sys_text
    assert "Long-Term Memory" not in sys_text
    assert "安全约定" not in sys_text
    user_text = msgs[1]["content"]
    assert "Bob：讲个笑话" in user_text
    assert "你好" in user_text  # recent history included
    print("[OK] round1 messages light")


def test_round2_messages():
    base = ctx_build.build_base_context(
        user_name="A", current_message="hi",
        hot_user=[], hot_group=[],
        warm_user_index=[], warm_group_index=[],
        cold_user_topk=[], cold_group_topk=[], kb_topk=[],
    )
    msgs = ctx_build.round2_messages(base, {"tendencies": {"闲聊": 0.8}, "rationale": "..."})
    assert msgs[0]["role"] == "system"
    all_text = " ".join(m.get("content", "") for m in msgs)
    assert "internal_note" in all_text
    assert "deliverables" in all_text
    assert "finish with one strict JSON plan" in all_text
    assert "Stay outside the conversation and produce execution metadata only" in all_text
    print("[OK] round2 messages")


def test_round2_messages_strips_static_context_when_recall_not_needed():
    base = ctx_build.build_base_context(
        user_name="A",
        current_message="压成一条 checklist",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "群组温记忆"}],
        cold_user_topk=[{"id": "cu1", "headline": "用户长期记忆"}],
        cold_group_topk=[{"id": "cg1", "headline": "共享长期记忆"}],
        kb_topk=[{"id": "kb1", "headline": "共享知识库"}],
        file_index=[{
            "id": "f1",
            "filename": "old.docx",
            "headline": "旧文件摘要",
            "uploader_name": "A",
            "file_size": 1,
            "download_status": "done",
        }],
    )
    msgs = ctx_build.round2_messages(
        base,
        {"tendencies": {"任务类": 0.8}, "rationale": "..."},
        needs_tools=False,
        needs_recall=False,
    )
    sys_text = msgs[0]["content"]
    assert "Shared Knowledge Base" not in sys_text
    assert "Shared Files" not in sys_text
    assert "Shared Warm Memory Index" not in sys_text
    assert "Shared Long-Term Memory" not in sys_text
    assert "Current Speaker Long-Term Memory" not in sys_text


def test_round2_messages_keeps_group_file_index_for_tool_tasks():
    base = ctx_build.build_base_context(
        user_name="A",
        current_message="生成一份报告",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "群组温记忆"}],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[{"id": "kb1", "headline": "共享知识库"}],
        file_index=[{
            "id": "f1",
            "filename": "source.csv",
            "headline": "源数据",
            "uploader_name": "A",
            "file_size": 1,
            "download_status": "done",
        }],
    )
    msgs = ctx_build.round2_messages(
        base,
        {"tendencies": {"任务类": 0.8}, "rationale": "..."},
        needs_tools=True,
        needs_recall=False,
    )
    sys_text = msgs[0]["content"]
    all_text = "\n".join(m.get("content", "") for m in msgs)
    assert "source.csv" not in sys_text
    assert "source.csv" in all_text
    assert "Shared Knowledge Base" not in sys_text
    assert "Shared Warm Memory Index" not in sys_text


def test_context_followup_can_keep_recent_group_without_static_indexes():
    base = ctx_build.build_base_context(
        user_name="A",
        current_message="这个呢？",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "群组温记忆"}],
        cold_user_topk=[{"id": "cu1", "headline": "用户长期记忆"}],
        cold_group_topk=[{"id": "cg1", "headline": "共享长期记忆"}],
        kb_topk=[{"id": "kb1", "headline": "共享知识库"}],
        file_index=[{
            "id": "f1",
            "filename": "old.docx",
            "headline": "旧文件摘要",
            "uploader_name": "A",
            "file_size": 1,
            "download_status": "done",
        }],
        recent_group_messages=[
            {"sender_name": "B", "content": "Redis 热 key 要加 singleflight", "role": "user"}
        ],
    )
    all_text = "\n".join(m.get("content", "") for m in base)
    assert "Redis 热 key 要加 singleflight" in all_text

    msgs = ctx_build.round2_messages(
        base,
        {"tendencies": {"闲聊": 0.6}, "rationale": "..."},
        needs_tools=False,
        needs_recall=False,
    )
    sys_text = msgs[0]["content"]
    assert "Redis 热 key 要加 singleflight" in "\n".join(m.get("content", "") for m in msgs)
    assert "Shared Knowledge Base" not in sys_text
    assert "Shared Files" not in sys_text
    assert "Shared Warm Memory Index" not in sys_text
    assert "Shared Long-Term Memory" not in sys_text


def test_round2_text_fallback_plan_uses_generated_content():
    from app.core.orchestrator import _plan_dict_from_round2_text

    raw = _plan_dict_from_round2_text("这张大概率是数字基带传输系统里关于线路编码功率谱分析那几页。")
    assert "没有可靠完成用户委托" in raw["intent"]
    assert raw["key_points"] == ["这张大概率是数字基带传输系统里关于线路编码功率谱分析那几页。"]
    assert "降级到道歉 plan" in raw["internal_note"]
    assert raw["deliverables"] == []


def test_round2_task_json_normalization_preserves_tool_facts():
    from app.core.orchestrator import _normalize_round2_plan_dict

    raw = _normalize_round2_plan_dict({
        "status": "计划已写好",
        "project": "网页版贪吃蛇（纯前端）",
        "plan_steps": [
            "HTML 骨架 — 画布 + 分数显示 + 控制按钮 + 基础样式",
            "核心逻辑 — 蛇的移动/转向、食物生成、碰撞检测",
        ],
        "acceptance_criteria": "蛇不能反向走、吃食物增长、撞墙撞自己结束、最高分刷新后保留",
        "saved_file": "实现计划.md",
        "offer": "开始写 index.html",
    })

    assert "计划已写好" in raw["intent"]
    joined = "\n".join(raw["key_points"])
    assert "网页版贪吃蛇" in joined
    assert "HTML 骨架" in joined
    assert "Vue" not in joined
    assert raw["deliverables"] == ["实现计划.md"]
    assert "task-specific schema" in raw["internal_note"]


def test_round2_task_json_normalization_preserves_structured_rankings():
    from app.core.orchestrator import _normalize_round2_plan_dict

    raw = _normalize_round2_plan_dict({
        "intent": "基于工具结果回答",
        "key_points": ["全量遍历完成"],
        "top_12_python_files": [
            {"path": "app/llm/tools/delegate.py", "size": 193686},
            {"path": "app/llm/tools/delegate_actions.py", "size": 180104},
        ],
    })

    joined = "\n".join(raw["key_points"])
    assert "top_12_python_files" in joined
    assert "delegate_actions.py" in joined


def test_round3_messages_no_memory():
    """Round3 必须不带记忆注入，但可带对话历史。"""
    plan = ResponsePlan(
        intent="解释项目",
        key_points=["先讲背景", "再讲方案"],
        tone="严谨克制",
        length_hint="中",
        avoid=["不要展开技术细节"],
        callbacks=["回应Bob昨天的疑问"],
    )
    now = datetime.now(timezone.utc)
    hot = [
        HotMessage(role="user", content="你好", turn_id="t1", created_at=now),
        HotMessage(role="assistant", content="嗨，有什么事？", turn_id="t1", created_at=now),
    ]
    msgs = ctx_build.round3_messages(
        persona="你是一个内向的研究员",
        plan=plan,
        user_name="Bob",
        current_message="还记得吗？",
        hot_user=hot,
        light=False,
    )
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    sys_text = msgs[0]["content"]
    assert "你是一个内向的研究员" in sys_text
    assert "Current Time" not in sys_text
    # 不能包含记忆相关字样
    assert "SYSTEM_MEMORY_INJECTION" not in sys_text
    assert "Recent Activity" not in sys_text
    assert "长期记忆" not in sys_text
    # user 消息应包含对话历史 + 当前发言
    assert msgs[1]["role"] == "user"
    user_text = msgs[1]["content"]
    assert "Current Time" in user_text
    assert "UTC" in user_text
    assert "Bob：还记得吗？" in user_text
    assert "你好" in user_text  # 对话历史已包含
    assert "嗨，有什么事？" in user_text
    assert "解释项目" in user_text
    assert "严谨克制" in user_text
    print("[OK] round3 messages (with history, memory-free)")


def test_round3_messages_light():
    """light=True（默认）时不应包含对话历史——Round 2 的 plan 已有上下文。"""
    now = datetime.now(timezone.utc)
    hot = [
        HotMessage(role="user", content="旧消息", turn_id="t1", created_at=now),
    ]
    plan = ResponsePlan(intent="回应用户", key_points=["自然回应"], tone="温和",
                        length_hint="短")
    msgs = ctx_build.round3_messages(
        persona="你是助手", plan=plan, user_name="A", current_message="hi",
        hot_user=hot, light=True,
    )
    user_text = msgs[1]["content"]
    assert "A：hi" in user_text
    assert "旧消息" not in user_text  # light 模式不注入历史
    print("[OK] round3 messages (light, no history)")


def test_round3_visual_internal_terms_prompt_has_exceptions():
    plan = ResponsePlan(intent="回应用户", key_points=["自然回应"], tone="温和",
                        length_hint="短")
    msgs = ctx_build.round3_messages(
        persona="你是助手",
        plan=plan,
        user_name="A",
        current_message="这图写了什么？",
        hot_user=[],
        light=True,
        helper_reports_excerpt=[
            {"task_id": "ocr_check", "excerpt": "识别到一些文字"}
        ],
    )
    sys_text = msgs[0]["content"]
    assert "Internal terms such as OCR, TTS, helper" in sys_text
    assert "ordinary delivery" in sys_text
    assert "Concept questions are answered as concepts" in sys_text
    assert "Explain internal process details only when the user asks about tools, logs, scheduling, or concept definitions" in sys_text
    assert "only the answer, no explanation, no expansion, or a short reply" in sys_text
    assert "PASS/FAIL and success/failure labels should follow the source evidence" in sys_text
    assert "Rewrite internal paths or tool errors into user-understandable file/material status" in sys_text
    assert "Round3 只基于计划和工具证据表达事实" in sys_text
    user_text = "\n".join(m["content"] for m in msgs if m["role"] == "user")
    assert "Confirmed content may be treated as fact" in user_text
    assert "Possible/uncertain content remains uncertain" in user_text
    assert "永远别说" not in sys_text


def test_user_output_constraints_are_applied_to_round2_plan():
    from app.core.orchestrator import _apply_user_output_constraints

    plan = ResponsePlan(
        intent="识别图片并告知用户",
        key_points=["卡片编号和结果"],
        tone="自然",
        length_hint="中",
        avoid=[],
    )
    _apply_user_output_constraints(plan, "这图写了什么？只回答结果，不要解释，也不要讲内部工具")

    assert plan.length_hint == "短"
    assert "直接" in plan.tone
    assert "开场白" in plan.avoid
    assert "解释过程" in plan.avoid
    assert "内部工具信息" in plan.avoid
    assert "后台流程说明" in plan.avoid
    assert "只保留结果/答案" in plan.internal_note


def test_json_parser():
    from app.llm.client import _parse_json_strict
    # 纯 JSON
    assert _parse_json_strict('{"a": 1}') == {"a": 1}
    # 带 markdown fence
    assert _parse_json_strict('```json\n{"a": 1}\n```') == {"a": 1}
    # 前后多余文字
    assert _parse_json_strict('Sure, here is:\n{"a": 1}\nThanks') == {"a": 1}
    # 中文键
    assert _parse_json_strict('{"严肃询问": 0.8}') == {"严肃询问": 0.8}
    print("[OK] json parser")


def test_workspace_mkdir_write_run():
    """工作区：创建目录、写文件、运行 Python 脚本。"""
    import json
    import asyncio
    from app.llm.tools import workspace as ws

    ws_dir = ws.create_workspace()
    try:
        # mkdir
        r = asyncio.run(ws.handle_mkdir(ws_dir, "sub"))
        assert r["ok"] is True
        assert r["action"] == "mkdir"

        # write
        code = "print('hello workspace')\nprint(1 + 2)"
        r = asyncio.run(ws.handle_write(ws_dir, "calc.py", code))
        assert r["ok"] is True
        assert r["action"] == "write"
        assert r["size"] > 0

        # run
        r = asyncio.run(ws.handle_run(ws_dir, "python calc.py"))
        assert r["ok"] is True
        assert "hello workspace" in r["stdout"]
        assert "3" in r["stdout"]
    finally:
        ws.cleanup_workspace(ws_dir)
    print("[OK] workspace mkdir + write + run")


def test_workspace_path_traversal_blocked():
    """工作区应拒绝 .. 路径遍历。"""
    import asyncio
    from app.llm.tools import workspace as ws

    ws_dir = ws.create_workspace()
    try:
        # mkdir with ..
        r = asyncio.run(ws.handle_mkdir(ws_dir, "../escape"))
        assert r["ok"] is False

        # write with ..
        r = asyncio.run(ws.handle_write(ws_dir, "../bad.py", "print(1)"))
        assert r["ok"] is False
    finally:
        ws.cleanup_workspace(ws_dir)
    print("[OK] workspace traversal blocked")


def test_workspace_edit_helper_allows_small_data_python_but_blocks_plotting(tmp_path):
    """edit helper 可写轻量数据处理脚本，但不能重画图。"""
    import asyncio
    from app.core.core_processes import set_current_helper_kind, reset_current_helper_kind
    from app.llm.tools import workspace as ws

    helper_ws = tmp_path / ".temp" / "_delegate_user_edit"
    helper_ws.mkdir(parents=True)
    token = set_current_helper_kind("edit")
    try:
        r = asyncio.run(ws.handle_write(
            str(helper_ws),
            "extract_data.py",
            "import csv\nprint('extract only')\n",
        ))
        assert r["ok"] is True

        r = asyncio.run(ws.handle_write(
            str(helper_ws),
            "plot.py",
            "import matplotlib.pyplot as plt\nplt.savefig('x.png')\n",
        ))
        assert r["ok"] is False
        assert r["blocked_reason"] == "edit_helper_writing_out_of_scope_python"
        assert r["suggested_helper_kind"] == "draw"
        assert r["suggested_tool"] == "request_resource"
    finally:
        reset_current_helper_kind(token)


    """run 应拒绝非 python 命令。"""
    import asyncio
    from app.llm.tools import workspace as ws

    ws_dir = ws.create_workspace()
    try:
        r = asyncio.run(ws.handle_run(ws_dir, "format C:"))
        assert r["ok"] is False
        assert ("not allowed" in r["error"] or "禁止" in r["error"] or "安全" in r["error"])
    finally:
        ws.cleanup_workspace(ws_dir)
    print("[OK] workspace disallowed command")


def test_workspace_registry_schema():
    """工具 schema 格式正确且已注册到 ROUND2_TOOLS。"""
    from app.llm.tools.registry import ROUND2_TOOLS, WORKSPACE_TOOL_SCHEMA

    assert WORKSPACE_TOOL_SCHEMA["type"] == "function"
    assert WORKSPACE_TOOL_SCHEMA["function"]["name"] == "workspace"
    params = WORKSPACE_TOOL_SCHEMA["function"]["parameters"]
    assert "action" in params["required"]
    assert params["properties"]["action"]["enum"] == ["mkdir", "write", "run", "locate"]
    assert "pattern" in params["properties"]
    assert any(t["function"]["name"] == "workspace" for t in ROUND2_TOOLS)
    print("[OK] workspace registry schema")


def test_workspace_cleanup_removes_dir():
    """cleanup_workspace 应真正删除目录。"""
    import os
    from app.llm.tools import workspace as ws

    ws_dir = ws.create_workspace()
    assert os.path.isdir(ws_dir)
    ws.cleanup_workspace(ws_dir)
    assert not os.path.isdir(ws_dir)
    print("[OK] workspace cleanup removes dir")


def test_sql_translator_any_expansion():
    """ANY($N::bigint[]) → IN(?,...?)，且 args 数量与 ? 数量一致。"""
    from app.db.pool import _translate_sql

    # 单 ANY 在最后位置（最常见：kb.py 场景）
    sql, tup = _translate_sql(
        "UPDATE t SET x=TRUE WHERE a=$1 AND b=$2 AND id = ANY($3::bigint[])",
        ("aid", "gid", [1, 2, 3, 4, 5]),
    )
    assert tup == ("aid", "gid", 1, 2, 3, 4, 5), f"got {tup}"
    assert "ANY" not in sql
    assert sql.count("?") == 7  # $1 $2 + 5 IN placeholders

    # 无 ANY 的正常查询
    sql2, tup2 = _translate_sql(
        "SELECT * FROM t WHERE x=$1 AND y=$2",
        ("a", "b"),
    )
    assert tup2 == ("a", "b")
    assert sql2.count("?") == 2

    # 空列表的 ANY
    sql3, tup3 = _translate_sql(
        "DELETE FROM t WHERE id = ANY($1::bigint[])",
        ([],),
    )
    assert "IN (NULL)" in sql3
    assert len(tup3) == 0

    print("[OK] sql translator any expansion")


if __name__ == "__main__":
    test_imports()
    test_base_context_minimal()
    test_base_context_full()
    test_round1_messages()
    test_round1_messages_light()
    test_round2_messages()
    test_round3_messages_no_memory()
    test_round3_messages_light()
    test_json_parser()
    test_workspace_mkdir_write_run()
    test_workspace_path_traversal_blocked()
    test_workspace_disallowed_command()
    test_workspace_registry_schema()
    test_workspace_cleanup_removes_dir()
    test_sql_translator_any_expansion()
    print("\n=== all smoke tests passed ===")

