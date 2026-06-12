from __future__ import annotations

from pathlib import Path


def test_helper_system_prompt_excludes_turn_dynamic_context() -> None:
    from app.llm.tools.delegate import _select_helper_system

    system_prompt = _select_helper_system("code", "easy")

    assert "## Helper Consistency Contract" in system_prompt
    assert "## Actual Tool Boundary" in system_prompt
    assert "## Dynamic Helper Context" not in system_prompt
    assert "## On-Demand Skills" not in system_prompt
    assert "## Helper Workspace Snapshot" not in system_prompt
    assert "## Output Language" not in system_prompt
    assert "Dependency File Paths" not in system_prompt


def test_helper_dynamic_context_holds_workspace_skills_and_language(tmp_path: Path) -> None:
    from app.llm.tools.helper_text import _helper_lang_hint
    from app.llm.tools.skills import _build_skills_listing
    from app.llm.tools.workspace_utils import _list_helper_workspace_for_prompt

    ws = tmp_path / "_delegate_code"
    ws.mkdir()
    (ws / "input.txt").write_text("hello", encoding="utf-8")

    dynamic_context = "\n\n".join(
        part.strip()
        for part in (
            _helper_lang_hint("zh"),
            _build_skills_listing(),
            _list_helper_workspace_for_prompt(str(ws)),
        )
        if part.strip()
    )

    assert "## Output Language" in dynamic_context
    assert "## On-Demand Skills" in dynamic_context
    assert "## Helper Workspace Snapshot" in dynamic_context
    assert "input.txt" in dynamic_context


def test_llm_metrics_records_helper_kind_aggregate(monkeypatch) -> None:
    from app.core import metrics
    from app.llm import client

    metrics.reset()
    cache_logs: list[str] = []

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)
        return None

    monkeypatch.setattr(
        "app.core.core_processes.current_helper_kind",
        lambda: "read",
    )
    monkeypatch.setattr(client.debug, "log", fake_debug_log)
    client._record_stream_usage_for_test(
        model="unit-model",
        task_id="read_ielts",
        prompt_tokens=100,
        completion_tokens=20,
        cache_hit=80,
        cache_miss=20,
    )

    snap = metrics.snapshot()
    assert snap["calls"]["helper.read_ielts|unit-model"] == 1
    assert snap["calls"]["helper_kind.read|unit-model"] == 1
    assert snap["cache_hit_tokens"]["helper_kind.read|unit-model"] == 80
    assert cache_logs == [
        "P49 [helper.read_ielts]: model=unit-model prompt=100 completion=20 "
        "cache_hit=80 cache_miss=20 hit_rate=80%",
        "P49 [helper_kind.read]: model=unit-model prompt=100 completion=20 "
        "cache_hit=80 cache_miss=20 hit_rate=80%",
    ]


def test_prompt_shape_metrics_record_local_cache_estimates(monkeypatch) -> None:
    from app.core import metrics
    from app.llm import client

    metrics.reset()
    monkeypatch.setattr(client.debug, "log", lambda *args, **kwargs: None)
    client._log_prompt_cache_shape(
        label="tools_loop.iter1.main",
        model="unit-model",
        messages=[
            {"role": "system", "content": "## Context And Safety Contract\nstable"},
            {"role": "user", "content": "## Current Time\nUTC:2026-06-02 10:00\n\nrequest"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read file.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    snap = metrics.snapshot()
    key = "tools_loop.iter1.main|unit-model"
    assert snap["prompt_shape_calls"][key] == 1
    assert snap["prompt_shape_static_bytes"][key] > 0
    assert snap["prompt_shape_dynamic_bytes"][key] > 0
    assert snap["prompt_shape_cacheable_prefix_bytes"][key] == snap["prompt_shape_static_bytes"][key]

    rendered = metrics.render_prometheus()
    assert "llm_prompt_shape_static_bytes_total" in rendered
    assert 'tag="tools_loop.iter1.main",model="unit-model"' in rendered


def test_prompt_shape_local_lcp_is_trace_isolated(monkeypatch) -> None:
    from app.core import debug
    from app.core.prompt_cache_observer import serialized_prompt_cache_input
    from app.llm import client

    logs = []
    monkeypatch.setattr(client.debug, "log", lambda category, msg="", payload=None: logs.append((category, payload)))
    client._last_prompt_shape_serialized.clear()

    messages_a = [
        {"role": "system", "content": "## Context And Safety Contract\nstable"},
        {"role": "user", "content": "trace A request"},
    ]
    messages_b = [
        {"role": "system", "content": "## Context And Safety Contract\nother"},
        {"role": "user", "content": "trace B request with different prefix"},
    ]

    debug.set_trace_id("trace-a")
    client._log_prompt_cache_shape(label="chat_stream", model="unit-model", messages=messages_a)
    debug.set_trace_id("trace-b")
    client._log_prompt_cache_shape(label="chat_stream", model="unit-model", messages=messages_b)
    debug.set_trace_id("trace-a")
    client._log_prompt_cache_shape(label="chat_stream", model="unit-model", messages=messages_a)

    lcp_payloads = [payload for category, payload in logs if category == "llm.prompt_cache_lcp"]
    assert len(lcp_payloads) == 1
    assert lcp_payloads[0]["trace_id"] == "trace-a"
    assert lcp_payloads[0]["common_prefix_bytes"] == len(
        serialized_prompt_cache_input(messages=messages_a)
    )
