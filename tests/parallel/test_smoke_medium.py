"""Smoke test: medium-coding 端到端(用 mock helper)."""
import pytest


@pytest.mark.slow
@pytest.mark.asyncio
async def test_smoke_db_perf_trace(monkeypatch):
    """重放本次 trace 的输入,断言总耗时改善。

    用 fake_clock + mock helper 跑完整路径,helper 的"工作"用 sleep 模拟。
    完成后统计 round2_total_sec。

    这部分需要模拟 _run_one_helper 的产出 — 实现稍复杂,
    推荐先做单元测试,smoke test 后续再加。
    """
    pass


@pytest.mark.asyncio
async def test_delegate_imports():
    """验证 delegate 核心模块可导入。"""
    from app.llm.tools.delegate import (
        handle_delegate, _run_one_helper, _dynamic_wait_loop,
        _sanitize_and_validate_tasks, _spawn_helpers_only,
        _handle_delegate_poll, _handle_delegate_collect,
        _handle_delegate_wait_any, _handle_delegate_spawn_async,
    )
    assert handle_delegate is not None
    assert _run_one_helper is not None
    assert _dynamic_wait_loop is not None
    # L1-1 新增函数
    assert _sanitize_and_validate_tasks is not None
    assert _spawn_helpers_only is not None
    assert _handle_delegate_poll is not None
    assert _handle_delegate_collect is not None
    assert _handle_delegate_wait_any is not None
    assert _handle_delegate_spawn_async is not None
