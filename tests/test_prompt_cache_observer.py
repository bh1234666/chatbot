from __future__ import annotations

import ast
from pathlib import Path

from app.core.prompt_cache_observer import describe_prompt_cache_input


def _tuple_assignment_strings(source: str, name: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if isinstance(node.value, ast.Tuple):
            values = []
            for item in node.value.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    values.append(item.value)
            return tuple(values)
    raise AssertionError(f"missing tuple assignment {name}")


def test_prompt_cache_observer_knows_context_dynamic_tail_headers() -> None:
    context_src = Path("app/core/context.py").read_text(encoding="utf-8")
    observer_src = Path("app/core/prompt_cache_observer.py").read_text(encoding="utf-8")

    header_order = _tuple_assignment_strings(context_src, "_HEADER_ORDER")
    observer_dynamic = {
        item.strip()
        for item in _tuple_assignment_strings(observer_src, "dynamic_headers")
    }
    dynamic_tail = header_order[header_order.index("## Shared Files") :]

    assert set(dynamic_tail) <= observer_dynamic


def test_tendency_block_uses_sorted_json_keys() -> None:
    from app.core.context import _build_tendency_block

    left = _build_tendency_block({"needs_tools": True, "complexity": "hard", "parallelizable": False})
    right = _build_tendency_block({"parallelizable": False, "complexity": "hard", "needs_tools": True})

    assert left == right
    assert left.index('"complexity"') < left.index('"needs_tools"') < left.index('"parallelizable"')


def test_toolchain_cache_sanitizer_does_not_create_internal_aliases() -> None:
    from app.core.toolchain_cache import _sanitize_model_visible_toolchain_text

    text = _sanitize_model_visible_toolchain_text(
        "delegate wrote _helpers_shared/fetch/page.txt then _delegate_fetch resumed; "
        "internal_shared/old/page.txt and internal_run_123 were historical.",
        500,
    )

    assert "_helpers_shared" not in text
    assert "internal_shared" not in text
    assert "_delegate_" not in text
    assert "internal_run" not in text
    assert "work material" in text
    assert "processing_record" in text


def test_tool_schema_hash_is_stable_for_key_order() -> None:
    messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "x"}]
    tools_a = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read files.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["path"],
                },
            },
        }
    ]
    tools_b = [
        {
            "function": {
                "parameters": {
                    "required": ["path"],
                    "properties": {"limit": {"type": "integer"}, "path": {"type": "string"}},
                    "type": "object",
                },
                "description": "Read files.",
                "name": "read",
            },
            "type": "function",
        }
    ]

    assert describe_prompt_cache_input(messages=messages, tools=tools_a)["tool_schema_hash"] == (
        describe_prompt_cache_input(messages=messages, tools=tools_b)["tool_schema_hash"]
    )


def test_user_tail_time_does_not_change_system_static_hash() -> None:
    msg_a = [
        {"role": "system", "content": "## Context And Safety Contract\nstable"},
        {"role": "user", "content": "## Current Time\nUTC:2026-06-02 10:00\n\n## Current Message\nA"},
    ]
    msg_b = [
        {"role": "system", "content": "## Context And Safety Contract\nstable"},
        {"role": "user", "content": "## Current Time\nUTC:2026-06-02 10:01\n\n## Current Message\nB"},
    ]

    shape_a = describe_prompt_cache_input(messages=msg_a)
    shape_b = describe_prompt_cache_input(messages=msg_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["cacheable_prefix_bytes"] == shape_b["cacheable_prefix_bytes"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]


def test_environment_project_facts_do_not_change_stable_mode_contract(tmp_path) -> None:
    from app.core.environment_prompt import environment_project_context, environment_prompt_addon
    from app.core.orchestrator_prompts import _inject_dynamic_session_info
    from app.core.runtime_mode import EnvironmentContext, runtime_context

    env_a = EnvironmentContext(
        root_dir=str(tmp_path / "project_a"),
        archive_id="arch_a",
        group_id="env_user_a",
        user_id="u",
        project_key="a",
        project_name="Project A",
    )
    env_b = EnvironmentContext(
        root_dir=str(tmp_path / "project_b"),
        archive_id="arch_b",
        group_id="env_user_b",
        user_id="u",
        project_key="b",
        project_name="Project B",
    )

    with runtime_context("environment", env_a):
        addon_a = environment_prompt_addon()
        project_a = environment_project_context()
    with runtime_context("environment", env_b):
        addon_b = environment_prompt_addon()
        project_b = environment_project_context()

    assert addon_a == addon_b
    assert str(tmp_path / "project_a") not in addon_a
    assert str(tmp_path / "project_a") in project_a
    assert str(tmp_path / "project_b") in project_b

    messages_a = [{"role": "system", "content": "stable"}, {"role": "user", "content": "task"}]
    messages_b = [{"role": "system", "content": "stable"}, {"role": "user", "content": "task"}]
    _inject_dynamic_session_info(messages_a, mode_text=addon_a, project_context=project_a)
    _inject_dynamic_session_info(messages_b, mode_text=addon_b, project_context=project_b)

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]


def test_dynamic_session_info_stays_before_current_request_but_after_history() -> None:
    from app.core.orchestrator_prompts import _inject_dynamic_session_info

    messages = [
        {"role": "system", "content": "stable"},
        {
            "role": "user",
            "content": (
                "## Conversation History\nsame previous context\n\n"
                "---\n\n"
                "## Current Time\n10:00\n\n"
                "---\n\n"
                "## Current Message To Answer\nA：finish the report"
            ),
        },
    ]

    _inject_dynamic_session_info(
        messages,
        lang_directive="Reply in Chinese.",
        project_context="Project root: F:/chatbot",
    )

    user_text = messages[-1]["content"]
    assert len(messages) == 2
    assert user_text.index("## Conversation History") < user_text.index("## Dynamic Session Information")
    assert user_text.index("## Dynamic Session Information") < user_text.index("## Current Message To Answer")
    assert user_text.endswith("A：finish the report")


def test_hash_chain_exposes_first_changed_segment() -> None:
    msg_a = [
        {"role": "system", "content": "stable\n\n## Shared Files\nA"},
        {"role": "user", "content": "same"},
    ]
    msg_b = [
        {"role": "system", "content": "stable\n\n## Shared Files\nB"},
        {"role": "user", "content": "same"},
    ]

    chain_a = describe_prompt_cache_input(messages=msg_a)["hash_chain"]
    chain_b = describe_prompt_cache_input(messages=msg_b)["hash_chain"]

    assert chain_a[0]["label"] == "system_static"
    assert chain_a[0]["hash"] == chain_b[0]["hash"]
    assert chain_a[2]["label"] == "system_dynamic"
    assert chain_a[2]["hash"] != chain_b[2]["hash"]


def test_task_tail_sections_are_counted_as_system_dynamic() -> None:
    msg_a = [
        {
            "role": "system",
            "content": (
                "## Context And Safety Contract\nstable\n\n"
                "## Previous Analysis\n{\"complexity\":\"normal\"}\n\n"
                "## Current Workspace (.temp) Snapshot\nfile-a.txt"
            ),
        },
        {"role": "user", "content": "same"},
    ]
    msg_b = [
        {
            "role": "system",
            "content": (
                "## Context And Safety Contract\nstable\n\n"
                "## Previous Analysis\n{\"complexity\":\"hard\"}\n\n"
                "## Current Workspace (.temp) Snapshot\nfile-b.txt"
            ),
        },
        {"role": "user", "content": "same"},
    ]

    shape_a = describe_prompt_cache_input(messages=msg_a)
    shape_b = describe_prompt_cache_input(messages=msg_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["system_dynamic_hash"] != shape_b["system_dynamic_hash"]
    assert shape_a["cacheable_prefix_bytes"] == shape_b["cacheable_prefix_bytes"]


def test_base_context_memory_sections_are_counted_as_system_dynamic() -> None:
    msg_a = [
        {
            "role": "system",
            "content": (
                "## Context And Safety Contract\nstable\n\n"
                "## Shared Long-Term Memory\nmemory A\n\n"
                "## Shared Knowledge Base\nkb A\n\n"
                "## Recent Shared Messages (chronological, 1 read-only snapshots)\nmessage A"
            ),
        },
        {"role": "user", "content": "same"},
    ]
    msg_b = [
        {
            "role": "system",
            "content": (
                "## Context And Safety Contract\nstable\n\n"
                "## Shared Long-Term Memory\nmemory B\n\n"
                "## Shared Knowledge Base\nkb B\n\n"
                "## Recent Shared Messages (chronological, 1 read-only snapshots)\nmessage B"
            ),
        },
        {"role": "user", "content": "same"},
    ]

    shape_a = describe_prompt_cache_input(messages=msg_a)
    shape_b = describe_prompt_cache_input(messages=msg_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["system_dynamic_hash"] != shape_b["system_dynamic_hash"]
    assert shape_a["cacheable_prefix_bytes"] == shape_b["cacheable_prefix_bytes"]


def test_cache_observer_counts_all_leading_system_messages_as_prefix() -> None:
    messages_a = [
        {"role": "system", "content": "## Stable Base\nsame"},
        {"role": "system", "content": "## Stable Round2\nsame"},
        {"role": "user", "content": "dynamic task A"},
        {"role": "system", "content": "## Late System\nlate A"},
    ]
    messages_b = [
        {"role": "system", "content": "## Stable Base\nsame"},
        {"role": "system", "content": "## Stable Round2\nsame"},
        {"role": "user", "content": "dynamic task B"},
        {"role": "system", "content": "## Late System\nlate B"},
    ]
    messages_c = [
        {"role": "system", "content": "## Stable Base\nsame"},
        {"role": "system", "content": "## Stable Round2\nchanged"},
        {"role": "user", "content": "dynamic task A"},
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)
    shape_c = describe_prompt_cache_input(messages=messages_c)

    assert shape_a["leading_system_count"] == 2
    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["system_static_hash"] != shape_c["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert any(item["label"] == "msg3.system:## Late System" for item in shape_a["message_sections"])


def test_round2_static_system_messages_are_inserted_before_user_context() -> None:
    from app.core.orchestrator import _insert_round2_system_messages_before_user

    messages = [
        {"role": "system", "content": "## Stable Base\nsame"},
        {"role": "user", "content": "## Round 2 Dynamic Context\nfile A"},
    ]

    _insert_round2_system_messages_before_user(
        messages,
        [
            {"role": "system", "content": "## Highest-Priority Persona Decision\nsame"},
            {"role": "system", "content": "## Toolchain Continuation\nsame"},
        ],
    )

    roles = [message["role"] for message in messages]
    shape = describe_prompt_cache_input(messages=messages)

    assert roles == ["system", "system", "system", "user"]
    assert shape["leading_system_count"] == 3
    assert messages[-1]["content"].startswith("## Round 2 Dynamic Context")


def test_round2_messages_keep_task_context_out_of_system_prefix() -> None:
    from app.core import context as ctx_build

    base_a = ctx_build.build_base_context(
        user_name="A",
        current_message="生成报告 A",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=[{
            "id": "f1",
            "filename": "source-a.csv",
            "headline": "A data",
            "uploader_name": "A",
            "file_size": 10,
            "download_status": "done",
        }],
    )
    base_b = ctx_build.build_base_context(
        user_name="A",
        current_message="生成报告 B",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=[{
            "id": "f2",
            "filename": "source-b.csv",
            "headline": "B data",
            "uploader_name": "A",
            "file_size": 20,
            "download_status": "done",
        }],
    )

    messages_a = ctx_build.round2_messages(
        base_a,
        {"complexity": "normal", "rationale": "A"},
        needs_tools=True,
        needs_recall=False,
    )
    messages_b = ctx_build.round2_messages(
        base_b,
        {"complexity": "hard", "rationale": "B"},
        needs_tools=True,
        needs_recall=False,
    )

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)
    joined_system = "\n".join(m["content"] for m in messages_a if m["role"] == "system")
    joined_user = "\n".join(m["content"] for m in messages_a if m["role"] == "user")

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert "source-a.csv" not in joined_system
    assert "## Entry Routing Snapshot" not in joined_system
    assert "source-a.csv" in joined_user
    assert "## Entry Routing Snapshot" in joined_user


def test_round2_real_context_system_prefix_is_task_independent() -> None:
    from app.core import context as ctx_build

    def build(current_message: str, filename: str, complexity: str) -> list[dict]:
        base = ctx_build.build_base_context(
            user_name="A",
            current_message=current_message,
            hot_user=[],
            hot_group=[],
            warm_user_index=[],
            warm_group_index=[{"id": "w1", "headline": f"warm {filename}"}],
            cold_user_topk=[],
            cold_group_topk=[],
            kb_topk=[{"id": "kb1", "headline": f"kb {filename}"}],
            file_index=[{
                "id": "f1",
                "filename": filename,
                "headline": f"file {filename}",
                "uploader_name": "A",
                "file_size": 1,
                "download_status": "done",
            }],
        )
        return ctx_build.round2_messages(
            base,
            {"complexity": complexity, "rationale": filename, "needs_tools": True},
            workspace_listing=[filename],
            needs_tools=True,
            needs_recall=True,
        )

    messages_a = build("分析 A.csv", "A.csv", "normal")
    messages_b = build("分析 B.csv", "B.csv", "hard")
    shape_a = describe_prompt_cache_input(messages=messages_a, tools=[])
    shape_b = describe_prompt_cache_input(messages=messages_b, tools=[])

    assert shape_a["system_prompt_hash"] == shape_b["system_prompt_hash"]
    assert shape_a["system_dynamic_hash"] == shape_b["system_dynamic_hash"]
    assert shape_a["hash_chain"][2]["label"] == "system_dynamic"
    assert shape_a["hash_chain"][2]["bytes"] == 0
    assert "A.csv" not in messages_a[0]["content"]
    assert any("A.csv" in m.get("content", "") for m in messages_a if m.get("role") == "user")


def test_round2_dynamic_context_precedes_current_request_tail() -> None:
    from app.core import context as ctx_build

    base = ctx_build.build_base_context(
        user_name="A",
        current_message="分析 A.csv 并输出结论",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "warm A"}],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[{"id": "kb1", "headline": "kb A"}],
        file_index=[{
            "id": "f1",
            "filename": "A.csv",
            "headline": "file A",
            "uploader_name": "A",
            "file_size": 1,
            "download_status": "done",
        }],
    )

    messages = ctx_build.round2_messages(
        base,
        {"complexity": "normal", "needs_tools": True, "rationale": "A.csv"},
        workspace_listing=["A.csv"],
        needs_tools=True,
        needs_recall=True,
    )
    user_messages = [m["content"] for m in messages if m.get("role") == "user"]
    user_text = "\n\n".join(user_messages)

    assert "## Round 2 Dynamic Context" in user_text
    assert "## Current Message To Answer" in user_text
    assert user_text.index("## Round 2 Dynamic Context") < user_text.index("## Current Message To Answer")
    assert "分析 A.csv 并输出结论" in user_text


def test_round2_dynamic_context_orders_task_specific_routing_late() -> None:
    from app.core import context as ctx_build

    base = ctx_build.build_base_context(
        user_name="A",
        current_message="分析 A.csv",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "shared warm"}],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[{"id": "kb1", "headline": "shared kb"}],
        file_index=[{
            "id": "f1",
            "filename": "A.csv",
            "headline": "source file",
            "uploader_name": "A",
            "file_size": 1,
            "download_status": "done",
        }],
    )

    messages = ctx_build.round2_messages(
        base,
        {"complexity": "hard", "needs_tools": True, "rationale": "task-specific"},
        workspace_listing=["A.csv"],
        needs_tools=True,
        needs_recall=True,
    )
    dynamic_context = next(
        m["content"] for m in messages
        if m.get("role") == "user" and "## Round 2 Dynamic Context" in m.get("content", "")
    )

    assert dynamic_context.index("## Shared Knowledge Base") < dynamic_context.index("## Current Workspace (.temp) Snapshot")
    assert dynamic_context.index("## Current Workspace (.temp) Snapshot") < dynamic_context.index("## Entry Routing Snapshot")


def test_round1_real_context_system_prefix_is_task_independent() -> None:
    from app.core import context as ctx_build

    def build(current_message: str, filename: str) -> list[dict]:
        base = ctx_build.build_base_context(
            user_name="A",
            current_message=current_message,
            hot_user=[],
            hot_group=[],
            warm_user_index=[],
            warm_group_index=[{"id": "w1", "headline": f"warm {filename}"}],
            cold_user_topk=[],
            cold_group_topk=[],
            kb_topk=[{"id": "kb1", "headline": f"kb {filename}"}],
            file_index=[{
                "id": "f1",
                "filename": filename,
                "headline": f"file {filename}",
                "uploader_name": "A",
                "file_size": 1,
                "download_status": "done",
            }],
        )
        return ctx_build.round1_messages(base)

    messages_a = build("路由 A.csv", "A.csv")
    messages_b = build("路由 B.csv", "B.csv")
    shape_a = describe_prompt_cache_input(messages=messages_a, tools=[])
    shape_b = describe_prompt_cache_input(messages=messages_b, tools=[])

    assert shape_a["system_prompt_hash"] == shape_b["system_prompt_hash"]
    assert shape_a["system_dynamic_hash"] == shape_b["system_dynamic_hash"]
    assert shape_a["hash_chain"][2]["label"] == "system_dynamic"
    assert shape_a["hash_chain"][2]["bytes"] == 0
    assert "background conversation router" in messages_a[0]["content"]
    assert "A.csv" not in messages_a[0]["content"]
    assert "## Round 1 Dynamic Context" in messages_a[1]["content"]
    assert "A.csv" in messages_a[1]["content"]


def test_round1_round3_fast_paths_stay_compact_for_ordinary_dialogue() -> None:
    """Round1/Round3 cache work must not make ordinary dialogue prompts heavy."""
    from datetime import datetime, timezone

    from app.core import context as ctx_build
    from app.schemas.api import HotMessage, ResponsePlan

    fast_prefix_budget_bytes = 8_000
    fast_total_budget_bytes = 10_000

    base = ctx_build.build_base_context(
        user_name="A",
        current_message="hi",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
    )
    plan = ResponsePlan(
        intent="reply",
        key_points=["answer directly"],
        tone="plain",
        length_hint="short",
    )
    persona = "You are bot. Answer directly.\n你是 bot，直接回答。"
    hot = [
        HotMessage(
            role="user",
            content="old message",
            turn_id="t1",
            created_at=datetime.now(timezone.utc),
        )
    ]
    cases = {
        "round1": ctx_build.round1_messages(base),
        "round1_light": ctx_build.round1_messages_light("A", "hi", []),
        "round3_light": ctx_build.round3_messages(
            persona,
            plan,
            "A",
            "hi",
            [],
            light=True,
        ),
        "round3_full": ctx_build.round3_messages(
            persona,
            plan,
            "A",
            "hi",
            hot,
            light=False,
        ),
    }

    for label, messages in cases.items():
        shape = describe_prompt_cache_input(messages=messages, tools=[])
        total_bytes = sum(len(str(m.get("content") or "").encode("utf-8")) for m in messages)

        assert len(messages) == 2, label
        assert shape["leading_system_count"] == 1, label
        assert shape["system_dynamic_bytes"] == 0, label
        assert shape["cacheable_prefix_bytes"] <= fast_prefix_budget_bytes, label
        assert total_bytes <= fast_total_budget_bytes, label


def test_section_summaries_expose_changed_user_block() -> None:
    msg_a = [
        {"role": "system", "content": "## Context And Safety Contract\nstable\n\n## Shared Files\nsame"},
        {
            "role": "user",
            "content": "## Conversation History\nsame\n\n## Current Time\n10:00\n\n## Current Message To Answer\nA",
        },
    ]
    msg_b = [
        {"role": "system", "content": "## Context And Safety Contract\nstable\n\n## Shared Files\nsame"},
        {
            "role": "user",
            "content": "## Conversation History\nsame\n\n## Current Time\n10:01\n\n## Current Message To Answer\nB",
        },
    ]

    shape_a = describe_prompt_cache_input(messages=msg_a)
    shape_b = describe_prompt_cache_input(messages=msg_b)
    user_a = {item["label"]: item["hash"] for item in shape_a["message_sections"]}
    user_b = {item["label"]: item["hash"] for item in shape_b["message_sections"]}

    assert user_a["msg1.user:## Conversation History"] == user_b["msg1.user:## Conversation History"]
    assert user_a["msg1.user:## Current Time"] != user_b["msg1.user:## Current Time"]
    assert user_a["msg1.user:## Current Message To Answer"] != user_b[
        "msg1.user:## Current Message To Answer"
    ]


def test_log_prompt_cache_shape_payload_contains_section_summaries(monkeypatch) -> None:
    from app.llm import client

    captured: dict[str, object] = {}

    def fake_log(category, message, payload=None):
        captured["category"] = category
        captured["payload"] = payload or {}

    monkeypatch.setattr(client.debug, "log", fake_log)
    client._log_prompt_cache_shape(
        label="unit",
        model="unit-model",
        messages=[
            {"role": "system", "content": "## Context And Safety Contract\nstable"},
            {"role": "user", "content": "## Current Message To Answer\nhello"},
        ],
        tools=[],
    )

    payload = captured["payload"]
    assert captured["category"] == "llm.prompt_cache_shape"
    assert payload["system_sections"][0]["label"] == "system:## Context And Safety Contract"
    assert payload["message_sections"][0]["label"] == "msg1.user:## Current Message To Answer"
    assert "messages" in payload


def test_log_prompt_cache_shape_summary_payload_is_opt_in(monkeypatch) -> None:
    from app.llm import client

    captured: dict[str, object] = {}

    def fake_log(category, message, payload=None):
        captured["payload"] = payload or {}

    monkeypatch.setattr(client.debug, "log", fake_log)
    monkeypatch.setattr(client.settings, "debug_prompt_cache_full_shape", False)
    client._log_prompt_cache_shape(
        label="unit",
        model="unit-model",
        messages=[
            {"role": "system", "content": "## Context And Safety Contract\nstable"},
            {"role": "user", "content": "## Current Message To Answer\nhello"},
        ],
        tools=[],
    )

    assert "messages" not in captured["payload"]


def test_environment_tools_preserve_chat_tool_prefix() -> None:
    from app.core.prompt_cache_observer import _tool_schema_summary
    from app.llm.tools.registry import tools_for_runtime_mode

    chat_tools = tools_for_runtime_mode("chat")
    env_tools = tools_for_runtime_mode("environment")

    chat_by_name = {tool["function"]["name"]: tool for tool in chat_tools}
    env_names = [tool["function"]["name"] for tool in env_tools]
    shared_names = [tool["function"]["name"] for tool in chat_tools if tool["function"]["name"] in env_names]

    assert shared_names
    assert [name for name in env_names if name in chat_by_name] == shared_names
    assert _tool_schema_summary([chat_by_name[name] for name in shared_names]) == _tool_schema_summary(env_tools[:len(shared_names)])
    assert "recall_thread" not in env_names
    assert "expand_warm" not in env_names
    assert any(tool["function"]["name"] == "delegate_inventory" for tool in env_tools[len(chat_tools):])


def test_environment_tool_list_reuses_stable_identity_and_schema_hash() -> None:
    from app.core.prompt_cache_observer import describe_prompt_cache_input
    from app.llm.tools.registry import tools_for_runtime_mode

    messages = [
        {"role": "system", "content": "stable environment system"},
        {"role": "user", "content": "dynamic task"},
    ]

    first = tools_for_runtime_mode("environment")
    second = tools_for_runtime_mode("environment")

    assert first is second
    assert describe_prompt_cache_input(messages=messages, tools=first)["tool_schema_hash"] == (
        describe_prompt_cache_input(messages=messages, tools=second)["tool_schema_hash"]
    )


def test_helper_tool_filter_reuses_stable_identity_and_schema_hash() -> None:
    from app.core.prompt_cache_observer import describe_prompt_cache_input
    from app.llm.tools.delegate import _HELPER_TOOLS, _filter_tools_for_kind

    messages = [
        {"role": "system", "content": "stable helper system"},
        {"role": "user", "content": "dynamic helper task"},
    ]

    for kind in ("code", "read", "edit", "verify", "inventory"):
        first = _filter_tools_for_kind(kind, _HELPER_TOOLS)
        second = _filter_tools_for_kind(kind, _HELPER_TOOLS)

        assert first is second, kind
        assert describe_prompt_cache_input(messages=messages, tools=first)["tool_schema_hash"] == (
            describe_prompt_cache_input(messages=messages, tools=second)["tool_schema_hash"]
        )


def test_continue_toolchain_use_does_not_change_tool_schema_hash() -> None:
    from app.core import toolchain_cache

    trace_id = "trace_prompt_cache_continue"
    toolchain_cache.reset_trace(trace_id)
    tools = [
        {
            "type": "function",
            "function": {"name": "continue_toolchain", "description": "resume", "parameters": {}},
        },
        {
            "type": "function",
            "function": {"name": "delegate", "description": "delegate", "parameters": {}},
        },
    ]
    messages = [{"role": "system", "content": "stable system"}, {"role": "user", "content": "task"}]

    before = describe_prompt_cache_input(
        messages=messages,
        tools=toolchain_cache.filter_tools_for_trace(tools, trace_id),
    )
    toolchain_cache._CONTINUED_TRACES.add(trace_id)
    after = describe_prompt_cache_input(
        messages=messages,
        tools=toolchain_cache.filter_tools_for_trace(tools, trace_id),
    )

    assert after["tool_schema_hash"] == before["tool_schema_hash"]
    assert after["tool_schema_bytes"] == before["tool_schema_bytes"]
    assert after["tool_count"] == before["tool_count"]
    toolchain_cache.reset_trace(trace_id)


def test_tool_schema_retry_guidance_is_trace_scoped_dynamic_overlay() -> None:
    from app.core import toolchain_cache

    trace_a = "trace_schema_retry_a"
    trace_b = "trace_schema_retry_b"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "workspace",
                "description": "workspace tool " + ("details " * 80),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["run", "write"]},
                        "path": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        }
    ]
    messages = [{"role": "system", "content": "stable system"}, {"role": "user", "content": "task"}]

    toolchain_cache.reset_trace(trace_a)
    toolchain_cache.reset_trace(trace_b)
    before = describe_prompt_cache_input(
        messages=messages,
        tools=toolchain_cache.filter_tools_for_trace(tools, trace_a),
    )
    toolchain_cache.mark_tool_schema_retry("workspace", "missing action", trace_a)

    guidance_a = toolchain_cache.tool_schema_retry_guidance(tools, trace_a)
    guidance_b = toolchain_cache.tool_schema_retry_guidance(tools, trace_b)
    after = describe_prompt_cache_input(
        messages=messages,
        tools=toolchain_cache.filter_tools_for_trace(tools, trace_a),
    )

    assert "Tool Schema Retry Facts" in guidance_a
    assert '"workspace"' in guidance_a
    assert guidance_b == ""
    assert after["tool_schema_hash"] == before["tool_schema_hash"]
    assert after["tool_schema_bytes"] == before["tool_schema_bytes"]

    toolchain_cache.clear_tool_schema_retry("workspace", trace_a, reason="test cleanup")
    assert toolchain_cache.tool_schema_retry_guidance(tools, trace_a) == ""
    toolchain_cache.reset_trace(trace_a)
    toolchain_cache.reset_trace(trace_b)


def test_round2_runtime_tool_schema_is_task_signal_independent() -> None:
    from app.core.prompt_cache_observer import describe_prompt_cache_input
    from app.llm.tools.registry import tools_for_runtime_mode

    tools = tools_for_runtime_mode("chat")
    ordinary_messages = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "## Current Message To Answer\nSay hello."},
    ]
    ocr_recall_messages = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "## Current Message To Answer\nRead the uploaded image and recall prior notes."},
    ]

    ordinary_shape = describe_prompt_cache_input(messages=ordinary_messages, tools=tools)
    ocr_recall_shape = describe_prompt_cache_input(messages=ocr_recall_messages, tools=tools)

    assert ordinary_shape["tool_schema_hash"] == ocr_recall_shape["tool_schema_hash"]
    assert ordinary_shape["tool_schema_bytes"] == ocr_recall_shape["tool_schema_bytes"]
    assert ordinary_shape["tool_count"] == ocr_recall_shape["tool_count"]


def test_round2_dynamic_task_guidance_does_not_change_system_prefix() -> None:
    from app.core.orchestrator import _append_round2_dynamic_user_tail

    messages_a = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "base task"},
    ]
    messages_b = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "base task"},
    ]

    _append_round2_dynamic_user_tail(
        messages_a,
        "## Active Task Contract Anchor\nAnalyze project A.",
    )
    _append_round2_dynamic_user_tail(
        messages_b,
        "## Mandatory Fresh OCR\nRecognize image B again.",
    )

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert "Active Task Contract Anchor" in messages_a[-1]["content"]
    assert "Mandatory Fresh OCR" in messages_b[-1]["content"]


def test_round2_dynamic_task_guidance_reuses_single_container() -> None:
    from app.core.orchestrator import _append_round2_dynamic_user_tail

    messages = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "base task"},
    ]

    _append_round2_dynamic_user_tail(messages, "## Active Task Contract Anchor\nAnalyze project A.")
    _append_round2_dynamic_user_tail(messages, "## Current Coding Task Contract\nUse helper evidence.")

    content = messages[-1]["content"]
    assert content.count("## Round 2 Dynamic Task Guidance") == 1
    assert "## Active Task Contract Anchor" in content
    assert "## Current Coding Task Contract" in content


def test_prior_tier_guidance_does_not_change_round2_system_prefix(tmp_path: Path) -> None:
    from app.core.orchestrator import (
        _append_round2_dynamic_user_tail,
        _build_prior_tier_dynamic_guidance,
    )
    from app.schemas.api import ResponsePlan

    workspace_a = tmp_path / "workspace_a"
    workspace_b = tmp_path / "workspace_b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    (workspace_a / "alpha_report.md").write_text("alpha", encoding="utf-8")
    (workspace_b / "beta_report.md").write_text("beta", encoding="utf-8")

    messages_a = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "base task"},
    ]
    messages_b = [
        {"role": "system", "content": "stable round2 system"},
        {"role": "user", "content": "base task"},
    ]

    _append_round2_dynamic_user_tail(
        messages_a,
        _build_prior_tier_dynamic_guidance(
            ResponsePlan(
                intent="Review alpha project",
                key_points=["alpha already has a report"],
                tone="concise",
                length_hint="medium",
            ),
            workspace_dir=str(workspace_a),
        ),
    )
    _append_round2_dynamic_user_tail(
        messages_b,
        _build_prior_tier_dynamic_guidance(
            ResponsePlan(
                intent="Review beta project",
                key_points=["beta already has a report"],
                tone="formal",
                length_hint="long",
            ),
            workspace_dir=str(workspace_b),
        ),
    )

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["cacheable_prefix_bytes"] == shape_b["cacheable_prefix_bytes"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert "Prior Tier Work" in messages_a[-1]["content"]
    assert "alpha_report.md" in messages_a[-1]["content"]
    assert "beta_report.md" in messages_b[-1]["content"]


def test_round2_recall_flag_does_not_change_system_prompt_set() -> None:
    from app.core.orchestrator_prompts import _build_round2_system_prompts

    recall = _build_round2_system_prompts(
        is_coding=True,
        is_document=False,
        parallelizable=True,
        needs_recall=True,
    )
    no_recall = _build_round2_system_prompts(
        is_coding=True,
        is_document=False,
        parallelizable=True,
        needs_recall=False,
    )

    assert recall == no_recall
    assert "Recall and indexed evidence discipline" in "\n".join(m["content"] for m in recall)


def test_tool_loop_dynamic_guidance_does_not_change_system_prefix() -> None:
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    messages_a = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]
    messages_b = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]

    _append_tool_loop_dynamic_guidance(messages_a, "[SYSTEM_HINT/auto_retry] retry task A")
    _append_tool_loop_dynamic_guidance(messages_b, "[SYSTEM_HINT/idle] collect helper B")

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert messages_a[-1]["role"] == "user"
    assert "Tool Loop Dynamic Guidance" in messages_a[-1]["content"]


def test_tool_loop_dynamic_guidance_merges_into_trailing_tool_result() -> None:
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ]

    before_len = len(messages)
    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/auto_retry] retry task A")

    assert len(messages) == before_len
    assert messages[-1]["role"] == "tool"
    assert "Tool Loop Dynamic Guidance" in messages[-1]["content"]
    assert "[SYSTEM_HINT/auto_retry]" in messages[-1]["content"]


def test_tool_loop_dynamic_guidance_merges_consecutive_guidance_messages() -> None:
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]

    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/first] first hint")
    before_len = len(messages)
    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/second] second hint")

    assert len(messages) == before_len
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"].count("Tool Loop Dynamic Guidance") == 1
    assert "[SYSTEM_HINT/first]" in messages[-1]["content"]
    assert "[SYSTEM_HINT/second]" in messages[-1]["content"]


def test_tool_loop_dynamic_guidance_dedupes_same_pending_tag() -> None:
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]

    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/retry] first fact")
    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/retry] repeated fact")

    text = messages[-1]["content"]
    assert text.count("[SYSTEM_HINT/retry]") == 1
    assert "first fact" in text
    assert "repeated fact" not in text


def test_tool_loop_dynamic_guidance_dedupes_same_tag_across_context() -> None:
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": (
                '{"ok": true}\n\n## Tool Loop Dynamic Guidance\n'
                "[SYSTEM_HINT/across_context] old pending fact"
            ),
        },
        {"role": "assistant", "content": "thinking"},
        {"role": "user", "content": "continue"},
    ]

    before = len(messages)
    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/across_context] new duplicate fact")

    assert len(messages) == before
    joined = "\n".join(str(m.get("content") or "") for m in messages)
    assert joined.count("[SYSTEM_HINT/across_context]") == 1
    assert "new duplicate fact" not in joined


def test_tool_loop_dynamic_guidance_shortens_large_payload() -> None:
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]
    payload = "[SYSTEM_HINT/large]\n" + ("a" * 30_000)

    _append_tool_loop_dynamic_guidance(messages, payload)

    text = messages[-1]["content"]
    assert len(text) < 13_000
    assert "dynamic guidance payload shortened" in text
    assert "[SYSTEM_HINT/large]" in text


def test_transient_tool_loop_guidance_clears_after_model_response() -> None:
    from app.llm.client_tools_loop import (
        _append_tool_loop_dynamic_guidance,
        _clear_transient_tool_loop_guidance,
    )

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]

    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/auto_retry] retry task A")

    assert _clear_transient_tool_loop_guidance(messages, reason="test") == 1
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "SYSTEM_HINT" not in "\n".join(str(m.get("content") or "") for m in messages)


def test_transient_tool_loop_guidance_retained_until_action_taken() -> None:
    from app.llm.client_tools_loop import (
        _append_tool_loop_dynamic_guidance,
        _clear_transient_tool_loop_guidance,
    )

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
    ]

    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/needs_action] retry task A")

    assert _clear_transient_tool_loop_guidance(messages, reason="test", action_taken=False) == 0
    assert "SYSTEM_HINT/needs_action" in messages[-1]["content"]

    assert _clear_transient_tool_loop_guidance(messages, reason="test", action_taken=True) == 1
    assert "SYSTEM_HINT" not in "\n".join(str(m.get("content") or "") for m in messages)


def test_transient_tool_loop_guidance_clears_from_tool_result_without_breaking_pairing() -> None:
    from app.llm.client_tools_loop import (
        _append_tool_loop_dynamic_guidance,
        _clear_transient_tool_loop_guidance,
    )

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ]

    _append_tool_loop_dynamic_guidance(messages, "[SYSTEM_HINT/auto_retry] retry task A")

    assert _clear_transient_tool_loop_guidance(messages, reason="test") == 1
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_1"
    assert messages[-1]["content"] == '{"ok": true}'


def test_transient_schema_retry_facts_clear_as_one_shot_guidance() -> None:
    from app.llm.client_tools_loop import _clear_transient_tool_loop_guidance

    messages = [
        {"role": "system", "content": "stable tool loop system"},
        {"role": "user", "content": "task A"},
        {"role": "user", "content": "## Tool Schema Retry Facts\nfull schema payload"},
    ]

    assert _clear_transient_tool_loop_guidance(messages, reason="test") == 1
    assert [m["content"] for m in messages] == ["stable tool loop system", "task A"]


def test_high_frequency_auxiliary_prompts_keep_stable_system_prefix() -> None:
    from app.api.chat import _auto_continue_user_payload
    from app.core.guard_prompts import AUTO_CONTINUE_JUDGE_SYSTEM
    from app.core.intermediate_feedback import (
        INTERMEDIATE_FEEDBACK_SYSTEM,
        _intermediate_feedback_user_payload,
    )
    from app.schemas.api import AutoContinueCheckRequest

    auto_a = [
        {"role": "system", "content": AUTO_CONTINUE_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": _auto_continue_user_payload(
                AutoContinueCheckRequest(
                    user_message="继续完成报告",
                    assistant_reply="第一部分完成，下一步继续。",
                    recent_context="ctx",
                    auto_continue_elapsed_sec=10,
                    max_auto_continue_sec=300,
                )
            ),
        },
    ]
    auto_b = [
        {"role": "system", "content": AUTO_CONTINUE_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": _auto_continue_user_payload(
                AutoContinueCheckRequest(
                    user_message="继续完成报告第二版",
                    assistant_reply="第一部分完成，下一步继续第二部分。",
                    recent_context="ctx2",
                    auto_continue_elapsed_sec=11,
                    max_auto_continue_sec=300,
                )
            ),
        },
    ]
    feedback_a = [
        {"role": "system", "content": INTERMEDIATE_FEEDBACK_SYSTEM},
        {
            "role": "user",
            "content": _intermediate_feedback_user_payload(
                persona="bot",
                user_request="整理材料",
                recent_work="event_fact=helper_started",
                event="helper_start",
                event_hint="started",
                preference=1.0,
                direct=True,
                stage="round2",
                iteration=1,
            ),
        },
    ]
    feedback_b = [
        {"role": "system", "content": INTERMEDIATE_FEEDBACK_SYSTEM},
        {
            "role": "user",
            "content": _intermediate_feedback_user_payload(
                persona="bot",
                user_request="整理材料二",
                recent_work="event_fact=helper_started\nhelper_task=read_b",
                event="helper_start",
                event_hint="started",
                preference=1.0,
                direct=True,
                stage="round2",
                iteration=2,
            ),
        },
    ]

    assert describe_prompt_cache_input(messages=auto_a)["system_static_hash"] == (
        describe_prompt_cache_input(messages=auto_b)["system_static_hash"]
    )
    assert describe_prompt_cache_input(messages=feedback_a)["system_static_hash"] == (
        describe_prompt_cache_input(messages=feedback_b)["system_static_hash"]
    )
    assert describe_prompt_cache_input(messages=auto_a)["messages_hash"] != (
        describe_prompt_cache_input(messages=auto_b)["messages_hash"]
    )
    assert describe_prompt_cache_input(messages=feedback_a)["messages_hash"] != (
        describe_prompt_cache_input(messages=feedback_b)["messages_hash"]
    )


def test_task_quality_guard_dynamic_facts_do_not_change_system_prefix() -> None:
    from app.llm import aux_prompts

    system_a = aux_prompts.build_task_quality_guard_system(
        persona="persona A",
        env_helper_kind_line="env A",
        helper_kind_scope_facts="kind A",
        project_kind_principle="project A",
        existing_block_counts={"a": 1},
        existing_kind_block_counts={"a": 2},
    )
    system_b = aux_prompts.build_task_quality_guard_system(
        persona="persona B",
        env_helper_kind_line="env B",
        helper_kind_scope_facts="kind B",
        project_kind_principle="project B",
        existing_block_counts={"b": 3},
        existing_kind_block_counts={"b": 4},
    )
    user_a = aux_prompts.TASK_QUALITY_GUARD_USER_TEMPLATE.format(
        task_anchor="anchor A",
        user_message="user A",
        task_brief="task A",
        runtime_facts=aux_prompts.build_task_quality_guard_runtime_facts(
            persona="persona A",
            env_helper_kind_line="env A",
            helper_kind_scope_facts="kind A",
            project_kind_principle="project A",
            existing_block_counts={"a": 1},
            existing_kind_block_counts={"a": 2},
        ),
    )
    user_b = aux_prompts.TASK_QUALITY_GUARD_USER_TEMPLATE.format(
        task_anchor="anchor B",
        user_message="user B",
        task_brief="task B",
        runtime_facts=aux_prompts.build_task_quality_guard_runtime_facts(
            persona="persona B",
            env_helper_kind_line="env B",
            helper_kind_scope_facts="kind B",
            project_kind_principle="project B",
            existing_block_counts={"b": 3},
            existing_kind_block_counts={"b": 4},
        ),
    )

    shape_a = describe_prompt_cache_input(
        messages=[{"role": "system", "content": system_a}, {"role": "user", "content": user_a}]
    )
    shape_b = describe_prompt_cache_input(
        messages=[{"role": "system", "content": system_b}, {"role": "user", "content": user_b}]
    )

    assert system_a == system_b
    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert '"existing_block_counts":{"a":1}' in user_a


def test_round3_plan_and_evidence_do_not_change_system_prefix() -> None:
    from app.core import context
    from app.schemas.api import ResponsePlan

    persona = "You are bot. Answer directly.\n你是 bot，直接回答。"
    messages_a = context.round3_messages(
        persona,
        ResponsePlan(
            intent="Summarize project A",
            key_points=["A uses FastAPI", "A has memory modules"],
            tone="concise",
            length_hint="short",
        ),
        "User",
        "What is project A?",
        files=[("report_a.md", "/files/report_a.md")],
        in_flight_others=[("u2", "Alice")],
        recent_group_messages=[
            {
                "created_at": "2026-06-01T10:00:00",
                "id": 1,
                "user_id": "u2",
                "user_name": "Alice",
                "content": "A recent chat fact",
                "addressed_bot": True,
                }
            ],
            helper_reports_excerpt=[{"task_id": "read_a", "excerpt": "A evidence"}],
            light=False,
        )
    messages_b = context.round3_messages(
        persona,
        ResponsePlan(
            intent="Summarize benchmark B",
            key_points=["B compares trees", "B writes a paper"],
            tone="formal",
            length_hint="medium",
            deliverables=["paper_b.docx"],
            delivery_partial=["missing_chart.png"],
        ),
        "User",
        "What is benchmark B?",
        files=[],
        in_flight_others=[("u3", "Bob")],
        recent_group_messages=[
            {
                "created_at": "2026-06-01T11:00:00",
                "id": 2,
                "user_id": "u3",
                "user_name": "Bob",
                "content": "B recent chat fact",
                "addressed_bot": False,
                }
            ],
            helper_reports_excerpt=[{"task_id": "read_b", "excerpt": "B evidence"}],
            light=False,
        )

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)
    system_a = messages_a[0]["content"]
    user_a = "\n".join(m["content"] for m in messages_a if m["role"] == "user")

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["system_dynamic_hash"] == shape_b["system_dynamic_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert "Summarize project A" not in system_a
    assert "Summarize project A" in user_a
    assert "Work evidence source 1" in user_a
    assert "A evidence" in user_a
    assert "Producer evidence" not in user_a
    assert "report_a.md" not in system_a
    assert "report_a.md" in user_a
    assert "Alice" not in system_a
    assert "Alice" in user_a
    assert "A recent chat fact" not in system_a
    assert "A recent chat fact" in user_a


def test_meta_judge_dynamic_facts_do_not_change_system_prefix() -> None:
    from app.llm.client import META_JUDGE_SYSTEM, _meta_judge_user_payload

    messages_a = [
        {"role": "system", "content": META_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": _meta_judge_user_payload(
                current_level="medium",
                current_iter=8,
                next_level="hard",
                timeline="iter1 read ok\niter2 test failed",
            ),
        },
    ]
    messages_b = [
        {"role": "system", "content": META_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": _meta_judge_user_payload(
                current_level="hard",
                current_iter=12,
                next_level="veryhard",
                timeline="iter1 edit ok\niter2 test passed",
            ),
        },
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert '"current_iter":8' in messages_a[1]["content"]
    assert '"tool_timeline":"iter1 read ok\\niter2 test failed"' in messages_a[1]["content"]


def test_debug_report_dynamic_facts_do_not_change_system_prefix() -> None:
    from app.core.debug import DEBUG_REPORT_SYSTEM, _debug_report_user_payload

    messages_a = [
        {"role": "system", "content": DEBUG_REPORT_SYSTEM},
        {"role": "user", "content": _debug_report_user_payload(["tool.read ok", "WARN retry"])},
    ]
    messages_b = [
        {"role": "system", "content": DEBUG_REPORT_SYSTEM},
        {"role": "user", "content": _debug_report_user_payload(["workspace write ok"])},
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert '"events":["tool.read ok","WARN retry"]' in messages_a[1]["content"]


async def test_debug_report_logs_prompt_cache_shape(monkeypatch) -> None:
    from app.core import debug

    captured: dict[str, object] = {}

    class _Message:
        content = "正在核对内容"

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _Response()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    async def fake_retry(call, **kwargs):
        captured["retry_label"] = kwargs.get("label")
        return await call()

    def fake_shape(**kwargs):
        captured["shape"] = kwargs

    monkeypatch.setattr(debug, "_emit_console", lambda *args, **kwargs: None)
    monkeypatch.setattr(debug, "_write_file", lambda *args, **kwargs: None)

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: _Client())
    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client, "_log_prompt_cache_shape", fake_shape)

    await debug._do_report(["tool.read ok", "workspace write ok"])

    assert captured["retry_label"] == "debug.report"
    assert captured["shape"]["label"] == "debug.report"
    assert captured["shape"]["messages"] == captured["messages"]
    assert captured["messages"][0]["content"] == debug.DEBUG_REPORT_SYSTEM


def test_delegate_progress_summary_dynamic_facts_do_not_change_system_prefix() -> None:
    from app.llm.tools.delegate import (
        DELEGATE_PROGRESS_SUMMARY_SYSTEM,
        _delegate_progress_summary_user_payload,
    )

    messages_a = [
        {"role": "system", "content": DELEGATE_PROGRESS_SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": _delegate_progress_summary_user_payload([
                {
                    "elapsed_seconds": 12.3,
                    "heartbeat_status": "fresh",
                    "iter": 2,
                    "last_thought": "reading",
                    "recent_tools": ["read_file"],
                    "task_id": "read_a",
                }
            ]),
        },
    ]
    messages_b = [
        {"role": "system", "content": DELEGATE_PROGRESS_SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": _delegate_progress_summary_user_payload([
                {
                    "elapsed_seconds": 41.0,
                    "heartbeat_status": "fresh",
                    "iter": 5,
                    "last_thought": "testing",
                    "recent_tools": ["bash", "python"],
                    "task_id": "code_b",
                }
            ]),
        },
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert '"task_id":"read_a"' in messages_a[1]["content"]


def test_voice_delivery_classifier_dynamic_facts_do_not_change_system_prefix() -> None:
    from app.llm import aux_prompts

    messages_a = [
        {"role": "system", "content": aux_prompts.VOICE_DELIVERY_CLASSIFIER_SYSTEM},
        {
            "role": "user",
            "content": aux_prompts.VOICE_DELIVERY_CLASSIFIER_USER_TEMPLATE.format(
                plan_intent="answer a short greeting",
                plan_length="short",
                plan_tone="friendly",
                plan_key_points="- greet briefly",
                plan_avoid="(none)",
                plan_deliverables="(none)",
                projected_reply_shape="length_hint=short; key_points=1; deliverables=0",
                projected_reply_shape_facts=(
                    "length_hint=short; key_point_count=1; deliverable_count=0; "
                    "partial_delivery_count=0; has_user_facing_files=no"
                ),
                candidate_output_previews="- voice candidate preview (partial, chars=3): hi",
                delivery_state="no",
                persona_context="persona A",
                recent_context="user: hi",
                user_message="用语音回复我",
                voice_preference=0.8,
                preference_hint="prefer voice",
            ),
        },
    ]
    messages_b = [
        {"role": "system", "content": aux_prompts.VOICE_DELIVERY_CLASSIFIER_SYSTEM},
        {
            "role": "user",
            "content": aux_prompts.VOICE_DELIVERY_CLASSIFIER_USER_TEMPLATE.format(
                plan_intent="deliver a long structured report",
                plan_length="long",
                plan_tone="direct",
                plan_key_points="- summarize findings\n- include table",
                plan_avoid="(none)",
                plan_deliverables="- report.xlsx",
                projected_reply_shape="length_hint=long; key_points=2; deliverables=1",
                projected_reply_shape_facts=(
                    "length_hint=long; key_point_count=2; deliverable_count=1; "
                    "partial_delivery_count=0; has_user_facing_files=yes; likely_readable=yes"
                ),
                candidate_output_previews="- voice candidate preview (partial, chars=120): long structured report",
                delivery_state="yes",
                persona_context="persona B",
                recent_context="user: report please",
                user_message="文字回复，附表格",
                voice_preference=0.2,
                preference_hint="prefer text",
            ),
        },
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert "用语音回复我" in messages_a[1]["content"]


def test_user_profile_extraction_dynamic_facts_do_not_change_system_prefix() -> None:
    from app.core.user_profile_maintenance import (
        PROFILE_EXTRACTION_SYSTEM,
        _build_profile_extraction_user_payload,
    )

    messages_a = [
        {"role": "system", "content": PROFILE_EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": _build_profile_extraction_user_payload(
                "以后回答短一点",
                "好的，我会更简洁。",
            ),
        },
    ]
    messages_b = [
        {"role": "system", "content": PROFILE_EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": _build_profile_extraction_user_payload(
                "我主要维护 Python 工程",
                "明白。",
            ),
        },
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
    assert '"user":"以后回答短一点"' in messages_a[1]["content"]


def test_helper_same_kind_system_prefix_is_task_independent() -> None:
    from app.llm.tools.delegate import _select_helper_system

    first = _select_helper_system("read", "easy")
    second = _select_helper_system("read", "easy")

    assert first == second
    assert "## Dynamic Helper Context" not in first
    assert "Current Message" not in first


def test_helper_hard_mode_is_easy_prefix_plus_fixed_suffix() -> None:
    from app.llm.tools.delegate import _HARD_MODE_SUFFIX, _select_helper_system

    for kind in ("code", "read", "edit", "verify", "draw", "tts", "project_map", "file_summary", "impact_review", "inventory"):
        easy = _select_helper_system(kind, "easy")
        hard = _select_helper_system(kind, "hard")
        assert hard == easy + "\n\n" + _HARD_MODE_SUFFIX


def test_helper_user_prompt_keeps_latest_task_after_dynamic_context() -> None:
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    first_dynamic_parts = [
        "## Available Skills\n- read_skill: workspace-deep-dive",
        "## Workspace Listing\n- _env/README.md\n- _env/src/app.py",
    ]
    second_dynamic_parts = [
        "## Available Skills\n- read_skill: workspace-deep-dive",
        "## Workspace Listing\n- _env/README.md\n- _env/src/api.py",
    ]
    first = _build_helper_user_prompt(
        prompt="Read README and summarize the project.",
        dynamic_prompt_prefix_parts=first_dynamic_parts,
    )
    second = _build_helper_user_prompt(
        prompt="Read README and summarize the project.",
        dynamic_prompt_prefix_parts=second_dynamic_parts,
    )

    assert "\n\n---\n\n## Dynamic Helper Context\n" in first
    assert first.split("## Dynamic Helper Context\n", 1)[0] == second.split("## Dynamic Helper Context\n", 1)[0]
    assert "Read README and summarize the project." not in first.split("## Dynamic Helper Context\n", 1)[0]
    assert "_env/src/app.py" in first.split("## Dynamic Helper Context\n", 1)[1]
    assert "_env/src/api.py" in second.split("## Dynamic Helper Context\n", 1)[1]
    assert "\n\n---\n\n## Task\nRead README and summarize the project." in first
    assert first.rfind("Read README and summarize the project.") > first.find("## Dynamic Helper Context")


def test_helper_user_prompt_resume_facts_stay_in_user_tail() -> None:
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    prompt = _build_helper_user_prompt(
        prompt="Continue the benchmark.",
        dynamic_prompt_prefix_parts=["## Workspace Listing\n- bench.py"],
        prior_summary="compiled ok; benchmark pending",
        resume=True,
    )

    assert "## Progress Report From Previous Interruption\n" in prompt
    assert "\n\n---\n\n## Dynamic Helper Context\n" in prompt
    assert "## Progress Report From Previous Interruption" in prompt
    assert "## Main Process Continuation Request\nContinue the benchmark." in prompt
    assert "compiled ok; benchmark pending" in prompt


def test_helper_real_prompt_system_prefix_is_task_independent() -> None:
    from app.llm.tools.delegate import _select_helper_system
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    for mode in ("easy", "hard"):
        messages_a = [
            {"role": "system", "content": _select_helper_system("read", mode)},
            {
                "role": "user",
                "content": _build_helper_user_prompt(
                    prompt="Read A.docx and summarize speaking materials.",
                    dynamic_prompt_prefix_parts=["## Workspace Listing\n- A.docx\n- B.pdf"],
                ),
            },
        ]
        messages_b = [
            {"role": "system", "content": _select_helper_system("read", mode)},
            {
                "role": "user",
                "content": _build_helper_user_prompt(
                    prompt="Read B.pdf and summarize writing materials.",
                    dynamic_prompt_prefix_parts=["## Workspace Listing\n- A.docx\n- B.pdf"],
                ),
            },
        ]
        shape_a = describe_prompt_cache_input(messages=messages_a, tools=[])
        shape_b = describe_prompt_cache_input(messages=messages_b, tools=[])

        assert shape_a["system_prompt_hash"] == shape_b["system_prompt_hash"]
        assert shape_a["system_dynamic_hash"] == shape_b["system_dynamic_hash"]
        assert shape_a["hash_chain"][2]["bytes"] == 0
        assert shape_a["messages_hash"] != shape_b["messages_hash"]


def test_helper_resume_state_does_not_change_system_prefix() -> None:
    from app.llm.tools.delegate import _select_helper_system
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    system = _select_helper_system("read", "easy")
    fresh_messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_helper_user_prompt(
                prompt="Read the material index and report coverage.",
                dynamic_prompt_prefix_parts=["## Workspace Listing\n- index.md"],
                resume=False,
                kind="read",
            ),
        },
    ]
    empty_resume_messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_helper_user_prompt(
                prompt="Continue reading the material index.",
                dynamic_prompt_prefix_parts=["## Workspace Listing\n- index.md"],
                resume=True,
                kind="read",
            ),
        },
    ]
    prior_summary_messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_helper_user_prompt(
                prompt="Continue reading the material index.",
                dynamic_prompt_prefix_parts=["## Workspace Listing\n- index.md\n- notes.txt"],
                prior_summary="covered chapter 1; chapter 2 remains",
                resume=True,
                kind="read",
            ),
        },
    ]

    fresh_shape = describe_prompt_cache_input(messages=fresh_messages, tools=[])
    empty_resume_shape = describe_prompt_cache_input(messages=empty_resume_messages, tools=[])
    prior_summary_shape = describe_prompt_cache_input(messages=prior_summary_messages, tools=[])

    assert fresh_shape["system_static_hash"] == empty_resume_shape["system_static_hash"]
    assert fresh_shape["system_static_hash"] == prior_summary_shape["system_static_hash"]
    assert fresh_shape["hash_chain"][0]["hash"] == empty_resume_shape["hash_chain"][0]["hash"]
    assert fresh_shape["hash_chain"][0]["hash"] == prior_summary_shape["hash_chain"][0]["hash"]
    assert fresh_shape["messages_hash"] != empty_resume_shape["messages_hash"]
    assert empty_resume_shape["messages_hash"] != prior_summary_shape["messages_hash"]
    assert "## Resume Task" not in system
    assert "## Progress Report From Previous Interruption" in prior_summary_messages[1]["content"]
    assert "## Resume Requested, But Workspace Is Empty" in empty_resume_messages[1]["content"]


def test_real_helper_runner_keeps_resume_state_out_of_system_prompt() -> None:
    import inspect

    from app.llm.tools import delegate_runner

    source = inspect.getsource(delegate_runner._run_one_helper)

    assert "_select_helper_system(kind, mode) + _HELPER_RESUME_HINT" not in source
    assert "sys_prompt = _select_helper_system(kind, mode)" in source
    assert "resume=resume" in source
    assert "resume_workspace_empty=resume_workspace_empty" in source
    assert "prior_summary=prior_summary" in source


def test_helper_latest_task_stays_after_dynamic_snapshot() -> None:
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    prompt_a = _build_helper_user_prompt(
        prompt="Read the provided files and summarize cache modules.",
        dynamic_prompt_prefix_parts=["## Helper Workspace Snapshot\n- old.txt"],
    )
    prompt_b = _build_helper_user_prompt(
        prompt="Read the provided files and summarize cache modules.",
        dynamic_prompt_prefix_parts=["## Helper Workspace Snapshot\n- new.txt"],
    )

    prefix_a = prompt_a.split("## Dynamic Helper Context\n", 1)[0]
    prefix_b = prompt_b.split("## Dynamic Helper Context\n", 1)[0]
    assert prefix_a == prefix_b
    assert "## Task\n" not in prefix_a
    assert "summarize cache modules" not in prefix_a
    assert "old.txt" not in prefix_a
    assert "new.txt" not in prefix_b
    assert "## Task\nRead the provided files and summarize cache modules." in prompt_a
    assert prompt_a.rfind("summarize cache modules") > prompt_a.find("## Dynamic Helper Context")


def test_helper_shared_dynamic_context_extends_prefix_before_task_divergence() -> None:
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    dynamic_parts = [
        "## Available Skills\n- read_skill: workspace-deep-dive",
        "## Workspace Listing\n- _env/README.md\n- _env/src/app.py",
    ]
    prompt_a = _build_helper_user_prompt(
        prompt="Summarize the API entry points.",
        dynamic_prompt_prefix_parts=dynamic_parts,
        kind="read",
    )
    prompt_b = _build_helper_user_prompt(
        prompt="Summarize the storage layer.",
        dynamic_prompt_prefix_parts=dynamic_parts,
        kind="read",
    )

    common_a = prompt_a.split("\n\n---\n\n## Task\n", 1)[0]
    common_b = prompt_b.split("\n\n---\n\n## Task\n", 1)[0]

    assert common_a == common_b
    assert "## Dynamic Helper Context" in common_a
    assert "_env/src/app.py" in common_a
    assert "Summarize the API entry points." not in common_a
    assert "Summarize the storage layer." not in common_b


def test_helper_auxiliary_context_stays_before_latest_task_tail() -> None:
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    prompt = _build_helper_user_prompt(
        prompt="Build benchmark.c using framework.h and report results.",
        dynamic_prompt_prefix_parts=[
            "## Dependency File Paths (system-provided)\n- `framework.h` -> `_helpers_shared/framework/framework.h`",
            "## Referenced File Availability Facts\n- `missing.csv`",
            "## Hard Mode Runtime Focus\nUse a stricter same-kind validation pass.",
        ],
        kind="code",
    )

    before_task, task_tail = prompt.split("\n\n---\n\n## Task\n", 1)

    assert "## Dependency File Paths" in before_task
    assert "## Referenced File Availability Facts" in before_task
    assert "## Hard Mode Runtime Focus" in before_task
    assert "## Dependency File Paths" not in task_tail
    assert "## Referenced File Availability Facts" not in task_tail
    assert "## Hard Mode Runtime Focus" not in task_tail
    assert task_tail.startswith("Build benchmark.c using framework.h and report results.")


def test_helper_task_contract_snapshot_stays_after_latest_task_tail() -> None:
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    prompt = _build_helper_user_prompt(
        prompt="Write the final DOCX from the verified outline.",
        dynamic_prompt_prefix_parts=["## Workspace Listing\n- outline.md\n- evidence.json"],
        task_contract_context=(
            '{\n'
            '  "task_id": "writer",\n'
            '  "expected_outputs": ["final.docx"],\n'
            '  "acceptance_checks": ["docx opens and follows outline"]\n'
            '}'
        ),
        kind="edit",
    )

    before_task, after_task = prompt.split("\n\n---\n\n## Task\n", 1)
    task_block, contract_block = after_task.split("\n\n---\n\n## Helper Task Contract Snapshot\n", 1)

    assert "## Dynamic Helper Context" in before_task
    assert "outline.md" in before_task
    assert "final.docx" in contract_block
    assert "docx opens and follows outline" in contract_block
    assert "Write the final DOCX from the verified outline." not in before_task
    assert "Write the final DOCX from the verified outline." in task_block
    assert task_block.startswith("Write the final DOCX from the verified outline.")


def test_core_long_workflow_prefix_shape_is_stable_across_dynamic_tasks() -> None:
    from app.core import context as ctx_build
    from app.llm.tools.delegate import _select_helper_system
    from app.llm.tools.delegate_runner import _build_helper_user_prompt
    from app.llm.tools.registry import tools_for_runtime_mode
    from app.schemas.api import ResponsePlan

    tools = tools_for_runtime_mode("environment")

    def base(current_message: str, filename: str) -> list[dict]:
        return ctx_build.build_base_context(
            user_name="A",
            current_message=current_message,
            hot_user=[],
            hot_group=[],
            warm_user_index=[],
            warm_group_index=[{"id": "w1", "headline": f"warm {filename}"}],
            cold_user_topk=[],
            cold_group_topk=[],
            kb_topk=[{"id": "kb1", "headline": f"kb {filename}"}],
            file_index=[{
                "id": "f1",
                "filename": filename,
                "headline": f"file {filename}",
                "uploader_name": "A",
                "file_size": 1,
                "download_status": "done",
            }],
        )

    round1_a = ctx_build.round1_messages(base("Route A", "A.csv"))
    round1_b = ctx_build.round1_messages(base("Route B", "B.csv"))
    round2_a = ctx_build.round2_messages(
        base("Analyze A", "A.csv"),
        {"complexity": "hard", "needs_tools": True, "rationale": "A"},
        workspace_listing=["A.csv"],
        needs_tools=True,
        needs_recall=True,
    )
    round2_b = ctx_build.round2_messages(
        base("Analyze B", "B.csv"),
        {"complexity": "normal", "needs_tools": True, "rationale": "B"},
        workspace_listing=["B.csv"],
        needs_tools=True,
        needs_recall=True,
    )
    round3_a = ctx_build.round3_messages(
        "You are bot. Answer directly.\n你是 bot，直接回答。",
        ResponsePlan(intent="A result", key_points=["A evidence"], tone="plain", length_hint="short"),
        "A",
        "Show A",
        files=[("A.docx", "/files/A.docx")],
        helper_reports_excerpt=[{"task_id": "read_a", "excerpt": "A details"}],
    )
    round3_b = ctx_build.round3_messages(
        "You are bot. Answer directly.\n你是 bot，直接回答。",
        ResponsePlan(intent="B result", key_points=["B evidence"], tone="plain", length_hint="short"),
        "A",
        "Show B",
        files=[("B.docx", "/files/B.docx")],
        helper_reports_excerpt=[{"task_id": "read_b", "excerpt": "B details"}],
    )
    helper_a = [
        {"role": "system", "content": _select_helper_system("read", "easy")},
        {
            "role": "user",
            "content": _build_helper_user_prompt(
                prompt="Read A.csv and summarize evidence.",
                dynamic_prompt_prefix_parts=["## Workspace Listing\n- A.csv"],
                kind="read",
            ),
        },
    ]
    helper_b = [
        {"role": "system", "content": _select_helper_system("read", "easy")},
        {
            "role": "user",
            "content": _build_helper_user_prompt(
                prompt="Read B.csv and summarize evidence.",
                dynamic_prompt_prefix_parts=["## Workspace Listing\n- B.csv"],
                kind="read",
            ),
        },
    ]

    pairs = [
        ("round1", round1_a, round1_b, []),
        ("round2", round2_a, round2_b, tools),
        ("round3", round3_a, round3_b, []),
        ("helper.read", helper_a, helper_b, tools),
    ]

    for label, messages_a, messages_b, pair_tools in pairs:
        shape_a = describe_prompt_cache_input(messages=messages_a, tools=pair_tools)
        shape_b = describe_prompt_cache_input(messages=messages_b, tools=pair_tools)
        assert shape_a["system_static_hash"] == shape_b["system_static_hash"], label
        assert shape_a["system_dynamic_hash"] == shape_b["system_dynamic_hash"], label
        assert shape_a["tool_schema_hash"] == shape_b["tool_schema_hash"], label
        assert shape_a["hash_chain"][0]["hash"] == shape_b["hash_chain"][0]["hash"], label
        assert shape_a["hash_chain"][1]["hash"] == shape_b["hash_chain"][1]["hash"], label
        assert shape_a["messages_hash"] != shape_b["messages_hash"], label


def test_serialized_prompt_shape_keeps_task_facts_after_large_shared_prefix() -> None:
    """Local structure diagnostic only; provider cache hit rate must come from logs."""
    from app.core import context as ctx_build
    from app.core.prompt_cache_observer import compare_prompt_cache_prefix, describe_prompt_cache_input
    from app.llm.tools.registry import tools_for_runtime_mode

    tools = tools_for_runtime_mode("environment")
    shared_history = [
        "User asked for a cache audit.",
        "Assistant inspected prompt layering.",
        "Main process kept current task facts in the dynamic tail.",
    ]
    large_shared_context = [
        {
            "id": f"kb{i}",
            "headline": f"cache and prompt layering note {i}: stable policy stays before dynamic task facts",
        }
        for i in range(80)
    ]
    large_workspace = [f"src/module_{i:03d}.py" for i in range(500)]

    def build(current_message: str) -> list[dict]:
        base = ctx_build.build_base_context(
            user_name="A",
            current_message=current_message,
            hot_user=[],
            hot_group=[],
            warm_user_index=[
                {
                    "id": f"w{i}",
                    "headline": text,
                    "timestamp": f"2026-06-01T00:0{i}:00",
                    "tendencies": {},
                }
                for i, text in enumerate(shared_history)
            ],
            warm_group_index=[],
            cold_user_topk=[],
            cold_group_topk=[],
            kb_topk=large_shared_context,
            file_index=[],
        )
        return ctx_build.round2_messages(
            base,
            {"complexity": "hard", "needs_tools": True, "needs_recall": True, "rationale": "shared cache audit"},
            workspace_listing=large_workspace,
            needs_tools=True,
            needs_recall=True,
        )

    messages_a = build("Continue the cache audit and focus on Round3 dynamic facts.")
    messages_b = build("Continue the cache audit and focus on helper prompt cache reuse.")

    shape_a = describe_prompt_cache_input(messages=messages_a, tools=tools)
    shape_b = describe_prompt_cache_input(messages=messages_b, tools=tools)
    comparison = compare_prompt_cache_prefix(
        left_messages=messages_a,
        right_messages=messages_b,
        left_tools=tools,
        right_tools=tools,
    )

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["tool_schema_hash"] == shape_b["tool_schema_hash"]
    assert "Round3 dynamic facts" not in messages_a[0]["content"]
    assert "helper prompt cache reuse" not in messages_b[0]["content"]
    assert comparison["common_prefix_ratio"] >= 0.99
    assert comparison["common_prefix_percent"] > 99.0


def test_direct_nonstream_llm_calls_log_shape_and_usage() -> None:
    import inspect
    from app.core import debug, orchestrator, user_profile_maintenance
    from app.llm import client as llm_client
    from app.llm.tools import delegate

    sources = [
        inspect.getsource(debug._do_report),
        inspect.getsource(user_profile_maintenance.bg_user_profile_update),
        inspect.getsource(delegate._generate_progress_summaries),
        inspect.getsource(orchestrator._self_check_plan),
        inspect.getsource(llm_client.chat_with_tools),
    ]

    for src in sources:
        assert "chat.completions.create" in src
        assert "_log_prompt_cache_shape" in src
        assert "_record_response_usage" in src


def test_direct_llm_completion_calls_are_cache_observable() -> None:
    """Guard against new direct completion calls bypassing cache observability."""

    root = Path(__file__).resolve().parents[1]
    usage_markers = (
        "_record_response_usage",
        "_record_usage_payload",
        "_record_llm_usage_and_log",
        "_record_nonstream_response_usage",
    )
    shape_markers = (
        "_log_prompt_cache_shape",
        "_log_nonstream_prompt_shape",
    )
    findings: list[str] = []

    def attr_chain(node: ast.AST) -> str:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    def children_with_parents(tree: ast.AST) -> None:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                setattr(child, "_parent", parent)

    def outer_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        cur = node
        while True:
            cur = getattr(cur, "_parent", None)
            if cur is None:
                break
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append(cur)
        return funcs[-1] if funcs else None

    for path in sorted((root / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        children_with_parents(tree)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not attr_chain(node.func).endswith("chat.completions.create"):
                continue
            scope = outer_function(node)
            if scope is None:
                findings.append(f"{path.relative_to(root)}:{node.lineno}: call outside function")
                continue
            end_lineno = getattr(scope, "end_lineno", scope.lineno)
            scope_source = "\n".join(lines[scope.lineno - 1:end_lineno])
            missing: list[str] = []
            if not any(marker in scope_source for marker in shape_markers):
                missing.append("shape")
            if not any(marker in scope_source for marker in usage_markers):
                missing.append("usage")
            if missing:
                findings.append(
                    f"{path.relative_to(root)}:{node.lineno} in {scope.name}: missing {','.join(missing)}"
                )

    assert findings == []
