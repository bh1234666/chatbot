"""
M5 测试：
1. 真实 asyncio 测试 GroupGuard 的并发互斥行为
2. 真实 debug 模块开关、contextvar trace_id 传播
3. 结构检查 chat.py 与 orchestrator 接入正确
"""
import asyncio
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def check(label, cond, detail=""):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}{(' :: ' + detail) if not cond and detail else ''}")
    assert cond, label


# ── 1. GroupGuard 真实并发测试 ──────────────────────────────
def test_group_guard():
    # 屏蔽 pydantic_settings 依赖：直接 mock 一个最小 settings
    # 实际上 locks.py 不依赖 settings，可以直接 import
    # 但 import locks 会通过 app/__init__.py，可能拉 settings
    # 这里直接读源码 exec 单文件
    import asyncio as aio
    src = (ROOT / "app/core/locks.py").read_text(encoding="utf-8")
    ns = {"asyncio": aio, "__name__": "locks_test"}
    exec(src, ns)
    GroupGuard = ns["GroupGuard"]
    GroupBusyError = ns["GroupBusyError"]

    async def scenario():
        g = GroupGuard()

        # 1. 不同 group 互不影响
        await g.acquire("a1", "g1", "u1", "t1")
        await g.acquire("a1", "g2", "u2", "t2")
        check("different groups don't conflict", True)

        # 2. 同 (archive, group, user) 第二次 acquire 抛异常（不阻塞）
        thrown = False
        try:
            await g.acquire("a1", "g1", "u1", "t-other")
        except GroupBusyError as e:
            thrown = True
            check("busy error has holder_trace",
                  e.holder_trace == "t1")
            check("busy error has archive_id", e.archive_id == "a1")
            check("busy error has group_id", e.group_id == "g1")
        check("second acquire on same group raises", thrown)

        # 3. 不同 archive 同 group 不冲突
        await g.acquire("a2", "g1", "u3", "t3")
        check("different archive same group doesn't conflict", True)

        # 4. release 后可重新 acquire
        await g.release("a1", "g1", "u1", "t1")
        await g.acquire("a1", "g1", "u1", "t-new")
        check("release allows reacquire", True)

        # 5. 错误的 trace_id 不能 release 别人的锁
        ok = await g.release("a1", "g1", "u1", "wrong-trace")
        check("release with wrong trace returns False", ok is False)
        # 锁仍然属于 t-new
        thrown2 = False
        try:
            await g.acquire("a1", "g1", "u1", "another")
        except GroupBusyError:
            thrown2 = True
        check("lock still held after wrong-trace release", thrown2)

        # 6. is_busy
        check("is_busy true", await g.is_busy("a1", "g1", "u1"))
        check("is_busy false", not await g.is_busy("a1", "g1", "non-existing-user"))

        # 7. signal_abort should create the abort channel if the active task has not
        # reached orchestrator setup yet.
        g_abort = GroupGuard()
        await g_abort.acquire("a", "g", "u", "t")
        ok_abort = await g_abort.signal_abort("a", "g", "u")
        check("signal_abort creates lazy abort channel for active task", ok_abort is True)
        ch = g_abort.get_abort_channel("a", "g", "u")
        check("lazy abort channel is signalled", ch.gen == 1 and ch.is_set())

        # 8. 真实并发测试：100 个协程同时 acquire 同一 (a,g,u)
        g2 = GroupGuard()
        successes = 0
        failures = 0

        async def worker(i):
            nonlocal successes, failures
            try:
                await g2.acquire("a", "g", "u", f"t{i}")
                successes += 1
                # 持锁一小段时间
                await asyncio.sleep(0.01)
                await g2.release("a", "g", "u", f"t{i}")
            except GroupBusyError:
                failures += 1

        await aio.gather(*[worker(i) for i in range(100)])
        # 100 个里只有 1 个成功（其他全部 BusyError）
        check(f"under contention: {successes} succeeded, {failures} failed",
              successes == 1 and failures == 99)

    asyncio.run(scenario())


# ── 2. Debug 模块行为 ───────────────────────────────────────
def test_debug_module():
    """
    不能直接 import app.core.debug（会拉 settings/pydantic_settings）。
    用 monkey-patching：先 stub settings，再 exec 源码。
    """
    import asyncio as aio

    class _S:
        debug_mode = False
        debug_payload_max_chars = 8000
        debug_color = False
        debug_verbose = False
        debug_console = True
        debug_prompt_cache_full_shape = True
        debug_log_dir = None

    fake_settings_mod = type(sys)("app.config")
    fake_settings_mod.settings = _S()
    sys.modules["app.config"] = fake_settings_mod

    # 重新 import debug 模块
    src = (ROOT / "app/core/debug.py").read_text(encoding="utf-8")
    # 让 import app.config 在 exec 内能拿到我们的 stub
    debug_ns: dict = {"__name__": "debug_test"}
    exec(src, debug_ns)

    set_trace_id = debug_ns["set_trace_id"]
    log_fn = debug_ns["log"]
    section_fn = debug_ns["section"]
    is_enabled = debug_ns["is_enabled"]
    current_trace_id = debug_ns["current_trace_id"]

    # 初始：debug_mode=False，应零输出
    captured = io.StringIO()
    old = sys.stderr
    sys.stderr = captured
    try:
        log_fn("test.cat", "should not appear", {"a": 1})
        section_fn("should not appear")
    finally:
        sys.stderr = old
    check("debug off produces no output", captured.getvalue() == "")
    check("is_enabled() False when off", is_enabled() is False)

    # 开启 debug
    _S.debug_mode = True
    _S.debug_verbose = True
    captured = io.StringIO()
    sys.stderr = captured
    try:
        set_trace_id("abc12345xyz")
        check("current_trace_id reads back", current_trace_id() == "abc12345xyz")
        log_fn("test.cat", "msg here", {"a": [1, 2], "b": "中文"})
    finally:
        sys.stderr = old
    out = captured.getvalue()
    check("debug on produces output", len(out) > 0)
    check("output contains category", "[test.cat]" in out)
    check("output contains trace_id", "abc12345xyz" in out)
    check("output contains msg", "msg here" in out)
    check("output contains payload", '"中文"' in out)
    _S.debug_verbose = False

    _S.debug_console = False
    _S.debug_verbose = True
    captured = io.StringIO()
    sys.stderr = captured
    try:
        log_fn("test.cat", "console disabled", {"a": "still file-log eligible"})
    finally:
        sys.stderr = old
    check("debug console disabled suppresses stderr", captured.getvalue() == "")
    _S.debug_console = True
    _S.debug_verbose = False

    # contextvar 跨 task 传递
    async def child_task():
        return current_trace_id()

    async def parent():
        set_trace_id("parent-tid")
        return await child_task()

    tid = asyncio.run(parent())
    check("contextvar propagates to child task", tid == "parent-tid")

    # payload 截断
    _S.debug_verbose = True
    _S.debug_payload_max_chars = 100
    captured = io.StringIO()
    sys.stderr = captured
    try:
        log_fn("trunc", "long payload", "x" * 5000)
    finally:
        sys.stderr = old
    out = captured.getvalue()
    check("payload truncated", "(truncated" in out or "truncated" in out.lower())
    check("payload length capped", len(out) < 5000)

    # 还原 settings 给后续测试用
    _S.debug_mode = False
    _S.debug_payload_max_chars = 8000

    # 清理 sys.modules
    del sys.modules["app.config"]


# ── 3. 结构检查 ──────────────────────────────────────────────
import ast


def parse(rel):
    return ast.parse((ROOT / "app" / rel).read_text(encoding="utf-8"))


def get_funcs(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            kwonly = [a.arg for a in node.args.kwonlyargs]
            out[node.name] = args + kwonly
    return out


def test_structure():
    # locks 模块
    locks = get_funcs(parse("core/locks.py"))
    for name in ["GroupGuard", "GroupBusyError", "get_group_guard"]:
        # 类也是 FunctionDef? 不，类不是；用 source 检查
        pass
    locks_src = (ROOT / "app/core/locks.py").read_text(encoding="utf-8")
    for name in ["class GroupBusyError", "class GroupGuard",
                 "def get_group_guard", "_guard = GroupGuard()"]:
        check(f"locks has {name!r}", name in locks_src)

    # debug 模块
    debug_src = (ROOT / "app/core/debug.py").read_text(encoding="utf-8")
    for name in [
        "ContextVar", "_trace_id_var",
        "def set_trace_id", "def current_trace_id",
        "def is_enabled", "def log", "def section",
        "settings.debug_mode", "settings.debug_payload_max_chars",
    ]:
        check(f"debug has {name!r}", name in debug_src)

    # config 加了 debug 项
    cfg_src = (ROOT / "app/config.py").read_text(encoding="utf-8")
    for name in ["debug_mode", "debug_payload_max_chars", "debug_color",
                 "debug_console", 'validation_alias="DEBUG_MODE"']:
        check(f"config has {name!r}", name in cfg_src)

    # chat.py 接入锁
    chat_src = (ROOT / "app/api/chat.py").read_text(encoding="utf-8")
    for kw in ["get_group_guard", "GroupBusyError",
               "guard.acquire", "guard.release",
               "HTTP_409_CONFLICT", '"code": "group_busy"',
               "EventSourceResponse"]:
        check(f"chat.py has {kw!r}", kw in chat_src)
    check("chat.py has /active endpoint", '"/active"' in chat_src)

    # orchestrator 接入 trace_id 参数和 complete 事件
    orch_src = "\n".join(
        (ROOT / rel).read_text(encoding="utf-8")
        for rel in ("app/core/orchestrator.py", "app/core/orchestrator_entry.py")
    )
    for kw in [
        "trace_id: Optional[str] = None",
        "debug.set_trace_id(trace_id)",
        '"complete"',
        "await _post_response_maintenance(",  # 不是 create_task
        "from app.core import debug",
    ]:
        check(f"orchestrator has {kw!r}", kw in orch_src)
    check("orchestrator no longer create_task for maintenance",
          "asyncio.create_task(\n        _post_response_maintenance" not in orch_src
          and "create_task(\n            _post_response_maintenance" not in orch_src)

    # llm/client.py 加了 debug
    cli_src = "\n".join(
        (ROOT / rel).read_text(encoding="utf-8")
        for rel in ("app/llm/client.py", "app/llm/client_tools_loop.py")
    )
    for kw in [
        "from app.core import debug",
        "debug.log(",
        '"llm.json.input"', '"llm.json.parsed"',
        '"llm.stream.input"', '"llm.stream.output"',
        '"llm.tools.start"', '"llm.tools.iter"', '"llm.tools.calls"',
    ]:
        check(f"llm.client has {kw!r}", kw in cli_src)

    # tools/registry.py 加了 debug
    reg_src = (ROOT / "app/llm/tools/registry.py").read_text(encoding="utf-8")
    for kw in ["from app.core import debug", "debug.log(", ".input", ".output", ".error"]:
        check(f"tools.registry has {kw!r}", kw in reg_src)


# ── 主入口 ──
if __name__ == "__main__":
    test_group_guard()
    test_debug_module()
    test_structure()
    print("\n=== M5 (debug + group lock) tests passed ===")
