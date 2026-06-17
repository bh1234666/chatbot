import asyncio

import pytest

from app.core.locks import GroupGuard


@pytest.mark.asyncio
async def test_signal_abort_cancels_helpers_in_active_trace(monkeypatch):
    calls: list[str] = []

    class Registry:
        async def cancel_all_helpers_in_trace(self, trace_id: str) -> int:
            calls.append(trace_id)
            return 2

    from app.core import core_processes

    monkeypatch.setattr(core_processes, "registry", lambda: Registry())

    guard = GroupGuard()
    await guard.acquire("archive", "group", "user", "trace123")

    ok = await guard.signal_abort("archive", "group", "user")

    assert ok is True
    assert calls == ["trace123"]
    assert guard.get_abort_channel("archive", "group", "user").is_set()


@pytest.mark.asyncio
async def test_signal_abort_round3_does_not_cancel_helpers(monkeypatch):
    calls: list[str] = []

    class Registry:
        async def cancel_all_helpers_in_trace(self, trace_id: str) -> int:
            calls.append(trace_id)
            return 2

    from app.core import core_processes

    monkeypatch.setattr(core_processes, "registry", lambda: Registry())

    guard = GroupGuard()
    await guard.acquire("archive", "group", "user", "trace123")
    guard.set_stage("archive", "group", "user", "round3")

    ok = await guard.signal_abort("archive", "group", "user")

    assert ok is False
    assert calls == []
