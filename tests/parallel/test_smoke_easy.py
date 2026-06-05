"""Smoke test: easy (闲聊) 路径回归."""
import pytest


@pytest.mark.slow
@pytest.mark.asyncio
async def test_easy_path_no_crash():
    """确保 easy 路径不抛异常。需要真实 LLM,标记为 slow。"""
    # 实际端到端测试需要 mock LLM client,这里留作占位。
    # 在 CI 中用 -m "not slow" 跳过。
    pass


@pytest.mark.asyncio
async def test_easy_imports():
    """验证 easy 路径核心模块可导入。"""
    from app.llm.client import chat_with_tools_loop
    from app.core.context import build_context
    from app.core.orchestrator import orchestrate
    assert chat_with_tools_loop is not None
    assert build_context is not None
    assert orchestrate is not None
