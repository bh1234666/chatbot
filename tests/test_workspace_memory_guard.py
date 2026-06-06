import pytest


@pytest.mark.asyncio
async def test_memory_coordinator_kills_one_process_at_a_time(monkeypatch):
    from app.llm.tools import memory_guard as mg

    mg._coordinator.reset_for_tests()
    kills: list[int] = []
    rss = {
        101: 9 * mg.GiB,
        202: 10 * mg.GiB,
    }
    monkeypatch.setattr(mg, "workspace_memory_limits", lambda: (16 * mg.GiB, 1 * mg.GiB))
    monkeypatch.setattr(mg, "_process_alive", lambda pid: True)
    monkeypatch.setattr(mg, "_process_tree_rss_bytes", lambda pid: rss[pid])

    g1 = mg.WorkspaceMemoryGuard(
        pid=101,
        proc_id="p1",
        command="python a.py",
        kill_tree=lambda pid: kills.append(pid),
        limit_bytes=16 * mg.GiB,
        min_available_bytes=1 * mg.GiB,
    )
    g2 = mg.WorkspaceMemoryGuard(
        pid=202,
        proc_id="p2",
        command="python b.py",
        kill_tree=lambda pid: kills.append(pid),
        limit_bytes=16 * mg.GiB,
        min_available_bytes=1 * mg.GiB,
    )
    mg._coordinator.register(g1)
    mg._coordinator.register(g2)
    try:
        facts = await mg._coordinator.request_relief(
            g1,
            reason="workspace_total_memory_limit_exceeded",
            system_snapshot={"ok": True, "available_bytes": 8 * mg.GiB},
        )
        assert facts is not None
        assert kills == [202]
        assert g2.triggered is not None
        assert g1.triggered is None

        again = await mg._coordinator.request_relief(
            g1,
            reason="workspace_total_memory_limit_exceeded",
            system_snapshot={"ok": True, "available_bytes": 8 * mg.GiB},
        )
        assert again is None
        assert kills == [202]
    finally:
        mg._coordinator.reset_for_tests()


def test_preflight_blocks_when_active_workspace_budget_is_full(monkeypatch):
    from app.llm.tools import memory_guard as mg

    monkeypatch.setattr(mg, "workspace_memory_limits", lambda: (16 * mg.GiB, 1 * mg.GiB))
    monkeypatch.setattr(
        mg,
        "active_workspace_memory_facts",
        lambda: {
            "active_process_count": 2,
            "workspace_total_rss_gib": 16.5,
            "workspace_total_limit_gib": 16.0,
            "processes": [],
        },
    )
    monkeypatch.setattr(
        mg,
        "system_memory_snapshot",
        lambda: {"ok": True, "available_bytes": 8 * mg.GiB},
    )

    result = mg.preflight_memory_check("python bench.py")

    assert result is not None
    assert result["ok"] is False
    assert result["error_kind"] == "workspace_memory_budget_busy"
    assert result["memory"]["active_process_count"] == 2


def test_memory_limit_error_exposes_facts_without_losing_partial_output():
    from app.llm.tools.memory_guard import memory_limit_error

    result = memory_limit_error(
        {"reason": "workspace_total_memory_limit_exceeded", "rss_gib": 10.0},
        stdout="partial out",
        stderr="partial err",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "memory_limit_exceeded"
    assert result["memory"]["reason"] == "workspace_total_memory_limit_exceeded"
    assert result["partial_stdout"] == "partial out"
    assert result["partial_stderr"] == "partial err"
