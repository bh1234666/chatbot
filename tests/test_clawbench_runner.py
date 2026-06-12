import subprocess
import sys
import asyncio
import argparse
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLAWBENCH_ROOT = PROJECT_ROOT / ".benchmarks" / "clawbench_original_agent"
for _path in (str(PROJECT_ROOT), str(CLAWBENCH_ROOT)):
    while _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, str(CLAWBENCH_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.mark.skipif(sys.platform != "win32", reason="python3.cmd shim is Windows-specific")
def test_clawbench_python3_shim_uses_lf_stdout(tmp_path):
    from stress_tools.run_clawbench_current_agent_scored import _write_python3_shim

    script = tmp_path / "emit.py"
    script.write_text("print('alpha')\n", encoding="utf-8", newline="\n")
    _write_python3_shim(tmp_path)
    assert (tmp_path / "pytest.cmd").exists()

    result = subprocess.run(
        ["cmd", "/c", "python3.cmd", "emit.py"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert result.stdout == b"alpha\n"
    assert result.stderr == b""

    pytest_result = subprocess.run(
        ["cmd", "/c", "pytest.cmd", "--version"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )

    assert "pytest" in pytest_result.stdout.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="node.cmd shim is Windows-specific")
def test_clawbench_node_shim_prefers_bundled_runtime(tmp_path):
    from stress_tools.run_clawbench_current_agent_scored import _bundled_node_paths, _prepare_node_runtime

    node_exe, node_modules = _bundled_node_paths()
    if node_exe is None:
        pytest.skip("bundled Node runtime is unavailable")

    _prepare_node_runtime(tmp_path)
    assert (tmp_path / "node.cmd").exists()

    result = subprocess.run(
        ["cmd", "/c", "node.cmd", "--version"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert result.stdout.startswith(b"v")
    if node_modules is not None:
        resolved = subprocess.run(
            ["cmd", "/c", "node.cmd", "-e", "console.log(require.resolve('playwright'))"],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        assert "playwright" in resolved.stdout.lower()


def test_chatbot_adapter_marks_node_web_verifier_as_browser_family():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    assert (
        adapter._tool_family(
            {
                "name": "env_run",
                "family": "execute",
                "input": {"command": "node verify_form.cjs http://127.0.0.1:1234/"},
            }
        )
        == "browser"
    )


def test_chatbot_adapter_does_not_treat_browser_path_as_browser_family():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    assert (
        adapter._tool_family(
            {
                "name": "bash",
                "family": "execute",
                "input": {"command": "ls stress_tools/runs/t2-browser-form-fix/app.js 2>&1"},
            }
        )
        == "execute"
    )


def test_chatbot_adapter_marks_internal_file_and_plan_tools_as_concrete_families():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    cases = [
        ({"name": "workspace", "family": "unknown", "input": {"action": "write"}}, "edit"),
        ({"name": "workspace", "family": "unknown", "input": {"action": "locate"}}, "read"),
        ({"name": "fetch_to_temp", "family": "unknown", "input": {}}, "read"),
        ({"name": "request_resource", "family": "unknown", "input": {}}, "read"),
        ({"name": "processes", "family": "unknown", "input": {"action": "list"}}, "plan"),
    ]

    for record, expected in cases:
        assert adapter._tool_family(record) == expected


def test_chatbot_adapter_progress_exposes_recovered_tool_failure():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    messages = adapter._progress_messages_from_result({
        "progress": [{"message": "reading local site"}],
        "workflow": [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": False,
                "args": {"command": "curl -s http://127.0.0.1:1234/"},
                "result_preview": '{"ok": false, "returncode": 1}',
            },
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "python fetch.py"},
                "result_preview": '{"ok": true, "returncode": 0}',
            },
        ],
    })

    assert any("Unable to use one attempted env_run call" in message.text for message in messages)


def test_chatbot_adapter_progress_names_acceptance_failure_as_evidence():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    messages = adapter._progress_messages_from_result({
        "progress": [],
        "workflow": [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": False,
                "args": {"command": "python -m pytest tests/test_report_client.py -v"},
                "result_preview": (
                    '{"ok": false, "returncode": 1, "stdout": "4 failed, 2 passed", '
                    '"acceptance_failure_fact": {"kind": "acceptance_failure_fact"}}'
                ),
            },
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "python -m pytest tests/test_report_client.py -v"},
                "result_preview": '{"ok": true, "returncode": 0, "stdout": "6 passed"}',
            },
        ],
    })

    assert any("failing acceptance/check command as task evidence" in message.text for message in messages)
    assert not any("Unable to use one attempted env_run call" in message.text for message in messages)


def test_chatbot_adapter_progress_omits_unrecovered_failure_note():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    messages = adapter._progress_messages_from_result({
        "progress": [],
        "workflow": [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": False,
                "args": {"command": "curl -s http://127.0.0.1:1234/"},
                "result_preview": '{"ok": false, "returncode": 1}',
            },
        ],
    })

    assert messages == []


def test_chatbot_adapter_marks_memory_expansion_tools_as_memory_family():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    for name in ("expand_warm", "expand_cold", "expand_kb", "mark_avoid_mention"):
        assert adapter._tool_family({"name": name, "family": "other", "input": {}}) == "memory"


def test_chatbot_adapter_marks_memory_agent_state_as_memory_family():
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    for payload in (
        {
            "action": "upsert_contract",
            "task_id": "memory/beta-regions",
            "goal": "Feature flag: Beta rollout regions = us, eu",
        },
        {
            "action": "add_evidence",
            "task_id": "beta-regions",
            "summary": "Beta rollout regions: us, eu",
            "kind": "evidence",
        },
    ):
        assert (
            adapter._tool_family(
                {
                    "name": "agent_state",
                    "family": "unknown",
                    "input": payload,
                }
            )
            == "memory"
        )


def test_chatbot_adapter_maps_progress_events_to_transcript_messages():
    from clawbench.scorer import evaluate_behavior
    from clawbench.schemas import BehaviorExpectations, Transcript
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    adapter = ChatbotAdapter()
    progress = adapter._progress_messages_from_result(
        {
            "progress": [
                {"message": "正在读取项目文件"},
                {"message": "正在运行验证"},
                {"message": "正在运行验证"},
                {"stage": "final_check"},
            ]
        }
    )

    transcript = Transcript(messages=progress)
    result = evaluate_behavior(
        BehaviorExpectations(require_plan=True, require_progress_updates=True),
        transcript,
    )

    assert [message.role for message in progress] == ["assistant", "assistant", "assistant"]
    assert progress[0].text.startswith("Plan/progress: checking")
    assert result.failed_expectations == []


def test_clawbench_transcript_memory_search_matches_regex_query():
    from clawbench.schemas import ToolCall, Transcript, TranscriptMessage
    from stress_tools.run_clawbench_current_agent_scored import _TranscriptMemorySearchClient

    transcript = Transcript(
        messages=[
            TranscriptMessage(
                role="assistant",
                text="stored",
                tool_calls=[
                    ToolCall(
                        name="expand_cold",
                        family="memory",
                        input={"ids": ["c_beta-regions"]},
                        output="No requested cold-memory IDs matched",
                        success=True,
                    ),
                    ToolCall(
                        name="agent_state",
                        family="unknown",
                        input={
                            "action": "upsert_contract",
                            "task_id": "beta-regions",
                            "goal": "Beta rollout regions flag: BETA_REGIONS = [\"us\", \"eu\"]",
                            "acceptance": ["Flag BETA_REGIONS with value ['us', 'eu']"],
                            "current_stage": "stored",
                        },
                        success=True,
                    )
                ],
            )
        ]
    )

    result = asyncio.run(
        _TranscriptMemorySearchClient(transcript)._rpc(
            "memory.search",
            {"query": "(?i)beta.*region|region.*beta", "limit": 20},
        )
    )

    entries = result["payload"]["entries"]
    assert len(entries) == 1
    assert entries[0]["key"] == "beta-regions"
    assert "us" in entries[0]["value"]
    assert "eu" in entries[0]["value"]


def test_clawbench_memory_search_reads_workspace_memory_files(tmp_path):
    from clawbench.schemas import Transcript
    from stress_tools.run_clawbench_current_agent_scored import _TranscriptMemorySearchClient

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "notes.md").write_text(
        "beta-regions: us, eu\nretry-budget: 3\napac-gating: 2026.3\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        _TranscriptMemorySearchClient(Transcript(), tmp_path)._rpc(
            "memory.search",
            {"query": "(?i)retry.*budget|budget.*retry", "limit": 20},
        )
    )

    entries = result["payload"]["entries"]
    assert len(entries) == 1
    assert entries[0]["key"] == "memory/notes.md"
    assert "3" in entries[0]["value"]


def test_chatbot_adapter_drives_dynamic_multi_turn_phase(tmp_path):
    from clawbench.adapters.base import AdapterContext
    from clawbench.canonical import CanonicalPhase, CanonicalTask
    from clawbench.schemas import SimulatedUser, Transcript, UserTurn
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def ask_environment(self, *, project: Path, message: str, turn: int):
            self.calls.append({"project": project, "message": message, "turn": turn})
            return {
                "ok": True,
                "text": f"assistant saw {message}",
                "workflow": [],
                "command_events": [],
            }

    phase = CanonicalPhase(
        name="main",
        user=SimulatedUser(
            turns=[
                UserTurn(message="first user turn"),
                UserTurn(message="second user turn", after_assistant_turns=1),
            ]
        ),
        timeout_seconds=10,
    )
    task = CanonicalTask(
        id="fake-multi-turn",
        name="fake multi turn",
        tier="tier1",
        family="tools",
        surface="local",
        phases=[phase],
    )
    ctx = AdapterContext(
        task=task,
        workspace=tmp_path,
        runtime_values={},
        run_index=0,
        model="test-model",
        transcript=Transcript(messages=[]),
    )
    adapter = ChatbotAdapter()
    fake_client = FakeClient()
    adapter._client = fake_client

    result = asyncio.run(adapter.run_phase(phase, ctx))

    assert result.completed_normally is True
    assert [call["message"] for call in fake_client.calls] == ["first user turn", "second user turn"]
    assert [call["turn"] for call in fake_client.calls] == [0, 1]
    assert all(call["project"] == tmp_path for call in fake_client.calls)
    assert [message.role for message in ctx.transcript.messages] == ["user", "assistant", "user", "assistant"]


def test_clawbench_trajectory_preserves_adapter_browser_family():
    from clawbench.schemas import ToolCall
    from clawbench.trajectory import classify_tool_call

    family, mutating = classify_tool_call(
        ToolCall(
            name="env_run",
            family="browser",
            input={"command": "node verify_form.cjs http://127.0.0.1:1234/"},
        )
    )

    assert family == "browser"
    assert mutating is False


def test_clawbench_trajectory_classifies_node_web_verifier_as_browser():
    from clawbench.schemas import ToolCall
    from clawbench.trajectory import classify_tool_call

    family, mutating = classify_tool_call(
        ToolCall(
            name="env_run",
            input={"command": "node verify_form.cjs http://127.0.0.1:1234/"},
        )
    )

    assert family == "browser"
    assert mutating is False


def test_clawbench_trajectory_preserves_locked_execute_family():
    from clawbench.schemas import ToolCall
    from clawbench.trajectory import classify_tool_call

    family, mutating = classify_tool_call(
        ToolCall(
            name="env_run_execute",
            family="execute",
            input={
                "command": "node verify_form.cjs http://127.0.0.1:1234/",
                "_preserve_family": "execute",
            },
        )
    )

    assert family == "execute"
    assert mutating is False


def test_clawbench_shell_stderr_redirection_is_not_mutation():
    from clawbench.trajectory import classify_shell_command

    family, mutating = classify_shell_command("curl -s http://127.0.0.1:1234/ 2>&1 | head -100")

    assert family in {"execute", "read"}
    assert mutating is False


def test_clawbench_capability_gating_splits_supported_and_missing_tasks():
    root = Path(__file__).resolve().parent.parent
    claw_root = root / ".benchmarks" / "clawbench_original_agent"
    from clawbench.canonical import AdapterCapability
    from clawbench.tasks import load_all_tasks
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapterConfig
    from stress_tools.run_clawbench_current_agent_scored import (
        _adapter_capabilities,
        _split_tasks_by_capability,
    )

    tasks = load_all_tasks(
        tasks_dir=claw_root / "tasks-public",
        task_ids=["t1-bugfix-discount", "t2-fs-find-that-thing"],
        prompt_variant="clear",
        pool="public_dev",
    )
    caps = _adapter_capabilities(ChatbotAdapterConfig())

    runnable, skipped = _split_tasks_by_capability(tasks, adapter_caps=caps, enabled=True)

    assert AdapterCapability.MULTI_TURN_INJECTION in caps
    assert {task.id for task in runnable} == {"t1-bugfix-discount", "t2-fs-find-that-thing"}
    assert skipped == []


def test_clawbench_capability_gating_can_be_disabled():
    root = Path(__file__).resolve().parent.parent
    claw_root = root / ".benchmarks" / "clawbench_original_agent"
    from clawbench.tasks import load_all_tasks
    from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapterConfig
    from stress_tools.run_clawbench_current_agent_scored import (
        _adapter_capabilities,
        _split_tasks_by_capability,
    )

    tasks = load_all_tasks(
        tasks_dir=claw_root / "tasks-public",
        task_ids=["t2-fs-find-that-thing"],
        prompt_variant="clear",
        pool="public_dev",
    )
    caps = _adapter_capabilities(ChatbotAdapterConfig())

    runnable, skipped = _split_tasks_by_capability(tasks, adapter_caps=caps, enabled=False)

    assert [task.id for task in runnable] == ["t2-fs-find-that-thing"]
    assert skipped == []


def test_clawbench_requested_task_ids_supports_explicit_all_public():
    from stress_tools.run_clawbench_current_agent_scored import _requested_task_ids

    assert _requested_task_ids(argparse.Namespace(task=None, all_public=False)) == ["t1-fs-quick-note"]
    assert _requested_task_ids(argparse.Namespace(task=["t2-config-loader"], all_public=False)) == ["t2-config-loader"]
    assert _requested_task_ids(argparse.Namespace(task=["t2-config-loader"], all_public=True)) is None


def test_clawbench_all_skipped_does_not_start_service(tmp_path, monkeypatch):
    import stress_tools.run_clawbench_current_agent_scored as runner

    def fail_start_service(*_args, **_kwargs):
        raise AssertionError("service should not start when all tasks are capability-skipped")

    def fake_split_tasks_by_capability(tasks, *, adapter_caps, enabled):
        return [], [
            {
                "task_id": tasks[0].id,
                "missing_capabilities": ["gateway_rpc"],
                "required_capabilities": ["execution", "files", "gateway_rpc"],
                "reason": "adapter_capability_gap",
            }
        ]

    monkeypatch.setattr(runner, "start_service", fail_start_service)
    monkeypatch.setattr(runner, "_split_tasks_by_capability", fake_split_tasks_by_capability)
    output = tmp_path / "result.json"
    args = argparse.Namespace(
        task=["t1-fs-quick-note"],
        runs=1,
        model="chatbot-current-agent",
        port=8129,
        pool="public_dev",
        prompt_variant="clear",
        health_timeout_sec=1.0,
        max_phase_seconds=1.0,
        judge_model="",
        judge_affects_score=False,
        capability_gating=True,
        start_service=True,
        output=output,
    )

    result = asyncio.run(runner._run(args))

    assert output.exists()
    assert result["task_results"] == []
    assert result["environment"]["runnable_task_ids"] == []
    assert result["skipped_tasks"][0]["task_id"] == "t1-fs-quick-note"
    assert result["skipped_tasks"][0]["missing_capabilities"] == ["gateway_rpc"]


def test_clawbench_usage_parser_excludes_helper_kind_alias(tmp_path):
    from stress_tools.run_clawbench_current_agent_scored import (
        _cache_stats_from_debug_logs,
        _usage_metadata_from_cache_stats,
    )

    debug_dir = tmp_path / "debug_logs"
    debug_dir.mkdir()
    (debug_dir / "debug.log").write_text(
        "\n".join(
            [
                "[10:00:00.000] [trace-a             ] [llm.cache_stats] P49 [main]: model=deepseek-v4-pro prompt=100 completion=10 cache_hit=80 cache_miss=20 hit_rate=80%",
                "[10:00:01.000] [trace-a.helper      ] [llm.cache_stats] P49 [helper.fix]: model=deepseek-v4-pro prompt=200 completion=20 cache_hit=150 cache_miss=50 hit_rate=75%",
                "[10:00:01.000] [trace-a.helper      ] [llm.cache_stats] P49 [helper_kind.code]: model=deepseek-v4-pro prompt=200 completion=20 cache_hit=150 cache_miss=50 hit_rate=75%",
            ]
        ),
        encoding="utf-8",
    )

    stats = _cache_stats_from_debug_logs(tmp_path)
    metadata = _usage_metadata_from_cache_stats(stats)

    assert [item.tag for item in stats] == ["main", "helper.fix"]
    assert metadata["prompt_tokens"] == 300
    assert metadata["completion_tokens"] == 30
    assert metadata["total_tokens"] == 330
    assert metadata["cache_hit_tokens"] == 230
    assert metadata["cache_miss_tokens"] == 70
    assert metadata["excluded_alias_tags"] == ["helper_kind.*"]


def test_clawbench_attach_usage_to_last_assistant_message():
    from clawbench.schemas import Transcript, TranscriptMessage
    from app.core.cache_report import CacheStats
    from stress_tools.run_clawbench_current_agent_scored import _attach_usage_to_transcript

    transcript = Transcript(
        messages=[
            TranscriptMessage(role="user", text="fix it"),
            TranscriptMessage(role="assistant", text="done"),
        ]
    )
    stats = [
        CacheStats(
            time_text="10:00:00.000",
            timestamp=36000.0,
            trace="trace-a",
            tag="main",
            model="deepseek-v4-pro",
            prompt_tokens=100,
            completion_tokens=10,
            cache_hit_tokens=80,
            cache_miss_tokens=20,
            hit_rate_percent=80,
        )
    ]

    metadata = _attach_usage_to_transcript(transcript, stats)

    usage = transcript.total_usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 10
    assert usage.cache_read_tokens == 80
    assert usage.cache_write_tokens == 0
    assert usage.total_tokens == 110
    assert metadata["source"] == "debug_logs.llm.cache_stats"
    assert metadata["cost_estimated"] is False


def test_clawbench_progress_table_uses_latest_run_and_main_prompt(tmp_path):
    from stress_tools.update_clawbench_progress_table import write_progress_table

    old_run = tmp_path / "20260609_010000"
    new_run = tmp_path / "20260609_020000"
    for run, score, prompt in ((old_run, 0.1, 1000), (new_run, 0.9, 2500)):
        (run / "debug_logs").mkdir(parents=True)
        (run / "current_agent_clawbench_scored.json").write_text(
            json.dumps(
                {
                    "task_results": [
                        {
                            "task_id": "task-a",
                            "mean_task_score": score,
                            "mean_completion_score": 1.0,
                            "mean_trajectory_score": score,
                            "mean_behavior_score": 1.0,
                            "mean_duration_ms": 1234,
                            "mean_input_tokens": 10,
                            "mean_output_tokens": 2,
                            "mean_total_tokens": 12,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (run / "debug_logs" / "debug.log").write_text(
            f"[00:00:00.000] [trace] [llm.cache_stats] P49 [main]: model=m prompt={prompt} completion=1 cache_hit=1 cache_miss=1 hit_rate=50%\n"
            "[00:00:00.000] [trace] [llm.cache_stats] P49 [helper.x]: model=m prompt=9999 completion=1 cache_hit=1 cache_miss=1 hit_rate=50%\n",
            encoding="utf-8",
        )

    out_md = tmp_path / "progress.md"
    out_json = tmp_path / "progress.json"
    rows = write_progress_table(runs_root=tmp_path, out_md=out_md, out_json=out_json)

    assert len(rows) == 1
    assert rows[0].run_id == "20260609_020000"
    assert rows[0].score == 0.9
    assert rows[0].max_main_prompt_tokens == 2500
    assert "task-a" in out_md.read_text(encoding="utf-8")
