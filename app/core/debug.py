"""
Debug 控制台 + 文件日志。

模式:
- 关闭(debug_mode=False):零成本(提前 return)。
- 弱 debug(debug_mode=True):控制台仅输出 section / LLM 状态报告 / error / warn。
  完整细节(含 payload)写入日志文件(若配置了 debug_log_dir)。
- 详细 debug(debug_mode=True + debug_verbose=True):控制台也输出完整 payload。

调用规约:
  debug.set_trace_id("xxx")        # 在 orchestrator 入口设置一次
  debug.log("category", "msg")     # 记录事件
  debug.log("category", "msg", payload_dict_or_str)
  debug.section("title")           # 视觉分隔
  await debug.report()             # 发送缓冲事件给 debug 模型,输出一句状态
  debug.error("msg")               # 立即输出
  debug.warn("msg")                # 立即输出

类别命名约定:
  orchestrate.{start,done,skip,error}
  round{1,2,3}.{input,done,skip,error}
  llm.{json,stream,tools}.{input,raw,calls,result,error}
  tool.<name>.{input,output,error}
  memory.{hot,warm,cold,kb}.{read,write,compress,...}
  maintenance.{start,task,done,error}
"""
from __future__ import annotations

import sys
import json
import os
from datetime import datetime
from contextvars import ContextVar
from typing import Any, TextIO

from app.config import settings


_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="--------")
_USE_COLOR = settings.debug_color and sys.stderr.isatty()

# 这些类别在文件日志中也跳过 payload(太冗长,如 llm.json.input 含完整文件内容)
# ── 2026-05-04 Bug #7 修复 ──
# 实测 3.16 MB log 里至少 1 MB 是重复的 helper system prompt(15KB × 6 helper × 多次启动)。
# 大 payload 类别(grep 验证过实际存在,不再猜测):
#   llm.tools.start   ← 含 helper / 主线程的完整 system prompt + messages 数组(15KB+)
#   llm.stream.input  ← 流式 LLM 调用入参,同上
#   llm.stream.output ← 累积完整 streamed 输出
#   llm.json.input    ← Round 1 / 维护任务 JSON 调用入参
# 加入跳过列表后,文件日志只记 message 不记完整 payload。日志规模可降 30-50%。
# 控制台输出不受影响(_should_show_console 走另一条路径)。
_NO_PAYLOAD_FILE = {
    "llm.json.input",
    "llm.tools.start",
    "llm.stream.input",
    "llm.stream.output",
}

# 事件缓冲(非 verbose 模式用于 LLM 总结)
# Patch 09 (2026-05-02): 改为 ContextVar,per-user 并发下每个 trace 独立 buffer,
# 避免跨 trace 事件混入(旧版模块级 list 在并发场景下被多个 trace 共享,
# 真实 trace 956f7fa8 的 [状态] 行混入了别的 trace 事件)。
_MAX_BUFFER = 60
_buffer_var: ContextVar[list[str] | None] = ContextVar("debug_buffer", default=None)

DEBUG_REPORT_SYSTEM = (
    "You summarize internal workflow events into one short user-visible Chinese status phrase.\n"
    "\n"
    "## Requirements\n"
    "- Return one short Chinese phrase, no more than 30 Chinese characters.\n"
    "- State only observable progress from the provided events.\n"
    "- Prioritize ERROR or WARN only when present.\n"
    "- Use file-generation wording only when the events show workspace/file write, delivery, office, image, audio, or artifact activity.\n"
    "- For read-only analysis, use process wording such as 正在阅读工程, 正在核对内容, 正在分析结构, or 正在整理结果.\n"
    "- Use user-friendly process wording rather than internal event names.\n"
    "- If a clean user-facing phrase is not possible, return an empty string.\n"
    "- Output only the status phrase, with no extra text.\n"
    "\n"
    "把内部事件压缩成一句用户可见的中文状态；只在确有文件或产物动作时说生成文件。"
)


def _debug_report_user_payload(events: list[str]) -> str:
    """Return deterministic dynamic facts for the debug status reporter."""
    payload = {
        "events": [str(event) for event in events],
    }
    return (
        "## Runtime Facts\n"
        + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\n输出一句用户可见的当前状态。"
    )


def _get_buffer() -> list[str]:
    """懒初始化当前 trace 的 buffer(每个 asyncio task 独立)。"""
    buf = _buffer_var.get()
    if buf is None:
        buf = []
        _buffer_var.set(buf)
    return buf

# 文件日志
_log_file: TextIO | None = None
_log_file_path: str = ""

# 这些类别的事件在非 verbose 模式下也会在控制台显示一行摘要
# 支持精确匹配和前缀匹配（以 * 结尾）
_CONSOLE_CATEGORIES = {
    "llm.tools.iter",
    "llm.tools.calls",
    "llm.tools.result",
    "llm.tools.cap",
    "llm.tools.final",
    "llm.tools.final_forced",
    "llm.tools.abort",
    "tool.*",
    "workspace.files",
    "workspace.create",
    "workspace.missing",
    "round1.done",
    "round2.done",
    "round2.medium",
    "round2.hard",
    "round2.upgrade",
    "round2.upgrade_veryhard",
    "round3.done",
    "load.done",
    "orchestrate.complete",
}


def _should_show_console(category: str) -> bool:
    if category in _CONSOLE_CATEGORIES:
        return True
    for pat in _CONSOLE_CATEGORIES:
        if pat.endswith("*") and category.startswith(pat[:-1]):
            return True
    return False


def set_trace_id(tid: str) -> None:
    _trace_id_var.set(tid or "--------")
    # Patch 09: 新 trace 起步,清空当前 task 的 buffer,避免上一个 trace 的残留事件
    _buffer_var.set([])


def current_trace_id() -> str:
    return _trace_id_var.get()


def is_enabled() -> bool:
    return settings.debug_mode


def log(category: str, msg: str = "", payload: Any | None = None) -> None:
    """记录 debug 事件。控制台默认仅缓冲；verbose 模式立即输出。文件始终写完整内容。"""
    if not settings.debug_mode:
        return

    # 文件日志：始终写完整内容（无颜色）
    _write_file(category, msg, payload)

    # 控制台：工具类事件始终显示一行摘要；verbose 模式显示完整 payload
    if settings.debug_verbose:
        _emit_console(category, msg, payload)
    elif _should_show_console(category):
        _emit_console(category, msg, None)
    else:
        buf = _get_buffer()
        buf.append(f"[{category}] {msg}")
        if len(buf) > _MAX_BUFFER:
            buf.pop(0)


def section(title: str) -> None:
    """视觉分隔。控制台+文件都输出。"""
    if not settings.debug_mode:
        return
    # Keep stderr section markers ASCII-only. Several Windows stress runners pipe
    # stderr through non-UTF-8 consoles before it reaches UTF-8 log files; box
    # drawing characters are the first thing to turn into mojibake and make
    # behavior-level log review noisy.
    bar = "=" * 60
    head = _header("section")
    # 控制台（带颜色）
    print(_c("33", f"{head} {bar}"), file=sys.stderr, flush=True)
    print(_c("33;1", f"{head} > {title}"), file=sys.stderr, flush=True)
    print(_c("33", f"{head} {bar}"), file=sys.stderr, flush=True)
    # 文件:单行只写 title,不写横线(parser 友好)
    _write_file("section", title)


def error(msg: str) -> None:
    """立即输出错误。控制台+文件。"""
    if not settings.debug_mode:
        return
    _emit_console("ERROR", msg, None, color="31")
    _write_file("ERROR", msg)


def warn(msg: str) -> None:
    """立即输出警告。控制台+文件。"""
    if not settings.debug_mode:
        return
    _emit_console("WARN", msg, None, color="33")
    _write_file("WARN", msg)


def status(msg: str) -> None:
    """直接输出一条状态（不经过 LLM）。"""
    if not settings.debug_mode:
        return
    _emit_console("status", msg, None, color="36;1")
    _write_file("status", msg)


async def report() -> None:
    """将缓冲事件发送给轻量模型总结为一句话中文状态报告。

    注意：LLM 调用是 fire-and-forget 的——不阻塞编排器。
    事件在调用前已从 buffer 取出，后续 log() 调用不受影响。
    """
    if not settings.debug_mode:
        return
    buf = _get_buffer()
    if len(buf) < 2:
        for ev in buf:
            _emit_console("status", ev, None, color="36")
            _write_file("status", ev)
        buf.clear()
        return

    events = buf[:]
    buf.clear()

    # fire-and-forget：不等待 LLM 响应，不阻塞编排器
    from app.core.bg_tasks import schedule
    schedule(_do_report(events), name="debug.report")


async def _do_report(events: list[str]) -> None:
    """后台执行 debug 报告，失败静默。"""
    try:
        from app.llm.client import _client_for_spec, _retry
        from app.llm.model_pool import resolve_task

        spec = resolve_task("progress_message")
        llm_client = _client_for_spec(spec)

        async def _call():
            messages = [
                {"role": "system", "content": DEBUG_REPORT_SYSTEM},
                {"role": "user", "content": _debug_report_user_payload(events)},
            ]
            try:
                from app.llm.client import _log_prompt_cache_shape
                _log_prompt_cache_shape(
                    label="debug.report",
                    model=spec.model,
                    messages=messages,
                )
            except Exception:
                pass
            resp = await llm_client.chat.completions.create(
                model=spec.model,
                messages=messages,
                stream=False,
                extra_body={"thinking": {"type": "disabled"}}
                if spec.provider.name == "deepseek" else None,
            )
            try:
                from app.llm.client import _record_response_usage
                _record_response_usage(resp, model=spec.model, tag="debug.report")
            except Exception:
                pass
            return resp.choices[0].message.content or ""

        text = await _retry(_call, label="debug.report", provider=spec.provider)
        text = text.strip()
        if text:
            _emit_console("状态", text, None, color="36;1")
            _write_file("状态", text)
    except Exception:
        _emit_console("status", f"调试报告失败，原始事件数: {len(events)}", None)
        _write_file("status", f"调试报告失败，原始事件数: {len(events)}")


# ── 文件日志 ─────────────────────────────────────────────────
def init_log() -> None:
    """在服务启动时调用，立即创建日志文件。"""
    if not settings.debug_mode:
        return
    _get_log_file()


def _get_log_file() -> TextIO | None:
    """初始化日志文件（幂等）。"""
    global _log_file, _log_file_path
    if _log_file is not None:
        return _log_file
    if not settings.debug_log_dir:
        return None
    os.makedirs(settings.debug_log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file_path = os.path.join(settings.debug_log_dir, f"debug_{ts}_{os.getpid()}.log")
    # buffering=1 = 行缓冲，每次换行即写入磁盘，配合 flush() 实现实时写入
    _log_file = open(_log_file_path, "w", encoding="utf-8", buffering=1)
    _log_file.write(f"# Debug log started at {datetime.now().isoformat()}\n")
    try:
        from app.llm.model_pool import resolve_task
        _main = resolve_task("round3_normal")
        _lite = resolve_task("helper_lite")
        _model_line = (
            f"# model_pool main={_main.provider.name}:{_main.model} "
            f"lite={_lite.provider.name}:{_lite.model}"
        )
    except Exception:
        _model_line = f"# model={settings.model_name} lite={settings.lite_model_name}"
    _log_file.write(f"{_model_line}\n\n")
    _log_file.flush()
    # Bug O 修(2026-05-02 review):注册 atexit 保证进程退出时 flush + close。
    # buffering=1 行缓冲在异常退出/SIGTERM 时不能保证 flush 已落盘,
    # atexit 在大多数干净退出路径会被调用,可救回最后几条 log。
    import atexit as _atexit
    _atexit.register(_close_log_file_on_exit)
    return _log_file


def _close_log_file_on_exit() -> None:
    """atexit 钩子:进程退出时确保 _log_file 关闭,避免 buffer 内容丢失。"""
    global _log_file
    f = _log_file
    if f is None:
        return
    try:
        f.write(f"# Debug log closed at {datetime.now().isoformat()}\n")
        f.flush()
        f.close()
    except Exception:
        pass  # atexit 钩子不能抛异常
    _log_file = None


def _write_file(category: str, msg: str, payload: Any | None = None) -> None:
    """写完整事件到日志文件（无颜色、始终包含 payload）。"""
    f = _get_log_file()
    if f is None:
        return
    head = _header(category)
    f.write(f"{head} {msg}\n")
    if category in _NO_PAYLOAD_FILE:
        pass
    elif payload is not None:
        try:
            if isinstance(payload, str):
                body = payload
            else:
                body = json.dumps(payload, ensure_ascii=False, indent=2, default=_safe_default)
        except Exception as e:
            body = f"<unserializable: {type(payload).__name__}: {e}>"
        if len(body) > settings.debug_payload_max_chars:
            body = (
                body[: settings.debug_payload_max_chars]
                + f"\n...(truncated, total {len(body)} chars)"
            )
        for line in body.split("\n"):
            f.write(f"{head}   {line}\n")
    f.flush()


def log_file_path() -> str:
    """返回当前日志文件路径（空字符串表示未启用）。"""
    _get_log_file()  # 触发懒初始化
    return _log_file_path


# ── 控制台输出 ───────────────────────────────────────────────
# trace_id 宽度(2026-05-04 Bug #8 part2 改:16→20):
#   主线程 trace = 16 字符随机 hex (uuid4().hex[:16],如 "e4eeb133a7c8...")
#   helper sub-trace = parent[:6] + "." + task_id[:12] = 19 字符 (如 "e4eeb1.huffman_v2  ")
# 旧版 task_id[:6] 把 "huffman"/"huffman_v2"/"bwt_final"/"bwt_v2" 截到 6 字符,
# grep 不友好且歧义。task_id[:12] 让常见名字完整保留。显示宽度同步从 16 升到 20
# 以容纳 19 字符 sub-trace 不被截尾。短于 20 的 trace 会被左对齐 padding。
# 旧 14 字符宽度对 16 字符 trace_id 会截掉末 2 位,导致 a8e4eeb133a7c801 与 a8e4eeb133a7c8ff
# 等罕见前缀重合 trace 在控制台显示成同一行,grep / eyeball 失误。
_TRACE_DISPLAY_MAX = 20

def _header(category: str) -> str:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    tid = _trace_id_var.get()[:_TRACE_DISPLAY_MAX]
    return f"[{ts}] [{tid:<{_TRACE_DISPLAY_MAX}}] [{category}]"


def _console_enabled() -> bool:
    """Return whether synchronous stderr debug output is safe to use."""
    return bool(getattr(settings, "debug_console", False))


def _emit_console(category: str, msg: str, payload: Any | None, *, color: str = "36") -> None:
    """输出到 stderr（带颜色，可选 payload）。"""
    if not _console_enabled():
        return
    head = _header(category)
    line = _c(color, head) + " " + str(msg)
    print(line, file=sys.stderr, flush=True)
    if payload is None:
        return
    try:
        if isinstance(payload, str):
            body = payload
        else:
            body = json.dumps(
                payload, ensure_ascii=False, indent=2, default=_safe_default,
            )
    except Exception as e:
        body = f"<unserializable: {type(payload).__name__}: {e}>"
    if len(body) > settings.debug_payload_max_chars:
        body = (
            body[: settings.debug_payload_max_chars]
            + f"\n...(truncated, total {len(body)} chars)"
        )
    for line in body.split("\n"):
        print(_c("90", head) + "   " + line, file=sys.stderr, flush=True)


def _safe_default(obj: Any) -> str:
    try:
        return f"<{type(obj).__name__}: {obj!r}>"
    except Exception:
        return f"<{type(obj).__name__}>"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text
