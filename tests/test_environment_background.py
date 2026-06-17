import asyncio
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))


async def _cleanup_background_tasks():
    from app.llm.tools.environment_background import reset_background_tasks_for_tests
    from app.api.interrupts import clear_interrupt_messages

    clear_interrupt_messages()
    await reset_background_tasks_for_tests()


def _env(project: Path):
    from app.core.runtime_mode import EnvironmentContext

    return EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_bg",
        group_id="123456",
        user_id="42",
        project_key="p",
    )


def test_env_background_tool_is_environment_only():
    from app.llm.tools import registry
    from app.core.runtime_mode import runtime_context

    chat_names = [tool["function"]["name"] for tool in registry.tools_for_runtime_mode("chat")]
    assert "env_background" not in chat_names

    project = Path.cwd()
    with runtime_context("environment", _env(project)):
        env_names = [tool["function"]["name"] for tool in registry.tools_for_current_runtime()]
    assert "env_background" in env_names


def test_env_background_is_environment_code_helper_only():
    from app.core.runtime_mode import runtime_context
    from app.llm.tools.delegate import _HELPER_TOOLS, _filter_tools_for_kind

    chat_code = {tool["function"]["name"] for tool in _filter_tools_for_kind("code", _HELPER_TOOLS)}
    assert "env_background" not in chat_code

    project = Path.cwd()
    with runtime_context("environment", _env(project)):
        env_code = {tool["function"]["name"] for tool in _filter_tools_for_kind("code", _HELPER_TOOLS)}
        env_edit = {tool["function"]["name"] for tool in _filter_tools_for_kind("edit", _HELPER_TOOLS)}

    assert "env_background" in env_code
    assert "env_background" not in env_edit


@pytest.mark.asyncio
async def test_env_background_start_writes_result_files(tmp_path):
    from app.core.runtime_mode import runtime_context
    from app.llm.tools.environment import handle_environment_tool

    await _cleanup_background_tasks()
    try:
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        project.mkdir()
        workspace.mkdir()

        with runtime_context("environment", _env(project)):
            started = json.loads(await handle_environment_tool(
                "env_background",
                str(workspace),
                {
                    "action": "start",
                    "task_id": "quick",
                    "python_code": "print('BG_OK')",
                    "notify_on_finish": True,
                    "reminder_text": "quick check",
                    "wake_interval_sec": 600,
                },
            ))

        assert started["ok"] is True
        assert started["started"] is True
        assert "stdout" not in started
        assert Path(started["result_path"]).is_absolute()
        assert started["result_abs_path"] == started["result_path"]
        assert started["result_rel_path"] == ".temp/_env_background/quick/result.json"

        result_file = Path(started["result_path"])
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline and not result_file.exists():
            await asyncio.sleep(0.05)

        assert result_file.exists()
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert result["ok"] is True
        assert Path(started["stdout_path"]).read_text(encoding="utf-8").strip() == "BG_OK"
    finally:
        await _cleanup_background_tasks()


@pytest.mark.asyncio
async def test_env_background_finish_interrupts_active_task(monkeypatch, tmp_path):
    from app.api.interrupts import pop_interrupt_payloads
    from app.core.locks import get_group_guard
    from app.core.runtime_mode import runtime_context
    from app.llm.tools.environment import handle_environment_tool

    await _cleanup_background_tasks()
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    env = _env(project)
    guard = get_group_guard()
    await guard.acquire(env.archive_id, env.group_id, env.user_id, "active-trace")

    try:
        with runtime_context("environment", env):
            started = json.loads(await handle_environment_tool(
                "env_background",
                str(workspace),
                {
                    "action": "start",
                    "task_id": "reminder",
                    "python_code": "print('done')",
                    "notify_on_finish": True,
                    "reminder_text": "xx任务结束",
                },
            ))
        assert started["ok"] is True
        deadline = asyncio.get_running_loop().time() + 10
        queued = []
        while asyncio.get_running_loop().time() < deadline:
            queued = pop_interrupt_payloads(env.archive_id, env.group_id, env.user_id)
            if queued:
                break
            await asyncio.sleep(0.05)
        assert queued
        assert queued[0]["message"].startswith("[CQ:at,qq=42]")
        assert "xx任务结束" in queued[0]["message"]
        assert queued[0]["kind"] == "background"
        assert queued[0]["source"] == "env_background_finished"
        assert queued[0]["meta"]["task_id"] == "reminder"
        assert Path(queued[0]["meta"]["result_path"]).is_absolute()
        assert queued[0]["meta"]["result_abs_path"] == queued[0]["meta"]["result_path"]
        assert queued[0]["meta"]["result_rel_path"] == ".temp/_env_background/reminder/result.json"
        assert guard.get_abort_event(env.archive_id, env.group_id, env.user_id).is_set()
    finally:
        await guard.release(env.archive_id, env.group_id, env.user_id, "active-trace")
        await _cleanup_background_tasks()


@pytest.mark.asyncio
async def test_env_background_periodic_wake_queues_check(monkeypatch, tmp_path):
    from app.api.interrupts import pop_interrupt_payloads
    from app.core.locks import get_group_guard
    from app.core.runtime_mode import runtime_context
    from app.llm.tools.environment import handle_environment_tool

    await _cleanup_background_tasks()
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    env = _env(project)
    guard = get_group_guard()
    await guard.acquire(env.archive_id, env.group_id, env.user_id, "active-trace")

    try:
        with runtime_context("environment", env):
            started = json.loads(await handle_environment_tool(
                "env_background",
                str(workspace),
                {
                    "action": "start",
                    "task_id": "slow",
                    "python_code": "import time\nprint('start')\ntime.sleep(1.2)\nprint('end')",
                    "notify_on_finish": False,
                    "wake_interval_sec": 1,
                },
            ))
        assert started["ok"] is True
        deadline = asyncio.get_running_loop().time() + 5
        queued = []
        while asyncio.get_running_loop().time() < deadline:
            queued = pop_interrupt_payloads(env.archive_id, env.group_id, env.user_id)
            if queued:
                break
            await asyncio.sleep(0.05)
        assert queued
        assert "后台任务仍在运行" in queued[0]["message"]
        assert queued[0]["kind"] == "background"
        assert queued[0]["source"] == "env_background_check"
        assert queued[0]["meta"]["reminder_kind"] == "check"
    finally:
        await guard.release(env.archive_id, env.group_id, env.user_id, "active-trace")
        await _cleanup_background_tasks()


@pytest.mark.asyncio
async def test_env_background_translates_sleep_command_on_windows_shell(tmp_path):
    from app.core.runtime_mode import runtime_context
    from app.llm.tools.environment import handle_environment_tool

    await _cleanup_background_tasks()
    try:
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        project.mkdir()
        workspace.mkdir()

        with runtime_context("environment", _env(project)):
            started = json.loads(await handle_environment_tool(
                "env_background",
                str(workspace),
                {
                    "action": "start",
                    "task_id": "sleep_cmd",
                    "command": "sleep 0.2 && python -c \"print('BG_SLEEP_OK')\"",
                    "notify_on_finish": False,
                },
            ))

        assert started["ok"] is True
        assert 'powershell -NoProfile -Command "Start-Sleep' in started["command"]
        result_file = Path(started["result_path"])
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline and not result_file.exists():
            await asyncio.sleep(0.05)

        assert result_file.exists()
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert result["ok"] is True
        assert Path(started["stdout_path"]).read_text(encoding="utf-8").strip() == "BG_SLEEP_OK"
    finally:
        await _cleanup_background_tasks()


@pytest.mark.asyncio
async def test_env_background_snapshot_keeps_only_running_tasks(monkeypatch, tmp_path):
    from app.core.environment_monitor import monitor

    async def fake_list_running_background_tasks():
        return [
            {
                "task_id": "running-task",
                "status": "running",
                "cwd": "work",
                "result_path": "_env_background/running-task/result.json",
                "status_path": "_env_background/running-task/status.json",
            },
            {
                "task_id": "done-task",
                "status": "done",
                "cwd": "work",
                "result_path": "_env_background/done-task/result.json",
                "status_path": "_env_background/done-task/status.json",
            },
        ]

    monkeypatch.setattr("app.llm.tools.environment_background.list_running_background_tasks", fake_list_running_background_tasks)
    snapshot = await monitor.snapshot()
    assert snapshot["active_background_task_count"] == 1
    assert snapshot["active_background_tasks"][0]["task_id"] == "running-task"
