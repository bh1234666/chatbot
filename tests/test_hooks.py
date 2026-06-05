import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_register_unregister_and_list_hooks():
    from app.core import hooks

    hooks.clear_all_hooks()

    def cb(_event, _payload):
        return None

    hooks.register_hook(hooks.HookEvent.PRE_TOOL_USE, cb)
    hooks.register_hook("pre_tool_use", cb)

    assert hooks.list_hooks() == {"pre_tool_use": 2}
    assert hooks.list_hooks(hooks.HookEvent.PRE_TOOL_USE) == {"pre_tool_use": 2}
    assert hooks.unregister_hook("pre_tool_use", cb) is True
    assert hooks.list_hooks("pre_tool_use") == {"pre_tool_use": 1}
    assert hooks.unregister_hook("pre_tool_use", cb) is True
    assert hooks.unregister_hook("pre_tool_use", cb) is False


async def test_dispatch_hook_schedules_async_callbacks(monkeypatch):
    from app.core import hooks

    hooks.clear_all_hooks()
    scheduled = []
    called = []

    async def cb(event, payload):
        called.append((event, payload["tool"]))

    def fake_schedule(coro, *, name=None):
        scheduled.append(name)
        task = asyncio.create_task(coro)
        return task

    monkeypatch.setattr("app.core.bg_tasks.schedule", fake_schedule)
    hooks.register_hook(hooks.HookEvent.PRE_TOOL_USE, cb)

    hooks.dispatch_hook(hooks.HookEvent.PRE_TOOL_USE, {"tool": "read_file"})
    await asyncio.sleep(0)

    assert scheduled == ["hook:pre_tool_use"]
    assert called == [("pre_tool_use", "read_file")]


async def test_dispatch_hook_isolates_sync_callback_failures():
    from app.core import hooks

    hooks.clear_all_hooks()
    calls = []

    def bad(_event, _payload):
        raise RuntimeError("boom")

    def good(event, payload):
        calls.append((event, payload["x"]))

    hooks.register_hook("session_start", bad)
    hooks.register_hook("session_start", good)

    hooks.dispatch_hook("session_start", {"x": 1})

    assert calls == [("session_start", 1)]


async def test_dispatch_hook_async_awaits_coroutines_and_isolates_failures():
    from app.core import hooks

    hooks.clear_all_hooks()
    calls = []

    async def good(event, payload):
        await asyncio.sleep(0)
        calls.append((event, payload["x"]))

    async def bad(_event, _payload):
        raise RuntimeError("boom")

    hooks.register_hook("subagent_stop", good)
    hooks.register_hook("subagent_stop", bad)
    hooks.register_hook("subagent_stop", good)

    await hooks.dispatch_hook_async("subagent_stop", {"x": 2})

    assert calls == [("subagent_stop", 2), ("subagent_stop", 2)]
