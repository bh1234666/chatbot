"""项目根 conftest:让 `import app...` 可用,并为离线测试提供重依赖的桩。

工程的纯逻辑模块(如 db.sql_translate)不应被 fastapi/pydantic 等重依赖牵连。
但有些模块在 import 链上会拉到它们。这里在 sys.modules 注入最小桩,使纯逻辑
模块能在不安装全部运行时依赖的环境(如 CI 的 lint 阶段)下被测试。

注意:桩只覆盖测试实际触达的符号;需要真实行为的集成测试应在装好依赖的环境运行。
"""
import os
import sys
import types
import asyncio

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ensure_stub(name: str):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


async def _cleanup_async_resources():
    try:
        from app.core.core_processes import registry

        await registry().cancel_all_for_tests(timeout=0.2)
    except Exception:
        pass
    try:
        from app.core.bg_tasks import cancel_all

        await cancel_all(timeout=0.2)
    except Exception:
        pass


def _run_cleanup_async_resources(loop: asyncio.AbstractEventLoop | None = None):
    if loop is not None and not loop.is_closed() and not loop.is_running():
        loop.run_until_complete(_cleanup_async_resources())
        return
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        asyncio.run(_cleanup_async_resources())


@pytest.fixture(autouse=True)
def _cleanup_project_background_tasks(request):
    yield
    loop = request.node.funcargs.get("event_loop")
    _run_cleanup_async_resources(loop)


def pytest_sessionfinish(session, exitstatus):
    _run_cleanup_async_resources()
