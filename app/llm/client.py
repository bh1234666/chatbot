"""
DeepSeek API 异步客户端封装。

约定：
- chat_json: 结构化 JSON 输出（默认 thinking disabled）
- chat_stream: 流式输出 token，用于 Round3（thinking disabled）
- chat_with_tools_loop: 带工具调用的多轮，用于 Round2
- chat_compress: 后台压缩任务（thinking disabled）

非流式请求有单次调用超时，避免上游连接挂住时永久占用对话锁。
复杂长任务应走 streaming 工具循环，由 idle-based timeout 判断是否仍在工作。

━━━ 官方文档要点（https://api-docs.deepseek.com/guides/thinking-mode）━━━
1. 默认 thinking enabled。每次调用都需显式判断是否要 disabled。
2. reasoning_effort 实际只有三档生效：
   - "disabled"：通过 thinking.type=disabled 关闭思考
   - "high"：默认开启思考，常规强度（low/medium 会被映射成 high，**没有更轻的档**）
   - "max"：最强思考，agentic 任务（Claude Code 类）API 自动启用
3. 思考模式忽略 temperature / top_p / presence_penalty / frequency_penalty。
4. 工具调用轮次的 reasoning_content **必须**完整回传给 API，
   否则后续请求 400。即使 reasoning_content 为空字符串也要带。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator, Optional, Any
from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError

from app.config import settings
from app.core import debug
from app.core.prompt_cache_observer import describe_prompt_cache_input
from app.llm.tool_pairing import repair_tool_call_pairing as _repair_tool_call_pairing_impl
from app.llm.tools.workspace import reset_fix_hint_counts  # Bug #30: hint 重复计数

log = logging.getLogger(__name__)


META_JUDGE_SYSTEM = (
    "You are evaluating a tool-calling workflow to decide whether the current model tier is sufficient "
    "or whether escalation to a stronger tier would materially help.\n"
    "\n"
    "## Decision Criterion\n"
    "Read the timeline and ask: is this workflow making real progress toward the solution?\n"
    "\n"
    "## Upgrade Signals\n"
    "- The same error pattern, edit location, failed test, or reread loop repeats without a useful pivot.\n"
    "- The solution direction is visibly wrong and the workflow is not correcting course.\n"
    "- Tool calls return successfully but verified task outcomes remain stalled.\n"
    "\n"
    "## Stay Signals\n"
    "- Outputs, tests, evidence, or deliverables are improving across attempts.\n"
    "- Failures are changing into smaller concrete issues.\n"
    "- The task is near completion and mainly needs verification or cleanup.\n"
    "- Recent artifact facts show ready or verified deliverables, and remaining warnings are non-blocking layout or cleanup signals.\n"
    "\n"
    "## Key Distinction\n"
    "A tool return code of 0 only means the tool itself ran; it does not prove task progress. "
    "Judge semantic progress from verified outputs, test results, evidence coverage, and whether the plan is adapting.\n"
    "\n"
    "根据工具时间线判断是否真卡住；同类失败且无转向时升级，错误在收敛时保持当前档。"
)

META_JUDGE_USER_TEMPLATE = (
    "## Runtime Facts\n"
    "{runtime_facts}\n\n"
    "## Output\n"
    "Strict JSON only: {{\"should_upgrade\": true|false, \"reason\": \"brief reason\"}}\n\n"
    "输出是否升级及简短依据。"
)


def _meta_judge_user_payload(
    *,
    current_level: str,
    current_iter: int,
    next_level: str,
    timeline: str,
    artifact_facts: list[str] | None = None,
) -> str:
    """Build a deterministic dynamic payload for the meta judge."""
    facts = {
        "current_iter": int(current_iter),
        "current_tier": str(current_level),
        "next_tier": str(next_level),
        "recent_artifact_facts": list(artifact_facts or [])[:12],
        "tool_timeline": str(timeline),
    }
    return META_JUDGE_USER_TEMPLATE.format(
        runtime_facts=json.dumps(
            facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _extract_recent_artifact_facts_for_meta_judge(msgs: list[dict], *, max_items: int = 12) -> list[str]:
    """Extract compact artifact/quality facts from recent tool messages."""
    facts: list[str] = []
    seen: set[str] = set()
    artifact_ext_re = re.compile(r"[\w./\\-]+\.(?:docx|pptx|xlsx|pdf|png|jpg|jpeg|csv|json|md|txt)\b", re.IGNORECASE)
    recent = msgs[-80:] if len(msgs) > 80 else msgs
    for msg in recent:
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        try:
            data = json.loads(content)
        except Exception:
            data = None
        candidates: list[str] = []
        if isinstance(data, dict):
            if data.get("outputs_complete") is True:
                candidates.append("outputs_complete=true")
            if data.get("quality_blocked_count") == 0:
                candidates.append("quality_blocked_count=0")
            if data.get("path"):
                candidates.append(f"path={data.get('path')}")
            artifact = data.get("artifact")
            if isinstance(artifact, dict) and artifact.get("path"):
                status = artifact.get("status") or "unknown"
                candidates.append(f"artifact {artifact.get('path')} status={status}")
            for key in ("artifacts_ready", "main_available_files", "internal_evidence_files"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    candidates.append(f"{key}={len(val)}")
            warnings = data.get("quality_warnings")
            if isinstance(warnings, list) and warnings:
                issues = []
                for item in warnings[:4]:
                    if isinstance(item, dict):
                        issue = item.get("issue")
                        severity = item.get("severity") or "warning"
                        if issue:
                            issues.append(f"{issue}:{severity}")
                if issues:
                    candidates.append("quality_warnings=" + ",".join(issues))
        else:
            for path in artifact_ext_re.findall(content):
                candidates.append(f"artifact_path_mentioned={path}")
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                facts.append(candidate[:180])
                if len(facts) >= max_items:
                    return facts
    return facts


def _log_prompt_cache_shape(
    *,
    label: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> None:
    """Log deterministic prompt/cache structure without logging full content."""
    try:
        shape = describe_prompt_cache_input(messages=messages, tools=tools)
        try:
            from app.core import metrics as _metrics
            _metrics.record_prompt_shape(
                label=label,
                model=model,
                prompt_static_bytes=shape["prompt_static_bytes"],
                prompt_dynamic_bytes=shape["prompt_dynamic_bytes"],
                cacheable_prefix_bytes=shape["cacheable_prefix_bytes"],
            )
        except Exception:
            pass
        debug.log(
            "llm.prompt_cache_shape",
            (
                f"{label}: model={model} msgs={shape['message_count']} tools={shape['tool_count']} "
                f"static={shape['prompt_static_bytes']} dynamic={shape['prompt_dynamic_bytes']} "
                f"cacheable_prefix={shape['cacheable_prefix_bytes']} "
                f"system={shape['system_prompt_hash']} tools_hash={shape['tool_schema_hash']}"
            ),
            {
                "system_prompt_hash": shape["system_prompt_hash"],
                "system_static_hash": shape["system_static_hash"],
                "system_dynamic_hash": shape["system_dynamic_hash"],
                "tool_schema_hash": shape["tool_schema_hash"],
                "messages_hash": shape["messages_hash"],
                "prompt_static_bytes": shape["prompt_static_bytes"],
                "prompt_dynamic_bytes": shape["prompt_dynamic_bytes"],
                "cacheable_prefix_bytes": shape["cacheable_prefix_bytes"],
                "message_count": shape["message_count"],
                "tool_count": shape["tool_count"],
                "hash_chain": shape["hash_chain"],
                "system_sections": shape.get("system_sections", []),
                "message_sections": shape.get("message_sections", []),
                "messages": shape["messages"],
            },
        )
    except Exception:
        pass


def _thinking_mode_rejects_tool_choice_error(exc: BaseException | str | None) -> bool:
    """Return True for provider errors where thinking mode rejects tool_choice."""
    text = str(exc or "").lower()
    return "thinking mode" in text and "tool_choice" in text and "does not support" in text


def _omit_tool_choice_for_thinking(extra_body: dict[str, Any] | None, provider: Any | None) -> bool:
    """Some OpenAI-compatible thinking APIs reject explicit tool_choice.

    The model can still call tools naturally when the field is omitted, so this
    keeps task behavior intact while avoiding provider-level 400 failures.

    部分思考模式接口不接受显式 tool_choice；省略该字段仍允许模型自然调用工具。
    """
    if not _is_thinking_enabled(extra_body):
        return False
    return provider is None or getattr(provider, "name", None) == "deepseek"


def _sanitize_tool_choice_for_thinking(
    kwargs: dict[str, Any],
    *,
    provider: Any | None,
    label: str,
) -> dict[str, Any]:
    """Return request kwargs with incompatible thinking/tool_choice removed."""
    if kwargs.get("tool_choice") is None:
        return kwargs
    if not _omit_tool_choice_for_thinking(kwargs.get("extra_body"), provider):
        return kwargs
    cleaned = dict(kwargs)
    cleaned.pop("tool_choice", None)
    debug.warn(f"{label}: omitted tool_choice because provider thinking mode rejects it")
    return cleaned


class _IdleDetector:
    _IDLE_TOOLS = {"processes"}
    _WARN_AT = 3
    _COOLDOWN_ITERS = 2

    def __init__(self):
        self.consecutive_idle_iters = 0
        self.last_warned_at_iter = -999

    def record_iter(self, it: int, tool_results: list[tuple]) -> None:
        if not tool_results:
            self.consecutive_idle_iters += 1
            return
        idle_only = True
        for _, name, _, args in tool_results:
            action = args.get("action") if isinstance(args, dict) else None
            command = (args.get("command") or "") if isinstance(args, dict) else ""
            if name == "processes":
                continue
            if name == "workspace" and action == "run" and command.strip().lower() in {"dir /b", "ls", "pwd"}:
                continue
            idle_only = False
            break
        if idle_only:
            self.consecutive_idle_iters += 1
        else:
            self.consecutive_idle_iters = 0

    def should_inject_warning(self, it: int) -> str | None:
        if self.consecutive_idle_iters < self._WARN_AT:
            return None
        if it - self.last_warned_at_iter < self._COOLDOWN_ITERS:
            return None
        self.last_warned_at_iter = it
        return (
            f"Idle workflow warning: the last {self.consecutive_idle_iters} iterations only called processes.list.\n"
            "Choose a productive next step: call delegate(action='poll/collect/wait_any') to collect helper results, "
            "spawn another useful helper, or finish with the required JSON when the work is complete.\n"
            "Repeated idle listing is recorded as inefficient tool use and may influence later routing decisions.\n\n"
            "连续空转时请收集 helper、派发有用任务或按格式收尾。"
        )

def _repair_tool_call_pairing(msgs: list[dict]) -> int:
    return _repair_tool_call_pairing_impl(msgs, debug=debug)


_client: Optional[AsyncOpenAI] = None

# ── 重试 ─────────────────────────────────────────────────────
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds, multiplied exponentially: 1s, 2s, 4s


_provider_limiters: dict[tuple[int, str], asyncio.Semaphore] = {}


def _provider_key(provider: Any | None) -> str:
    if provider is None:
        return "default"
    name = getattr(provider, "name", "") or "provider"
    base_url = getattr(provider, "base_url", "") or ""
    return f"{name}:{base_url.rstrip('/')}"


def _provider_limit(provider: Any | None) -> int:
    name = (getattr(provider, "name", "") or "").lower()
    if name == "gpt55":
        value = settings.llm_gpt55_max_concurrent
    elif name == "deepseek":
        value = settings.llm_deepseek_max_concurrent
    else:
        value = settings.llm_provider_max_concurrent_default
    try:
        return max(1, int(value))
    except Exception:
        return 1


def _provider_limiter(provider: Any | None) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), _provider_key(provider))
    sem = _provider_limiters.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_provider_limit(provider))
        _provider_limiters[key] = sem
    return sem


async def _with_provider_limit(fn, *, provider: Any | None = None, label: str = "llm") -> Any:
    sem = _provider_limiter(provider)
    limit = _provider_limit(provider)
    try:
        queued = max(0, len(getattr(sem, "_waiters", ()) or ()))
    except Exception:
        queued = 0
    if queued:
        debug.log(
            "llm.provider_queue",
            f"label={label} provider={_provider_key(provider)} queued={queued} limit={limit}",
        )
    async with sem:
        return await fn()

# 2026-05-03 v18.x:同 task_id 累计 timeout 次数,用于自动降级 reasoning
# key = task_id (None 主线程也加,但主线程一般每 trace 至多 1-2 次 timeout)
# value = 累计 timeout 数
# 不持久化,进程重启清零。
# 2026-05-06 §A3:从 module-level dict 改为 ContextVar — 旧实现即使入口清零,
# 不同 task_id 之间还是共享同一 dict,一个用户 helper 卡死累计的 timeout
# 会被另一个用户的 helper 看到。ContextVar 确保每个 asyncio task 独立。
from contextvars import ContextVar

_helper_timeout_counts_var: ContextVar[dict[str | None, int]] = ContextVar(
    "_helper_timeout_counts", default=None,
)

def _get_timeout_dict() -> dict[str | None, int]:
    d = _helper_timeout_counts_var.get()
    if d is None:
        d = {}
        _helper_timeout_counts_var.set(d)
    return d


# ──────────────────────────────────────────────────────────────
# 2026-05-04 v19:Streaming 工具循环实现
#
# 背景:旧的 chat_with_tools_loop 用 `stream=False` + 上层 `asyncio.wait_for(90s)`
# 做硬截断。实测 trace 1a05ecf0 / 1fbb00b6 写大型 docx 论文时:
#   - LLM 服务端 reasoning 100-200s 是正常的
#   - 90s 没收到完整响应就被强行 cancel → 任务真的需要长时间但被误杀
#   - retry 也 90s 超(thinking_disabled 也救不了 — 任务本身需要长时间)
#
# 根治:改 streaming + idle-based timeout
#   - 每个 chunk(reasoning token / content token / tool_call delta)刷新 idle 计时
#   - LLM 在持续输出 = 一直在工作 → 不杀
#   - 真正卡住(idle 过久)才 cancel → 精准识别"卡死" vs "在思考"
#
# 续写策略(idle timeout 触发后,用户提的"直接拼接"方案):
#   - 已收到 partial content / tool_calls 不丢
#   - 拼到下一次 messages 末尾作为 assistant prefix(用普通 /chat/completions
#     的 prefix=True 字段,无 tools 时走 /beta;有 tools 时拼成 user 提示让
#     模型自然续上)
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# 2026-05-04 v19.1:Timeout 策略二分(thinking vs non-thinking)
#
# 用户明确需求:
#   - non-think 路径(thinking=disabled):只要流式有 chunk 在出就不停 — 无 idle
#     timeout(模型一直在吐 token = 一直在工作,API 不会平白挂起几分钟还在吐)
#   - think 路径:thinking chain 偶尔会卡死(reasoning loop 内部死循环),
#     需要 timeout 兜底,统一 90s 即可
#   - 超时后:保留已收到的 reasoning_content + content + tool_calls partial,
#     拼成续写提示注入下次调用(让模型看到"我之前想了什么 / 写到哪了")
#
# 这个二分基于实测:thinking_disabled 模式下 99.9% 不会卡(模型直接输出);
# thinking 模式偶尔会有 reasoning chain 死循环(已知 trace 9b3a6a/85e330/94fe17 都是这种)。
# ──────────────────────────────────────────────────────────────

# Think 路径(thinking enabled / high / max):统一 90s idle/first chunk
# 超时即触发续写或 thinking_disabled 降级,**不再因为"任务大"放宽到 300s**,
# 因为大任务根因是 reasoning chain 死循环,不是任务真需要 5 分钟思考
_THINK_STREAM_IDLE_TIMEOUT = 90.0
_THINK_STREAM_FIRST_CHUNK_TIMEOUT = 90.0

# Non-think 路径(thinking=disabled):无 idle 限制
# 用一个极大值(1 小时)兜底防真死锁,但实际 chunks 持续来就不会触发
_NOTHINK_STREAM_IDLE_TIMEOUT = 3600.0  # 1h 兜底,实际不该触发
_NOTHINK_STREAM_FIRST_CHUNK_TIMEOUT = 180.0  # first chunk 仍给 3 分钟(冷启动 / 排队)


def _configured_stream_first_chunk_timeout(default: float) -> float:
    try:
        value = float(settings.llm_stream_first_chunk_timeout_sec)
    except Exception:
        value = 0.0
    return value if value > 0 else default


def _record_stream_usage_for_test(
    *,
    model: str,
    task_id: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit: int,
    cache_miss: int,
) -> None:
    """Record streaming usage metrics; kept as a small testable helper."""
    from app.llm.client_tools_loop import _tools_loop_usage_tag
    _tag = _tools_loop_usage_tag(task_id)
    try:
        _record_llm_usage_and_log(
            tag=_tag, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cache_hit=cache_hit, cache_miss=cache_miss,
        )
        if task_id:
            try:
                from app.core.core_processes import current_helper_kind as _current_helper_kind
                _helper_kind = (_current_helper_kind() or "").strip()
            except Exception:
                _helper_kind = ""
            if _helper_kind:
                _record_llm_usage_and_log(
                    tag=f"helper_kind.{_helper_kind}", model=model,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    cache_hit=cache_hit, cache_miss=cache_miss,
                )
    except Exception:
        pass


def _record_llm_usage_and_log(
    *,
    model: str,
    tag: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit: int,
    cache_miss: int,
) -> None:
    """Record provider usage/cache data with a stable tag."""
    safe_tag = str(tag or "unknown")
    hit = int(cache_hit or 0)
    miss = int(cache_miss or 0)
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    total = hit + miss
    if total > 0:
        rate = hit * 100 // total
        debug.log(
            "llm.cache_stats",
            f"P49 [{safe_tag}]: model={model} prompt={prompt} completion={completion} "
            f"cache_hit={hit} cache_miss={miss} hit_rate={rate}%",
        )
    else:
        debug.log(
            "llm.cache_stats",
            f"P49 [{safe_tag}]: model={model} prompt={prompt} completion={completion} "
            f"(no cache stats in usage)",
        )
    try:
        from app.core import metrics as _metrics
        _metrics.record_llm_usage(
            tag=safe_tag,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit=hit,
            cache_miss=miss,
        )
    except Exception:
        pass


def _record_response_usage(
    resp: Any,
    *,
    model: str,
    tag: str,
) -> None:
    """Extract OpenAI-compatible usage from a non-streaming response."""
    usage = getattr(resp, "usage", None)
    _record_usage_payload(usage, model=model, tag=tag)


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    """Normalize SDK usage objects into a plain dict for cache accounting.

    OpenAI-compatible providers expose cache fields in different places. Some
    use flat `prompt_cache_hit_tokens`, others expose
    `prompt_tokens_details.cached_tokens`, and Responses-style payloads may use
    `input_tokens` / `input_tokens_details.cached_tokens`. This function keeps
    logging provider-agnostic without changing request behavior.

    统一 usage 字段形态，避免真实缓存统计因上游字段差异丢失。
    """
    if not usage:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        try:
            dumped = usage.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            return {}
    data: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_tokens_details",
        "input_tokens_details",
        "completion_tokens_details",
    ):
        try:
            value = getattr(usage, key)
        except Exception:
            continue
        if value is not None:
            data[key] = value
    return data


def _usage_nested_int(data: dict[str, Any], *path: str) -> int:
    current: Any = data
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            try:
                current = getattr(current, key)
            except Exception:
                return 0
        if current is None:
            return 0
    try:
        return int(current or 0)
    except Exception:
        return 0


def _extract_usage_cache_tokens(usage: dict[str, Any]) -> tuple[int, int]:
    """Return cache hit/miss tokens from flat or nested provider usage."""
    prompt = _usage_nested_int(usage, "prompt_tokens") or _usage_nested_int(usage, "input_tokens")
    hit = (
        _usage_nested_int(usage, "prompt_cache_hit_tokens")
        or _usage_nested_int(usage, "cache_hit_tokens")
        or _usage_nested_int(usage, "cache_read_input_tokens")
    )
    miss = (
        _usage_nested_int(usage, "prompt_cache_miss_tokens")
        or _usage_nested_int(usage, "cache_miss_tokens")
        or _usage_nested_int(usage, "cache_creation_input_tokens")
    )
    if hit or miss:
        if hit and not miss and prompt:
            miss = max(0, prompt - hit)
        return hit, miss

    cached = (
        _usage_nested_int(usage, "prompt_tokens_details", "cached_tokens")
        or _usage_nested_int(usage, "input_tokens_details", "cached_tokens")
    )
    if cached:
        prompt_miss = max(0, prompt - cached) if prompt else 0
        return cached, prompt_miss
    return 0, 0


def _record_usage_payload(
    usage: Any,
    *,
    model: str,
    tag: str,
) -> None:
    """Extract OpenAI-compatible usage from a response or stream chunk."""
    usage = _usage_to_dict(usage)
    if not usage:
        return
    cache_hit, cache_miss = _extract_usage_cache_tokens(usage)
    _record_llm_usage_and_log(
        model=model,
        tag=tag,
        prompt_tokens=usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0,
        completion_tokens=usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0,
        cache_hit=cache_hit,
        cache_miss=cache_miss,
    )


# 消息/格式化工具族已抽离到 message_utils.py(2026-05-20 重构);re-export 兼容。
from app.llm.message_utils import (  # noqa: E402,F401
    _estimate_msgs_token_size,
    _now_iso,
    _short_action_desc,
    _safe_progress,
    _is_thinking_enabled,
    _thinking_extra_body,
    _build_continuation_prefix_message,
    _summarize_tool_history,
    _tool_result_signal,
    _inject_tool_timestamps,
    _serialize_assistant_message,
    _extract_iteration_timeline,
    _soft_compact_redundant_tool_results,
)


class _StreamCollector:
    """收集 streaming chunks → 拼成兼容 non-streaming response.choices[0].message 的对象。

    支持中途断流:已收到的 content / reasoning_content / tool_calls(按 index 累积
    arguments)都保留,可以传给续写逻辑作为 prefix。
    """
    # 2026-05-09 Patch 5:DeepSeek 内部工具调用 DSML 格式泄漏成 content 的特征字符串。
    # 出现一次就说明模型已经丢失格式协议,后续不会自动恢复 — 应立刻断流走 forced finalize。
    # trace 96c47298 实测:iter 22 模型把 delegate(wait_for=...) 写成了
    # `<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke>...["rb_tree","bplus_tree"]</｜｜DSML｜｜parameter>`
    # 当 content 字面量,API 没识别为 tool_calls,主线程后续等了 9 分钟才拿到完整垃圾。
    # 字面量含全角半角分隔符,原 prompt 防御失败 → 这里做 collector 层拦截。
    _DSML_LEAK_MARKERS = (
        "<｜｜DSML｜｜",     # 全角(实测出现的形态)
        "<|DSML|",         # 半角变体(防御性)
        "｜｜DSML｜｜",      # 不带尖括号的子串(尖括号可能在另一个 chunk)
    )

    def __init__(self) -> None:
        self.content: str = ""
        self.reasoning_content: str = ""
        self.tool_calls: dict[int, dict] = {}  # index -> partial dict
        self.finish_reason: Optional[str] = None
        self.role: str = "assistant"
        self.chunk_count: int = 0
        self.first_chunk_at: Optional[float] = None
        self.last_chunk_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.dsml_leak_detected: bool = False  # 2026-05-09 Patch 5
        self._dsml_check_offset: int = 0       # 上次扫描 content 到的位置
        # 2026-05-12 P49: 收集 LLM API usage (含 cache hit/miss tokens)
        # 病因: 85% cache 命中率没监控点, 优化效果无法量化。
        # DeepSeek 在 streaming 最后 chunk 返回 usage:
        #   {prompt_tokens, completion_tokens,
        #    prompt_cache_hit_tokens, prompt_cache_miss_tokens}
        # 提取后由调用方统一 log。
        self.usage: Optional[dict] = None

    def absorb(self, chunk) -> None:
        self.chunk_count += 1
        now = time.monotonic()
        if self.first_chunk_at is None:
            self.first_chunk_at = now
        self.last_chunk_at = now
        # 2026-05-12 P49: 提取 usage (cache hit/miss tokens) — 在 choices 检查前
        # DeepSeek streaming 在 final chunk (无 choices) 或 final delta 带 usage。
        _u = getattr(chunk, "usage", None)
        if _u is not None:
            # 转 dict 兼容 SDK 对象 / dict 两种返回，并保留嵌套 cache usage 字段。
            try:
                self.usage = _usage_to_dict(_u)
            except Exception:
                pass
        if not getattr(chunk, "choices", None):
            return
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            return
        # role
        role = getattr(delta, "role", None)
        if role:
            self.role = role
        # content
        c = getattr(delta, "content", None)
        if c:
            self.content += c
            # 2026-05-09 Patch 5:增量扫描新增片段是否含 DSML 泄漏标记。
            # 用 _dsml_check_offset 避免每个 chunk 重扫整个 content;
            # 标记最长 ~10 字符,所以扫"上次结束位置 - 10"开始即可不漏跨 chunk 边界的标记。
            if not self.dsml_leak_detected:
                start = max(0, self._dsml_check_offset - 16)
                tail = self.content[start:]
                for mk in self._DSML_LEAK_MARKERS:
                    if mk in tail:
                        self.dsml_leak_detected = True
                        break
                self._dsml_check_offset = len(self.content)
        # reasoning_content (DeepSeek 思考内容,工具循环里需要回传)
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            self.reasoning_content += rc
        # tool_calls (按 index 增量累积)
        for tc_delta in (getattr(delta, "tool_calls", None) or []):
            idx = getattr(tc_delta, "index", 0) or 0
            if idx not in self.tool_calls:
                self.tool_calls[idx] = {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            buf = self.tool_calls[idx]
            tc_id = getattr(tc_delta, "id", None)
            if tc_id:
                buf["id"] = tc_id
            tc_type = getattr(tc_delta, "type", None)
            if tc_type:
                buf["type"] = tc_type
            fn = getattr(tc_delta, "function", None)
            if fn is not None:
                fn_name = getattr(fn, "name", None)
                if fn_name:
                    buf["function"]["name"] = fn_name
                fn_args = getattr(fn, "arguments", None)
                if fn_args:
                    buf["function"]["arguments"] += fn_args
        # finish_reason
        fr = getattr(choice, "finish_reason", None)
        if fr:
            self.finish_reason = fr

    def has_partial(self) -> bool:
        """是否已收到任何有用的 partial 内容。"""
        return bool(self.content or self.reasoning_content or self.tool_calls)

    def to_message(self):
        """重建一个 SDK ChatCompletionMessage 兼容对象,供上层用 .content / .tool_calls 访问。"""
        from types import SimpleNamespace

        tool_calls_list = None
        if self.tool_calls:
            tool_calls_list = []
            for idx in sorted(self.tool_calls.keys()):
                tc = self.tool_calls[idx]
                # SDK 风格 SimpleNamespace,上游用 .id / .type / .function.name / .function.arguments
                tool_calls_list.append(SimpleNamespace(
                    id=tc["id"] or f"call_streamed_{idx}",
                    type=tc.get("type", "function"),
                    function=SimpleNamespace(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    ),
                ))
        msg = SimpleNamespace(
            role=self.role,
            content=self.content or None,
            tool_calls=tool_calls_list,
            reasoning_content=self.reasoning_content or None,
        )
        return msg

    def to_response(self, model: str):
        """重建一个 ChatCompletion 兼容对象 — 用 .choices[0].message 访问。"""
        from types import SimpleNamespace
        choice = SimpleNamespace(
            index=0,
            message=self.to_message(),
            finish_reason=self.finish_reason or "stop",
        )
        return SimpleNamespace(
            id="streamed",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[choice],
        )



async def _call_llm_streaming_with_idle(
    *,
    the_client: AsyncOpenAI,
    model: str,
    provider: Any | None = None,
    msgs: list,
    tools: list,
    extra_body: dict,
    abort_event: Optional[asyncio.Event],
    iter_no: int,
    task_id: Optional[str],
    idle_timeout: float,
    first_chunk_timeout: float,
    label_suffix: str = "",
    tool_choice: Any | None = None,
    progress_log_every_chunks: int = 80,
    chunk_callback=None,  # 2026-05-05: async cb() called per chunk (API stall detection)
    stream_event_cb=None,  # 2026-05-09: cb("open"|"close", reason?) for stall detection at orchestrator level
) -> tuple[Any, _StreamCollector, str]:
    """真正的 streaming + idle-based timeout 实现。

    与 non-streaming 模式的关键差异:
    - 每个 chunk 重置 idle 计时器 → 模型持续输出就一直读
    - first chunk 单独 budget(API 启动 + reasoning chain 第一阶段)
    - 后续 chunks 用更短的 idle budget(token 之间几秒空隙正常,几十秒沉默才异常)

    返回与 non-streaming 同样的 response_compat 对象,
    + collector(包含 partial 状态,用于续写)
    + exit_reason 标记为什么停的。
    """
    collector = _StreamCollector()

    async def _cancel_task_bounded(
        task: asyncio.Task | None,
        label: str,
        *,
        timeout: float = 2.0,
    ) -> None:
        if task is None:
            return
        if task.done():
            try:
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass
            return

        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            debug.warn(
                f"streaming cancel cleanup still pending after {timeout}s "
                f"at iter {iter_no} task={task_id}: {label}; continuing"
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _do_create():
        # 2026-05-12 P50: streaming 模式必须显式请求 usage 才会在 final chunk 返回。
        # DeepSeek/OpenAI 兼容 API 默认不返回 usage in streaming, 需要传
        # `stream_options={"include_usage": True}`。
        # 配合 P49 (collector 收集 usage) 实现 cache hit rate 监控。
        effective_tool_choice = tool_choice if tools else None
        if effective_tool_choice is not None and _omit_tool_choice_for_thinking(extra_body, provider):
            debug.warn(
                f"omitting tool_choice for thinking-mode request at iter {iter_no} "
                f"task={task_id}; model may still call tools naturally"
            )
            effective_tool_choice = None
        from app.llm.client_tools_loop import _tools_loop_shape_label
        shape_label = _tools_loop_shape_label(iter_no, task_id)
        _log_prompt_cache_shape(
            label=shape_label,
            model=model,
            messages=msgs,
            tools=tools if tools else None,
        )
        return await _retry(
            lambda: the_client.chat.completions.create(
                model=model,
                messages=msgs,
                tools=tools if tools else None,
                tool_choice=effective_tool_choice,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=extra_body,
                timeout=first_chunk_timeout,
            ),
            label=f"tools loop iter={iter_no} (stream{label_suffix})",
            provider=provider,
            timeout_sec=first_chunk_timeout,
        )

    # 1. 启动 stream(create 本身有 1-3s TTFT)
    create_task = asyncio.create_task(_do_create())
    abort_task = (
        asyncio.create_task(abort_event.wait()) if abort_event is not None else None
    )

    waiters = {create_task}
    if abort_task is not None:
        waiters.add(abort_task)

    try:
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=first_chunk_timeout,
        )
    except BaseException:
        await _cancel_task_bounded(create_task, "create_task exception cleanup")
        await _cancel_task_bounded(abort_task, "abort_task exception cleanup")
        raise
    if abort_task is not None and abort_task in done and create_task not in done:
        await _cancel_task_bounded(create_task, "create_task after abort")
        return collector.to_response(model), collector, "abort"
    if abort_task is not None and abort_task not in done:
        await _cancel_task_bounded(abort_task, "unused abort_task")
    if not done or create_task not in done:
        await _cancel_task_bounded(create_task, "create_task first_chunk_timeout")
        return collector.to_response(model), collector, "first_chunk_timeout"

    try:
        stream = create_task.result()
    except Exception as e:
        collector.last_error = f"{type(e).__name__}: {e}"
        debug.error(
            f"streaming create failed at iter {iter_no} task={task_id}: "
            f"{collector.last_error}"
        )
        return collector.to_response(model), collector, "error"

    # 2026-05-09: 通知调用方 stream 已打开,可以开始监控 stall(只对开着的 stream 计时)
    if stream_event_cb is not None:
        try:
            stream_event_cb("open")
        except Exception:
            pass

    # 2026-05-09: 局部辅助 — 任何 stream-after-open 退出点统一调一次,
    # 让调用方知道"现在没在 stream 了"。reason 透传:abort/idle_timeout/error/ok 等。
    def _fire_close(reason: str) -> None:
        if stream_event_cb is not None:
            try:
                stream_event_cb("close", reason)
            except Exception:
                pass

    # 2. 用 anext + wait_for 显式 idle-wrap 每个 chunk 读取
    stream_iter = stream.__aiter__()
    next_progress_at = progress_log_every_chunks
    chunk_idle_budget = first_chunk_timeout  # 第一个 chunk 仍用较长 budget
    # 2026-06-05 软化: 之前 12K 触发硬关流,但合法的文档/代码生成 (一次写入 docx
    # 章节、写完整 matplotlib 脚本、生成长 .py 实现) 经常超过 12K,被阻断 → 续写
    # 非常碎片化。提高 close 阈值到 24K,warn 阈值到 14K。真正失控 (循环重复内容)
    # 在 24K 时用户也会看到, 但合法长内容能一次过。
    helper_tool_arg_bloat_warn_at = 14_000
    helper_tool_arg_bloat_close_at = 24_000
    helper_tool_arg_bloat_warned = False

    try:
        while True:
            # 2026-05-04 v19:同时 race abort_event 和 anext,
            # 让 abort 在两个 chunk 之间的等待也能立即响应(不等到下一个 chunk)。
            anext_task = asyncio.ensure_future(stream_iter.__anext__())
            abort_wait_task = (
                asyncio.ensure_future(abort_event.wait())
                if abort_event is not None else None
            )
            waiters = {anext_task}
            if abort_wait_task is not None:
                waiters.add(abort_wait_task)
            try:
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=chunk_idle_budget,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                anext_task.cancel()
                if abort_wait_task is not None:
                    abort_wait_task.cancel()
                raise

            # 优先级 1:abort 命中 → 立即停止
            if abort_wait_task is not None and abort_wait_task in done:
                anext_task.cancel()
                try:
                    await anext_task
                except (asyncio.CancelledError, StopAsyncIteration, Exception):
                    pass
                debug.log(
                    "llm.tools.stream.abort",
                    f"abort during stream at iter {iter_no}; "
                    f"closing stream (chunks_received={collector.chunk_count})"
                )
                try:
                    await stream.close()
                except Exception:
                    pass
                _fire_close("abort")
                return collector.to_response(model), collector, "abort"

            # cleanup abort waiter(还在 pending 时取消)
            if abort_wait_task is not None and abort_wait_task not in done:
                abort_wait_task.cancel()
                try:
                    await abort_wait_task
                except (asyncio.CancelledError, Exception):
                    pass

            # 优先级 2:anext 完成
            if anext_task in done:
                try:
                    chunk = anext_task.result()
                except StopAsyncIteration:
                    break
                except Exception as iter_e:
                    # iteration 中途异常通常是上游连接断开 / incomplete chunked read。
                    # Treat it as recoverable stream interruption so the tools loop can retry
                    # with continuation or lower reasoning. Returning "error" here makes
                    # Round2 fall back immediately and can lose otherwise valid long-tool work.
                    debug.error(
                        f"streaming chunk iteration failed at iter {iter_no} "
                        f"task={task_id}: {type(iter_e).__name__}: {iter_e}"
                    )
                    collector.last_error = f"{type(iter_e).__name__}: {iter_e}"
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    reason = "idle_timeout"
                    _fire_close(reason)
                    return collector.to_response(model), collector, reason
            else:
                # 优先级 3:idle timeout(没 chunk 也没 abort)
                anext_task.cancel()
                try:
                    await anext_task
                except (asyncio.CancelledError, StopAsyncIteration, Exception):
                    pass
                reason = "idle_timeout" if collector.has_partial() else "first_chunk_timeout"
                debug.warn(
                    f"stream {reason} at iter {iter_no} task={task_id}: "
                    f"no chunk for {chunk_idle_budget}s "
                    f"(received {collector.chunk_count} chunks, "
                    f"content={len(collector.content)} chars, "
                    f"reasoning={len(collector.reasoning_content)} chars, "
                    f"tool_calls={len(collector.tool_calls)})"
                )
                try:
                    await stream.close()
                except Exception:
                    pass
                _fire_close(reason)
                return collector.to_response(model), collector, reason

            collector.absorb(chunk)
            # 收到 chunk 后切换后续 idle 预算。工具调用已经开始后，如果上游不再
            # 继续发送参数块，尽快交给上层 partial tool_call 接管；持续出块不受影响。
            chunk_idle_budget = idle_timeout
            if collector.tool_calls:
                try:
                    named_tool_calls = [
                        tc for tc in collector.tool_calls.values()
                        if str(((tc.get("function") or {}).get("name") or "")).strip()
                    ]
                    has_named_tool_call = bool(named_tool_calls)
                    args_are_parseable = bool(named_tool_calls)
                    for tc in named_tool_calls:
                        args_text = str(((tc.get("function") or {}).get("arguments") or "")).strip()
                        if not args_text:
                            args_are_parseable = False
                            break
                        try:
                            json.loads(args_text)
                        except Exception:
                            args_are_parseable = False
                            break
                except Exception:
                    has_named_tool_call = False
                    args_are_parseable = False
                if has_named_tool_call and args_are_parseable:
                    chunk_idle_budget = min(chunk_idle_budget, 12.0)
            # 2026-05-05: chunk callback (for API stall detection at helper level)
            if chunk_callback is not None:
                try:
                    chunk_callback()
                except Exception:
                    pass

            # 2026-05-09 Patch 5:DSML 格式泄漏即时断流。
            # absorb 里增量扫描发现 `<｜｜DSML｜｜...>` 这种 DeepSeek 内部 tool-call
            # 标记当 content 字面量出现 → 模型已丢失格式协议,继续流只会等更多垃圾。
            # 早断让 chat_with_tools_loop 走 forced_finalize 用 tool_choice="none" +
            # response_format=json_object 强制重派,通常能在数秒内救出来。
            # trace 96c47298 实测:不断流主线程要等 ~9 分钟,断流后预期 < 30 秒收尾。
            if collector.dsml_leak_detected:
                debug.warn(
                    f"DSML format leak detected in stream content at iter {iter_no} "
                    f"task={task_id} (chunks={collector.chunk_count}, "
                    f"content_len={len(collector.content)}). "
                    f"Closing stream early to trigger forced_finalize."
                )
                debug.log(
                    "llm.tools.stream.dsml_leak",
                    f"DSML leak in content; aborting stream at iter {iter_no}",
                    collector.content[-300:],
                )
                try:
                    await stream.close()
                except Exception:
                    pass
                # 用 idle_timeout 而不是 error,让上层走 partial 续写/forced finalize 路径,
                # 不丢已收到的 reasoning + tool_calls 内容。
                _fire_close("dsml_leak")
                return collector.to_response(model), collector, "idle_timeout"

            # ── 2026-05-11: 主线程写源码早断 ──
            # 实测教训(trace 2026-05-11 12:12 → 13:06):主线程 streaming 生成 14443 字符
            # 的 generate_paper.py,服务端在生成完才能 reject,**100 秒不可挽回**。
            #
            # 早断条件(满足全部才断):
            #   1. 是主线程 (task_id is None)
            #   2. 当前在 streaming workspace.write 调用
            #   3. args 累积里看得到源码扩展名(.py/.c/.h/.cpp 等)
            #   4. args 累积 > 5500 字符(.py 上限 1500 已超 3.6x,确定要被 server reject)
            #
            # 早断收益:从浪费 100 秒(14000 字符)→ 浪费 ~5 秒(5500 字符)
            # 早断后:返回专用 reason,collector 已 partial,上层走重试路径(LLM 看到 partial
            # 被截会重新决策,这次大概率不再走 workspace.write 而是 delegate)。
            if (
                task_id is None
                and collector.tool_calls
                and collector.chunk_count % 32 == 0  # 每 32 chunk 检查一次,不每 chunk 检查
            ):
                _main_thread_source_write_detected = False
                for _tc in collector.tool_calls.values():
                    _fn = _tc.get("function", {})
                    _tool_name = _fn.get("name")
                    if _tool_name not in {"workspace", "env_apply_create"}:
                        continue
                    _args = _fn.get("arguments", "") or ""
                    if len(_args) < 5500:
                        continue
                    # partial JSON 可能没合法闭合;用字符串 contain 判断
                    if (
                        _tool_name == "workspace"
                        and '"action":"write"' not in _args
                        and '"action": "write"' not in _args
                    ):
                        continue
                    if _tool_name == "env_apply_create" and '"content"' not in _args:
                        continue
                    # 看是不是源码扩展名
                    _src_ext_markers = (
                        '.py"', '.c"', '.cpp"', '.cc"', '.cxx"',
                        '.h"', '.hpp"', '.hxx"',
                        '.js"', '.ts"', '.jsx"', '.tsx"',
                        '.go"', '.rs"', '.java"', '.cs"', '.kt"', '.swift"',
                    )
                    if any(m in _args for m in _src_ext_markers):
                        _main_thread_source_write_detected = True
                        break
                if _main_thread_source_write_detected:
                    debug.warn(
                        f"Main thread streaming source-write detected at iter {iter_no}: "
                        f"args累积 {len(_args)} chars, 早断 stream(主线程禁写代码硬规则)。"
                    )
                    debug.log(
                        "llm.tools.stream.main_source_write_abort",
                        f"tool={_tool_name} args长度={len(_args)}, "
                        "早断省剩余生成时间并让主进程改用 helper envelope",
                    )
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    _fire_close("main_thread_source_write")
                    return collector.to_response(model), collector, "main_thread_source_write"

            # 周期性进度日志(每 N 个 chunk 一次,不刷屏)
            if collector.chunk_count >= next_progress_at:
                # 2026-05-11: 同时显示 tool_call argument 累积长度。
                # 实测教训(trace 12:56-13:04 paper_final iter 12):helper streaming 一个
                # 14000+ 字符的 tool_call argument 8 分钟,progress 只显示 tool_calls=1
                # 而 content/reasoning 字段不变 → 主线程 API stall 监控误判为卡死 → kill.
                # 加上 args 长度后,监控可见 LLM 仍在产出 token,不会误杀。
                _tc_args_total = sum(
                    len((tc.get("function") or {}).get("arguments", "") or "")
                    for tc in collector.tool_calls.values()
                )
                if task_id is not None and collector.tool_calls:
                    _largest_tool = ""
                    _largest_args = 0
                    for _tc in collector.tool_calls.values():
                        _fn = _tc.get("function") or {}
                        _args_text = _fn.get("arguments", "") or ""
                        if len(_args_text) > _largest_args:
                            _largest_args = len(_args_text)
                            _largest_tool = str(_fn.get("name") or "")
                    if _largest_args >= helper_tool_arg_bloat_warn_at and not helper_tool_arg_bloat_warned:
                        helper_tool_arg_bloat_warned = True
                        debug.warn(
                            f"Helper streaming tool args are very large at iter {iter_no}: "
                            f"task={task_id} tool={_largest_tool} args={_largest_args} chars"
                        )
                    if _largest_args >= helper_tool_arg_bloat_close_at:
                        debug.warn(
                            f"Helper streaming tool args exceeded convergence limit at iter {iter_no}: "
                            f"task={task_id} tool={_largest_tool} args={_largest_args} chars; closing stream"
                        )
                        try:
                            await stream.close()
                        except Exception:
                            pass
                        _fire_close("helper_tool_call_bloat")
                        return collector.to_response(model), collector, "helper_tool_call_bloat"
                debug.log(
                    "llm.tools.stream.progress",
                    f"iter {iter_no} task={task_id}: "
                    f"chunks={collector.chunk_count} "
                    f"content={len(collector.content)} "
                    f"reasoning={len(collector.reasoning_content)} "
                    f"tool_calls={len(collector.tool_calls)} "
                    f"tc_args={_tc_args_total}"
                )
                next_progress_at += progress_log_every_chunks
                # 2026-05-11 P-streaming-heartbeat: 在 streaming 长 tool_call 期间也
                # ping helper heartbeat,让主线程通过 processes.list 看到 helper 还活着。
                # 实测教训(trace 12:56-13:04 paper_final): helper iter 12 streaming
                # 14000+ 字符 tool_call args 用了 8 分钟, last_progress_at 不更新 →
                # heartbeat_age 470s → 主线程认为 stale → kill API stall emergency.
                # 真实场景: helper 在正常 streaming, 没卡死, 不该被 kill.
                # 修法: progress log 时顺便 ping heartbeat (fire-and-forget).
                try:
                    from app.core.core_processes import (
                        report_helper_progress, current_helper_proc_id,
                    )
                    if current_helper_proc_id() is not None:
                        from app.core.bg_tasks import schedule

                        # 让 helper 心跳显示 streaming 进度
                        _note = (
                            f"streaming iter {iter_no}: "
                            f"{collector.chunk_count} chunks, "
                            f"tc_args={_tc_args_total}c"
                        )
                        schedule(
                            report_helper_progress(iter_num=iter_no, note=_note),
                            name=f"helper_progress:stream:{task_id or 'unknown'}",
                        )
                except Exception:
                    pass  # 心跳失败永不阻塞 streaming

    except Exception as e:
        # iteration 中途异常(网络抖 / SDK 错)— 透传
        debug.error(
            f"streaming chunk iteration failed at iter {iter_no} task={task_id}: "
            f"{type(e).__name__}: {e}"
        )
        try:
            await stream.close()
        except Exception:
            pass
        # 已有 partial → 视作 idle_timeout(可续写);否则 error
        reason = "idle_timeout" if collector.has_partial() else "error"
        _fire_close(reason)
        return collector.to_response(model), collector, reason

    # stream 自然结束
    try:
        await stream.close()
    except Exception:
        pass
    _fire_close("ok")

    # 2026-05-12 P49: stream 结束时 log cache 命中率
    # 2026-05-12 P54: 加 task_id 分类标记 (main / helper.<tid>) 方便部署后 grep 分组统计
    # 2026-05-15: 同时累计到 app.core.metrics, /metrics 端点暴露 Prometheus 格式
    # DeepSeek usage 字段: prompt_tokens, completion_tokens,
    # prompt_cache_hit_tokens, prompt_cache_miss_tokens
    if collector.usage:
        _u = collector.usage
        _hit, _miss = _extract_usage_cache_tokens(_u)
        _prompt = _usage_nested_int(_u, "prompt_tokens") or _usage_nested_int(_u, "input_tokens")
        _completion = _usage_nested_int(_u, "completion_tokens") or _usage_nested_int(_u, "output_tokens")
        # P54: tag 标识来源, 便于按 main/helper 分组统计
        from app.llm.client_tools_loop import _tools_loop_usage_tag
        _tag = _tools_loop_usage_tag(task_id)
        _record_llm_usage_and_log(
            model=model,
            tag=_tag,
            prompt_tokens=_prompt,
            completion_tokens=_completion,
            cache_hit=_hit,
            cache_miss=_miss,
        )
        # Also log a helper-kind aggregate for offline cache reports. The
        # task-id tag stays as the canonical per-helper row; the kind tag shows
        # whether same-kind helper prompts reuse their stable prefix.
        #
        # 同时记录 helper kind 聚合，便于离线报告分析同类 helper 前缀复用。
        if task_id:
            try:
                from app.core.core_processes import current_helper_kind as _current_helper_kind
                _helper_kind = (_current_helper_kind() or "").strip()
                if _helper_kind:
                    _record_llm_usage_and_log(
                        tag=f"helper_kind.{_helper_kind}",
                        model=model,
                        prompt_tokens=_prompt,
                        completion_tokens=_completion,
                        cache_hit=_hit,
                        cache_miss=_miss,
                    )
            except Exception:
                pass
    return collector.to_response(model), collector, "ok"









# JSON 解析/修复族已抽离到 json_utils.py(2026-05-20 重构);re-export 兼容。
from app.llm.json_utils import (  # noqa: E402,F401
    _escape_control_chars_in_json_strings,
    _complete_truncated_json_suffix,
    _escape_inner_quotes_in_json_strings,
    _try_parse_json,
    _parse_json_strict,
    _try_extract_json_locally,
    _normalize_tool_call_args_for_dispatch,
    stable_prompt_json,
    TOOL_ARGS_JSON_BROKEN_HINT,
)








async def _retry(
    fn,
    *,
    label: str = "llm",
    provider: Any | None = None,
    timeout_sec: float | None = None,
) -> Any:
    """Exponential backoff retry for transient API failures (429, 5xx, connection)."""
    last_exc = None
    for attempt in range(1 + _MAX_RETRIES):
        try:
            return await _create_with_timeout(
                _with_provider_limit(fn, provider=provider, label=label),
                label=label,
                timeout_sec=timeout_sec,
            )
        except asyncio.TimeoutError as e:
            debug.warn(f"{label} local timeout; not retrying same non-stream request")
            raise
        except (APITimeoutError, APIConnectionError) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BACKOFF_BASE ** attempt
                log.warning("llm retry %d/%d %s: %s", attempt + 1, _MAX_RETRIES, label, e)
                debug.log("llm.retry", f"attempt={attempt + 1} label={label} delay={delay:.1f}")
                await asyncio.sleep(delay)
        except APIStatusError as e:
            last_exc = e
            if e.status_code == 429 or e.status_code >= 500:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BACKOFF_BASE ** attempt
                    log.warning("llm retry %d/%d %s: HTTP %d", attempt + 1, _MAX_RETRIES, label, e.status_code)
                    debug.log("llm.retry", f"attempt={attempt + 1} label={label} status={e.status_code} delay={delay:.1f}")
                    await asyncio.sleep(delay)
                else:
                    raise
            else:
                raise  # 4xx (except 429) — no retry
    raise last_exc  # type: ignore[return-value]


async def _create_with_timeout(awaitable, *, label: str, timeout_sec: float | None = None) -> Any:
    """Bound non-streaming LLM creates so a dead upstream cannot hold locks forever."""
    timeout = settings.llm_call_timeout_sec if timeout_sec is None else timeout_sec
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        timeout_f = 0.0
    if timeout_f <= 0:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_f)
    except asyncio.TimeoutError:
        debug.warn(f"{label} timed out after {timeout_f:.1f}s")
        raise


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=None,
        )
    return _client


# Per-provider client cache for model_spec overrides (future multi-provider support).
_spec_client_cache: dict[tuple[str, str], AsyncOpenAI] = {}


def _legacy_model_spec(*, lite: bool = False, reasoning: str = "disabled"):
    """Resolve legacy lite/reasoning flags through the active model pool."""
    from app.llm.model_pool import resolve_task

    if lite:
        return resolve_task("helper_lite")
    if str(reasoning or "disabled").lower() not in {"", "disabled", "none"}:
        return resolve_task("helper_full_legacy_hard")
    return resolve_task("round3_normal")


def _client_for_spec(spec) -> AsyncOpenAI:
    """Get or create an AsyncOpenAI client for a ModelSpec's provider.

    Falls back to default client() when spec is None or matches default base_url.
    API key from settings is authoritative for the default endpoint.
    """
    if spec is None:
        return client()
    p = spec.provider
    # Same endpoint as default config → use the configured client
    # (its API key comes from settings/.env, which is correctly loaded)
    if p.base_url.rstrip("/") == settings.deepseek_base_url.rstrip("/"):
        return client()
    # Different provider → create dedicated client
    key = (p.api_key, p.base_url)
    if key not in _spec_client_cache:
        _spec_client_cache[key] = AsyncOpenAI(
            api_key=p.api_key,
            base_url=p.base_url,
            timeout=None,
        )
    return _spec_client_cache[key]


# 2026-05-03 加:beta client 用于 prefix 续写(对话前缀续写 API beta 功能)
# https://api-docs.deepseek.com/zh-cn/guides/chat_prefix_completion
# 用法:messages 末尾追加 {"role": "assistant", "content": "...", "prefix": True},
# LLM 从该 content 续写。
# 用途:hard timeout retry 时强引导短回应,绕过卡死的 thinking chain。
_beta_client: Optional[AsyncOpenAI] = None


def beta_client() -> AsyncOpenAI:
    """Beta endpoint client for prefix completion API.

    与主 client 分离,因为 base_url 不同(`/beta`)。其他 API 行为一致。
    """
    global _beta_client
    if _beta_client is None:
        # 优先用配置里的 beta url,没有就在主 url 加 /beta
        beta_url = getattr(settings, "deepseek_beta_base_url", None)
        if not beta_url:
            base = settings.deepseek_base_url.rstrip("/")
            # 已经是 /beta 就用,否则拼上
            if base.endswith("/beta"):
                beta_url = base
            else:
                beta_url = base + "/beta"
        _beta_client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=beta_url,
            timeout=None,
        )
    return _beta_client


async def chat_json(
    messages: list[dict],
    *,
    reasoning: str = "disabled",
    response_format_hint: str = "json",
    lite: bool = False,
    model_spec=None,
    metrics_tag: str = "json",
) -> dict:
    """
    要求模型返回 JSON。Prefill `{` 强制 JSON 开头。

    reasoning: "disabled"（默认） / "low" / "medium" / "high"。
    lite=True: 用 lite 模型。
    model_spec: ModelSpec override（优先于 lite/reasoning）。
    metrics_tag: stable observability tag for cache/usage diagnostics.
    """
    if model_spec is not None:
        model = model_spec.model
        reasoning = model_spec.reasoning
        _cli = _client_for_spec(model_spec)
    else:
        model_spec = _legacy_model_spec(lite=lite, reasoning=reasoning)
        model = model_spec.model
        reasoning = model_spec.reasoning
        _cli = _client_for_spec(model_spec)

    # Prefill: 在对话末尾插入 assistant 的 `{`，强制模型以 JSON 开头续写。
    msgs = [m.copy() if isinstance(m, dict) else m for m in messages]
    msgs.append({"role": "assistant", "content": "{"})

    debug.log("llm.json.input", f"model={model} reasoning={reasoning} msgs={len(msgs)}", {"messages": msgs})
    _log_prompt_cache_shape(
        label=metrics_tag,
        model=model,
        messages=msgs,
    )
    create_kwargs: dict[str, Any] = dict(
        model=model, messages=msgs, stream=False,
        extra_body=_thinking_extra_body(reasoning, model_spec.provider if model_spec else None),
        response_format={"type": "json_object"},
    )

    # 第一次：prefill + response_format
    _t0 = time.monotonic()
    try:
        resp = await _retry(
            lambda: _cli.chat.completions.create(**create_kwargs),
            label=f"chat_json model={model}",
            provider=model_spec.provider if model_spec else None,
        )
        _elapsed = time.monotonic() - _t0
        _record_response_usage(resp, model=model, tag=metrics_tag)
        content = resp.choices[0].message.content or ""
        debug.log("llm.json.raw", f"len={len(content)} elapsed={_elapsed:.1f}s", content)
        stripped = content.strip()
        full = stripped if stripped.startswith("{") else "{" + content
        try:
            parsed = _parse_json_strict(full.strip())
        except Exception:
            extracted = _try_extract_json_locally(content)
            if not extracted:
                raise
            parsed = _parse_json_strict(extracted)
        debug.log("llm.json.parsed", "parsed ok", parsed)
        return parsed
    except Exception as e:
        log.warning("chat_json first attempt failed (%s); fallback to bare", e)
        debug.log("llm.json.retry", "fallback to bare mode (no prefill, no response_format)")

    # 兜底：原始 messages + 不带 prefill / response_format。一次成败。
    bare_kwargs = dict(create_kwargs)
    bare_kwargs.pop("response_format", None)
    bare_kwargs["messages"] = messages
    _log_prompt_cache_shape(
        label=f"{metrics_tag}.bare_retry",
        model=model,
        messages=messages,
    )
    resp = await _retry(
        lambda: _cli.chat.completions.create(**bare_kwargs),
        label=f"chat_json bare retry model={model}",
        provider=model_spec.provider if model_spec else None,
    )
    _record_response_usage(resp, model=model, tag=f"{metrics_tag}.bare_retry")
    content = resp.choices[0].message.content or ""
    debug.log("llm.json.raw_bare", f"len={len(content)}", content)
    extracted = _try_extract_json_locally(content)
    parsed = _parse_json_strict(extracted or content)
    debug.log("llm.json.parsed", "parsed ok (bare)", parsed)
    return parsed


async def chat_json_with_upgrade(
    messages: list[dict],
    *,
    validate,
    label: str = "compress",
    lite_first: bool = True,
) -> dict | None:
    """先用 lite + reasoning="disabled" 跑（速度快、成本低）。
    若 LLM 异常或 validate(raw) 返回 False，自动升级到
    main 模型 + reasoning="max"（最强思考）重试一次。

    两次都失败返回 None；caller 应保留源数据等下次重试。

    专给后台压缩任务用——压缩有"完整性约束"（如所有 turn_id 必须被覆盖），
    简单分类任务 lite 够用，但偶尔 lite 切错话题边界，此时 main+thinking
    能修正。这样平均成本接近 lite，最坏情况能保住质量。

    validate: callable(raw_dict) -> bool，True 表示输出合格可用。
    label: debug 日志用的标识。
    lite_first: True（默认）先尝试 lite。设 False 直接 main+thinking。
    """
    if lite_first:
        try:
            raw = await chat_json(
                messages,
                reasoning="disabled",
                lite=True,
                metrics_tag=f"json.{label}.lite",
            )
            if validate(raw):
                return raw
            log.warning(
                "[%s] lite output failed validation; upgrading to main+thinking",
                label,
            )
            debug.log(
                f"compress.{label}.lite_failed",
                "validation failed, upgrading",
            )
        except Exception as e:
            log.warning(
                "[%s] lite attempt failed; upgrading to main+thinking: %s: %s",
                label,
                type(e).__name__,
                e,
            )

    try:
        raw = await chat_json(
            messages,
            reasoning="max",
            lite=False,
            metrics_tag=f"json.{label}.upgrade",
        )
        if validate(raw):
            debug.log(
                f"compress.{label}.upgraded",
                "main+max thinking succeeded after lite failure",
            )
            return raw
        log.warning(
            "[%s] main+thinking also failed validation; giving up", label,
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        log.warning("[%s] main+thinking timed out; giving up: %s", label, e)
    except Exception:
        log.exception("[%s] main+thinking attempt error", label)

    return None


async def chat_stream(
    messages: list[dict],
    *,
    reasoning: str = "disabled",
    lite: bool = False,
    abort_event: asyncio.Event | None = None,
    model_spec=None,
) -> AsyncIterator[str]:
    """
    流式 token 输出。Round3 用。reasoning 默认关闭——
    Round3 是润色，已有 Round2 的 plan 指导，无需深思考。

    lite: True 时用 lite 模型(deepseek-v4-flash),适用于 easy 路径短回复。
    model_spec: ModelSpec override（优先于 lite/reasoning）。

    abort_event: 2026-05-02 part10 (A3) 加。同 chat_with_tools_loop 的 racing 模式,
        让等 first chunk(TTFT 1-3s)的阶段也能响应 abort。**已开始流后**的检测交给
        调用方在 `async for tok` 循环里看 abort_event(避免 cancel stream 引起的
        资源泄漏)。

    `create()` 调用本身用 _retry 兜底(429 / 5xx / 网络抖动);
    `async for chunk` 阶段一旦开始流,中断了无法恢复——抛给上层。
    """
    if model_spec is not None:
        model = model_spec.model
        reasoning = model_spec.reasoning
        _cli = _client_for_spec(model_spec)
    else:
        model_spec = _legacy_model_spec(lite=lite, reasoning=reasoning)
        model = model_spec.model
        reasoning = model_spec.reasoning
        _cli = _client_for_spec(model_spec)
    debug.log(
        "llm.stream.input",
        f"reasoning={reasoning} model={model} msgs={len(messages)}",
        {"messages": messages},
    )
    _log_prompt_cache_shape(
        label="chat_stream",
        model=model,
        messages=messages,
    )
    create_kwargs: dict[str, Any] = dict(
        model=model, messages=messages, stream=True,
        stream_options={"include_usage": True},  # 2026-05-12 P50
        extra_body=_thinking_extra_body(reasoning, model_spec.provider if model_spec else None),
        timeout=float(settings.llm_stream_first_chunk_timeout_sec or 180.0),
    )

    # ── 2026-05-02 part10 (A3):TTFT racing ──
    # 等 stream 第一个 chunk 是 1-3s 阻塞期。期间 abort 应立即取消请求。
    # 没 abort_event 走老路径(直接 await _retry)。
    async def _cancel_stream_task_bounded(
        task: asyncio.Task | None,
        label: str,
        *,
        timeout: float = 2.0,
    ) -> None:
        if task is None:
            return
        if task.done():
            try:
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            debug.warn(
                f"chat_stream cancel cleanup still pending after {timeout}s: "
                f"{label}; continuing"
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    if abort_event is not None:
        stream_task = asyncio.ensure_future(_retry(
            lambda: _cli.chat.completions.create(**create_kwargs),
            label="chat_stream",
            provider=model_spec.provider if model_spec else None,
            timeout_sec=float(settings.llm_stream_first_chunk_timeout_sec or 180.0),
        ))
        abort_task = asyncio.ensure_future(abort_event.wait())

        try:
            done, _pending = await asyncio.wait(
                [stream_task, abort_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            # 上层 cancel 我们 — 也 cancel 内部 task 防泄漏
            await _cancel_stream_task_bounded(stream_task, "stream_task outer cancel")
            await _cancel_stream_task_bounded(abort_task, "abort_task outer cancel")
            raise
        if abort_task in done and not stream_task.done():
            await _cancel_stream_task_bounded(stream_task, "stream_task before TTFT")
            debug.log("llm.stream.abort_ttft", "aborted before first chunk")
            return  # generator 结束,无 chunk yield
        # stream_task 先到
        if not abort_task.done():
            await _cancel_stream_task_bounded(abort_task, "unused abort_task before stream")
        stream = stream_task.result()
    else:
        stream = await _retry(
            lambda: _cli.chat.completions.create(**create_kwargs),
            label="chat_stream",
            provider=model_spec.provider if model_spec else None,
            timeout_sec=float(settings.llm_stream_first_chunk_timeout_sec or 180.0),
        )

    accumulated: list[str] = []
    stream_usage: Any | None = None
    stream_iter = stream.__aiter__()
    chunk_timeout = float(settings.llm_stream_first_chunk_timeout_sec or 180.0)
    idle_timeout = float(settings.llm_stream_idle_timeout_sec or 300.0)
    while True:
        next_task = asyncio.create_task(stream_iter.__anext__())
        abort_task = (
            asyncio.create_task(abort_event.wait()) if abort_event is not None else None
        )
        waiters = {next_task}
        if abort_task is not None:
            waiters.add(abort_task)
        try:
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=chunk_timeout,
            )
        except BaseException:
            await _cancel_stream_task_bounded(next_task, "next_task exception cleanup")
            await _cancel_stream_task_bounded(abort_task, "abort_task exception cleanup")
            raise

        if abort_task is not None and abort_task in done and not next_task.done():
            await _cancel_stream_task_bounded(next_task, "next_task after abort")
            debug.log("llm.stream.abort", "aborted while waiting for next chunk")
            break

        if abort_task is not None and not abort_task.done():
            await _cancel_stream_task_bounded(abort_task, "unused abort_task")

        if next_task not in done:
            await _cancel_stream_task_bounded(next_task, "next_task chunk timeout")
            debug.warn(
                f"chat_stream chunk timeout after {chunk_timeout:.1f}s "
                f"(received_chars={sum(len(s) for s in accumulated)})"
            )
            if not accumulated:
                raise asyncio.TimeoutError(
                    f"chat_stream first chunk not received within {chunk_timeout:.1f}s"
                )
            break

        try:
            chunk = next_task.result()
        except StopAsyncIteration:
            break
        except Exception:
            raise

        chunk_timeout = idle_timeout
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            stream_usage = chunk_usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            accumulated.append(delta.content)
            yield delta.content

    try:
        await stream.close()
    except Exception:
        pass
    debug.log(
        "llm.stream.output",
        f"finished, total_chars={sum(len(s) for s in accumulated)}",
        "".join(accumulated),
    )
    if stream_usage is not None:
        _record_usage_payload(stream_usage, model=model, tag="chat_stream")


from app.llm.client_tools_loop import chat_with_tools_loop  # noqa: E402,F401





def _maybe_clear_stale_upgrade(
    signal: dict | None,
    successful_after_signal: int,
    *,
    natural_stop: bool = False,
    threshold: int = 8,
    plan_intent: str = "",
) -> None:
    """Bug 2 修:撤销已经过时的升级信号。

    场景:meta_judge 在 iter N 看到模型卡住,写下 should_upgrade=True。
    但模型从 iter N+1 起自我修复,iter N+5 已稳定输出。这种情况下
    升级到更强模型会丢弃所有进展(hard 重跑会把 medium 写好的代码、
    跑出来的实验全部抛弃,见 trace 96071c40 — medium 跑了 7.5 分钟
    建出 CartPole+Q-learning+DQN+report,hard 重跑 33s 改出"我什么都没建"
    的 plan)。

    **B5 修复 (2026-05-02)**: trace 55a58558/55b6a1d7/3da78120 暴露此函数
    清得太狠 — 38 次升级信号撤销 36 次(95%),许多是真正的卡住却被错清。
    根因:
      1. threshold=3 过低: lite 卡住时反复做 read_file 等只读工具就能凑够 3 次 ok
      2. natural_stop=True 一律清: lite 卡住后写"诚实降级"plan 也算 natural_stop
         (intent 含"降级/无法完成"),等于鼓励 lite 自我放弃来逃避升级。
    新策略:
      - threshold 8 (原 3): lite 真正恢复需要更多证据
      - natural_stop 严格化: intent 含"降级/无法完成/工具调用上限/超时"等表
        明放弃语义的不算自然完成,这种情况应该升级。
    """
    if signal is None or not signal.get("should_upgrade"):
        return

    # 强制不清的标志 — 编码任务首次失败等场景设此 force
    if signal.get("force"):
        return

    # B5: natural_stop 严格化 — 降级语义的 plan 不算"自然停手"
    natural_stop_real = natural_stop
    if natural_stop and plan_intent:
        intent_lower = plan_intent.lower()
        give_up_markers = (
            "降级", "无法完成", "未能", "工具调用上限", "调用上限",
            "ran out", "iteration cap", "超时", "撞墙",
            "未完成", "做不完", "没做完", "失败",
        )
        if any(m in intent_lower or m in plan_intent for m in give_up_markers):
            natural_stop_real = False
            debug.log(
                "meta_judge.no_clear_giveup",
                f"natural_stop reframed as False because plan intent indicates give-up: "
                f"{plan_intent[:80]}",
            )

    if successful_after_signal >= threshold or natural_stop_real:
        old_reason = signal.get("reason", "")
        signal["should_upgrade"] = False
        signal["cleared"] = True
        signal["clear_reason"] = (
            f"stale signal cleared: "
            f"successful_after_signal={successful_after_signal}, "
            f"natural_stop={natural_stop_real} (raw={natural_stop}), "
            f"original_reason={old_reason[:80]}"
        )
        debug.log(
            "meta_judge.cleared",
            f"upgrade signal cleared after recovery "
            f"(successful_runs={successful_after_signal}, natural_stop={natural_stop_real})",
            old_reason[:200],
        )


async def _meta_judge_should_upgrade(
    msgs_snapshot: list[dict],
    *,
    current_iter: int,
    current_lite: bool,
    current_reasoning: str,
    signal: dict,
) -> None:
    """旁路：用 lite 模型分析工具调用链，判断主模型是否需要升级。

    fire-and-forget 调用——主流程不等。结果写入 signal dict 供外层在
    循环结束后读取。

    判断标准（让 lite 自己判，不靠"失败次数"启发式）：
    - 是否在原地打转（反复改同一处仍失败）？
    - 是否走入死胡同（思路明显错误但跳不出来）？
    - 还是只是"长但简单"（小语法错误连环、本质能解决）？

    后两种 → should_upgrade=True；最后一种 → False。
    """
    if signal.get("should_upgrade"):
        return  # 已经标记过了

    # 抽取调用链摘要：每轮做了什么 + 是否成功
    timeline = _extract_iteration_timeline(msgs_snapshot)
    if not timeline:
        return  # 没有可分析的内容

    current_level = (
        "medium (lite + thinking=disabled)" if current_lite
        else "hard (main + thinking=disabled)" if current_reasoning == "disabled"
        else "veryhard (main + thinking=max)"
    )
    next_level = (
        "hard (main + thinking=disabled)" if current_lite
        else "veryhard (main + thinking=max)" if current_reasoning == "disabled"
        else None
    )
    if next_level is None:
        return  # 已是顶档

    user_prompt = _meta_judge_user_payload(
        current_level=current_level,
        current_iter=current_iter,
        next_level=next_level,
        timeline=timeline,
        artifact_facts=_extract_recent_artifact_facts_for_meta_judge(msgs_snapshot),
    )

    try:
        raw = await chat_json(
            [
                {"role": "system", "content": META_JUDGE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            reasoning="disabled",
            lite=True,
            metrics_tag="json.meta_judge",
        )
        if not isinstance(raw, dict):
            return
        should = bool(raw.get("should_upgrade", False))
        reason = str(raw.get("reason", ""))[:200]
        debug.log(
            "meta_judge.result",
            f"iter={current_iter} should_upgrade={should} reason={reason}",
        )
        if should and not signal.get("should_upgrade"):
            # 用 setdefault 思维写：已经被设过 True 就别覆盖
            signal["should_upgrade"] = True
            signal["reason"] = reason
            signal["evaluated_at_iter"] = current_iter
    except Exception:
        log.exception("meta_judge failed; signal unchanged")








def _fold_completed_task_window(msgs: list[dict]) -> int:
    """**任务边界折叠**(2026-05-03 加 — 比 _fold_old_tool_messages 更语义化)。

    核心思路:helper / 主线程调 todo_write 把某 todo 标 `completed` 时,
    工作流上等同于"这个子任务的所有 tool call 都已经完成,中间过程对后续
    决策没价值"。这是天然的语义边界,可以把该任务窗口内的 tool result
    全部压成一条 "完成了 [任务描述]" 的占位符。

    实现:
    - 扫所有 tool message,找最近一条 todo_write 的 result 含 `_completed_todos`
    - 把"上次任务边界 → 这次任务边界"之间的所有 tool result content 压缩
    - 触发后在 msg 上打 `_task_boundary_fold` 标记,幂等

    幂等 + 保守:
    - 已含 `_folded` / `_task_boundary_fold` 不再处理
    - 不动 assistant message(API 严格校验 tool_calls 配对)
    - 不动最近 1 轮(模型可能基于刚做的事决策)
    - 不动 todo_write/todo_read 自己(他们是边界标记,要保留可读)
    - 不动 commit_to_main / delegate / spawn_helper(跨任务的关键事件)

    Returns: 压缩的 tool result 数量
    """
    if not msgs or len(msgs) < 6:  # 太少不值得压
        return 0

    # 1. 建 tool_call_id → (tool_name, args) 映射,识别 todo_write 的位置
    pending_calls: dict[str, tuple[str, dict]] = {}
    tool_meta: dict[int, tuple[str, dict]] = {}  # msg_idx → (tool_name, args)
    todo_write_indices: list[int] = []   # 所有 todo_write tool result 的 msg idx
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                if not tc_id or not fn:
                    continue
                fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                fn_args_str = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                try:
                    fn_args = json.loads(fn_args_str or "{}") if isinstance(fn_args_str, str) else (fn_args_str or {})
                except (json.JSONDecodeError, ValueError):
                    fn_args = {}
                if fn_name:
                    pending_calls[tc_id] = (fn_name, fn_args)
        elif m.get("role") == "tool":
            tc_id = m.get("tool_call_id")
            if tc_id and tc_id in pending_calls:
                name, args = pending_calls.pop(tc_id)
                tool_meta[i] = (name, args)
                if name == "todo_write":
                    todo_write_indices.append(i)

    if len(todo_write_indices) < 1:
        return 0

    # 2. 找最近一条带 _completed_todos 的 todo_write
    #    (即"刚才完成了一个 todo,触发本次折叠")
    recent_completion_idx = -1
    completed_summary = ""
    for idx in reversed(todo_write_indices):
        m = msgs[idx]
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if "_completed_todos" not in content:
            continue
        try:
            r = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(r, dict) and r.get("_completed_todos"):
            recent_completion_idx = idx
            # 抓 todo content 作为折叠 summary
            tdos = r.get("_completed_todos") or []
            names = " + ".join(
                str(td.get("content", "?"))[:60]
                for td in tdos[:3]
                if isinstance(td, dict)
            )
            completed_summary = names or "完成 todo"
            break

    if recent_completion_idx < 0:
        return 0

    # 3. 找上一个边界 — 上一条已经被 task_boundary 折叠的 todo_write,或第一条
    prev_boundary_idx = -1
    for idx in todo_write_indices:
        if idx >= recent_completion_idx:
            break
        # 已经做过任务边界折叠的 todo_write 算上一个边界
        if msgs[idx].get("_task_boundary_fold"):
            prev_boundary_idx = idx
    # 没找到上一个边界 → 从对话起点折叠到本次完成点之间的 tool result

    # 4. 安全边界:不动最近 1 轮(模型可能基于刚做的事决策)
    last_assistant_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
            last_assistant_idx = i
            break

    # 5. 不动的工具白名单(跨任务关键信号,保留)
    KEEP_TOOLS = {
        "todo_write",        # 边界标记本身
        "todo_read",         # 跟边界相关
        "commit_to_main",    # 主区固化事件
        "delegate",          # 子任务派发
        "spawn_helper",
        "wait_helper",
        "kill",
        "fork_from",
        "processes",         # 进程管理事件
    }

    def _extract_evidence_tail(tool_name: str, content: str) -> str:
        """Keep compact factual stdout/content when folding a completed task.

        Task-boundary folding must not erase the only copy of command output
        that contains exact numbers or file content. Preserve a short excerpt
        for data-bearing tools while still dropping bulky process details.
        """
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return ""
        if not isinstance(parsed, dict) or parsed.get("ok") is False:
            return ""
        candidates: list[str] = []
        for field in ("stdout", "content", "text", "text_preview", "matches", "source", "symbols"):
            value = parsed.get(field)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
            elif isinstance(value, list) and value:
                lines: list[str] = []
                for item in value[:20]:
                    if isinstance(item, dict):
                        path = item.get("file") or item.get("path") or ""
                        line = item.get("line") or item.get("lineno") or ""
                        text = item.get("text") or item.get("snippet") or item.get("name") or str(item)
                        lines.append(f"{path}:{line} {text}".strip())
                    else:
                        lines.append(str(item))
                if lines:
                    candidates.append("\n".join(lines))
        if not candidates:
            return ""
        # Prefer compact endings for command output because final summary lines
        # usually contain totals; keep beginnings for file reads/searches.
        text = "\n".join(candidates)
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if tool_name in {"env_run", "workspace", "bash", "run"}:
            excerpt = "\n".join(lines[-24:])
        else:
            excerpt = "\n".join(lines[:24])
        return excerpt[:1800]

    folded_count = 0
    fold_range_start = prev_boundary_idx + 1 if prev_boundary_idx >= 0 else 0
    fold_range_end = recent_completion_idx  # 含

    for i in range(fold_range_start, fold_range_end + 1):
        m = msgs[i]
        if m.get("role") != "tool":
            continue
        if m.get("_folded") or m.get("_task_boundary_fold"):
            continue
        if i >= last_assistant_idx:  # 不动最后一轮
            continue
        # 不动 todo_write 自己
        meta = tool_meta.get(i)
        if not meta:
            continue
        tool_name, _args = meta
        if tool_name in KEEP_TOOLS:
            continue
        # 折叠
        content = m.get("content", "")
        if not isinstance(content, str) or len(content) < 80:
            continue
        try:
            r = json.loads(content)
            if isinstance(r, dict):
                ok = r.get("ok", True)
            else:
                ok = True
        except (json.JSONDecodeError, ValueError):
            ok = True
        folded_payload = {
            "ok": ok,
            "_task_boundary_fold": True,
            "_part_of_task": completed_summary[:120],
            "_tool": tool_name,
            "_note": (
                f"中间过程已压缩(任务'{completed_summary[:40]}'已完成)。"
                "如需重新看本次结果,直接重新调对应工具。"
            ),
        }
        evidence_tail = _extract_evidence_tail(tool_name, content)
        if evidence_tail:
            folded_payload["_evidence_excerpt"] = evidence_tail
            folded_payload["_note"] += " 已保留该工具输出中的关键证据摘录。"
        new_content = stable_prompt_json(folded_payload)
        m["content"] = new_content
        m["_task_boundary_fold"] = True
        folded_count += 1

    # 在边界 todo_write 上打标记(供下次折叠定位 prev boundary)
    if folded_count > 0:
        msgs[recent_completion_idx]["_task_boundary_fold"] = True
        debug.log(
            "llm.tools.task_boundary_fold",
            f"folded {folded_count} tool results between idx "
            f"{fold_range_start} and {fold_range_end} "
            f"(task='{completed_summary[:60]}')",
        )

    return folded_count


def _fold_old_tool_messages(
    msgs: list[dict],
    *,
    keep_recent_iters: int = 4,
    force_fold_size: int = 32 * 1024,  # 32KB 单条 → 强制折叠,无论年龄
) -> int:
    """折叠老的 tool 消息内容，原地修改 msgs。

    保留最近 keep_recent_iters 轮（一轮 = assistant tool_call + 对应 tool result）
    的完整内容，更早的 tool message content 被压成 {"ok": True/False, "summary": "..."}。

    **B3 修复 (2026-05-02)**: 在 keep_recent_iters 窗口内的 tool result 也会
    在单条 ≥ force_fold_size 时被强制折叠。这是为了防止极端大单条 tool result
    (如 helper 报告字段单条 2.6MB-5.9MB,实测 trace 55a58558)直接撑爆 LLM context
    导致 BadRequest。原版只看年龄不看大小,4 轮内的巨型字段不会被压。

    assistant 消息和 tool_call_id 配对**不能动**(API 严格校验);
    只折叠 tool message 的 content。

    幂等:已折叠过的消息(含 _folded 标记)不会再被处理。

    2026-05-12 P43: 返回 fold 的 tool message 数量, 方便诊断。
    """
    if not msgs:
        return 0
    _folded_this_call = 0

    # 倒数:找到最近 keep_recent_iters 个 assistant-with-tool_calls 之前的边界
    boundary = len(msgs)
    seen_assistants = 0
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            seen_assistants += 1
            if seen_assistants > keep_recent_iters:
                boundary = i
                break

    for i in range(len(msgs)):
        m = msgs[i]
        if m.get("role") != "tool":
            continue
        if m.get("_folded"):
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue

        # 决定是否折叠这条:
        #   - 在 boundary 之前(老消息)且 > 200 字符 → 折叠
        #   - 单条 ≥ force_fold_size → 强制折叠(B3,无论年龄)
        is_old = i < boundary
        is_huge = len(content) >= force_fold_size
        if not is_old and not is_huge:
            continue
        if not is_huge and len(content) <= 200:
            continue

        # 提炼成功/失败 + 简短摘要
        try:
            r = json.loads(content)
            if isinstance(r, dict):
                ok = r.get("ok")
                # 2026-05-11 优化: delegate result 折叠时保留关键摘要字段
                # helper 完成的 result 含 task_id/terminal_reason/outputs_complete 等
                # 关键信息, 折叠时要保留这些, 让 LLM 后面回看能知道 "之前哪个 helper 完成了什么"
                _is_delegate_result = (
                    "task_id" in r or "terminal_reason" in r or
                    "outputs_check" in r or "delivered_summary" in r or
                    "task_ok" in r or "_task_status" in r or "incomplete_count" in r
                )
                if ok is True:
                    summary = (
                        r.get("path") or r.get("action")
                        or (str(r.get("stdout", ""))[:80].replace("\n", " "))
                        or "ok"
                    )
                    _folded_data = {
                        "ok": True, "_folded": True,
                        "summary": str(summary)[:120],
                    }
                    # delegate result 加保留字段
                    if _is_delegate_result:
                        if "task_ok" in r:
                            _folded_data["task_ok"] = r.get("task_ok")
                        if "_task_status" in r:
                            _folded_data["_task_status"] = r.get("_task_status")
                        if "incomplete_count" in r:
                            _folded_data["incomplete_count"] = r.get("incomplete_count")
                        if "resource_required_count" in r:
                            _folded_data["resource_required_count"] = r.get("resource_required_count")
                        if "_evidence_policy" in r:
                            _folded_data["_evidence_policy"] = r.get("_evidence_policy")
                        if r.get("task_id"):
                            _folded_data["task_id"] = r["task_id"]
                        if r.get("terminal_reason"):
                            _folded_data["terminal_reason"] = r["terminal_reason"]
                        if r.get("elapsed_sec") is not None:
                            _folded_data["elapsed_sec"] = r["elapsed_sec"]
                        _oc = r.get("outputs_check") or {}
                        if "outputs_complete" in _oc:
                            _folded_data["outputs_complete"] = _oc["outputs_complete"]
                            if not _oc["outputs_complete"]:
                                _folded_data["outputs_missing"] = _oc.get(
                                    "outputs_missing", [])[:5]
                        _ds = r.get("delivered_summary") or {}
                        if _ds:
                            _folded_data["delivered"] = {
                                k: len(v) for k, v in _ds.items()
                            }
                    if is_huge:
                        _folded_data["_folded_reason"] = "oversize"
                        _folded_data["_orig_size"] = len(content)
                    new_content = stable_prompt_json(_folded_data)
                elif ok is False:
                    err = str(r.get("error", "?"))[:120]
                    _folded_data = {
                        "ok": False, "_folded": True, "error": err,
                    }
                    # delegate 失败也保留 terminal_reason 等
                    if _is_delegate_result:
                        if "task_ok" in r:
                            _folded_data["task_ok"] = r.get("task_ok")
                        if "_task_status" in r:
                            _folded_data["_task_status"] = r.get("_task_status")
                        if "incomplete_count" in r:
                            _folded_data["incomplete_count"] = r.get("incomplete_count")
                        if "resource_required_count" in r:
                            _folded_data["resource_required_count"] = r.get("resource_required_count")
                        if r.get("task_id"):
                            _folded_data["task_id"] = r["task_id"]
                        if r.get("terminal_reason"):
                            _folded_data["terminal_reason"] = r["terminal_reason"]
                    if is_huge:
                        _folded_data["_folded_reason"] = "oversize"
                        _folded_data["_orig_size"] = len(content)
                    new_content = stable_prompt_json(_folded_data)
                else:
                    new_content = stable_prompt_json({"_folded": True, "preview": content[:120]})
            else:
                new_content = stable_prompt_json({"_folded": True, "preview": content[:120]})
        except (json.JSONDecodeError, ValueError):
            new_content = stable_prompt_json({"_folded": True, "preview": content[:120]})
        m["content"] = new_content
        m["_folded"] = True
        _folded_this_call += 1
        if is_huge and not is_old:
            debug.log(
                "llm.tools.fold.huge",
                f"force-folded oversize tool result at idx {i}: "
                f"{len(content)} bytes → {len(new_content)} bytes "
                f"(within recent window but exceeded {force_fold_size} threshold)",
            )
    return _folded_this_call




def _emergency_compact_msgs(
    msgs: list[dict],
    *,
    target_token_budget: int = 600_000,
) -> int:
    """紧急压缩 messages,在接近 context 上限或 BadRequest 后调用。

    比 _fold_old_tool_messages 更激进:
      1. 先用 force_fold_size=8KB 强制折叠所有大型 tool result
      2. 若仍超 target,把 keep_recent_iters 降到 2 再压
      3. 仍超,把更老的 tool result 整体替换为 {"_folded":true,"preview":...}

    Returns: 估算的 token 数(压缩后)。

    用于 B4 修复: 旧路径在 BadRequest(context length)时直接 fallback,丢光所有进展。
    新路径调本函数压缩后让主循环重试,保住 LLM 已经做的工作。
    """
    # Step 1: 8KB 强制折叠
    _fold_old_tool_messages(msgs, keep_recent_iters=4, force_fold_size=8 * 1024)
    est = _estimate_msgs_token_size(msgs)
    if est <= target_token_budget:
        return est

    # Step 2: 更激进 — keep_recent=2 + 4KB 强制折叠
    _fold_old_tool_messages(msgs, keep_recent_iters=2, force_fold_size=4 * 1024)
    est = _estimate_msgs_token_size(msgs)
    if est <= target_token_budget:
        return est

    # Step 3: 最暴力 — 老 tool messages 全部替换为 1KB preview
    boundary = len(msgs)
    seen_assistants = 0
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            seen_assistants += 1
            if seen_assistants > 2:
                boundary = i
                break
    for i in range(boundary):
        m = msgs[i]
        if m.get("role") != "tool":
            continue
        c = m.get("content", "")
        if isinstance(c, str) and len(c) > 200:
            m["content"] = json.dumps(
                {"_folded": True, "_emergency_truncated": True,
                 "preview": c[:150]},
                ensure_ascii=False,
            )
            m["_folded"] = True

    return _estimate_msgs_token_size(msgs)






# ── 2026-05-02 part8:工具结果时间戳注入 ─────────────────────────








class _ToolProgressState:
    """追踪工具调用历史,检测"剧情节点"——而不是机械地每 N 轮反馈。

    剧情节点判定:
    - **stuck**:连续失败 ≥5 次且持续时间 ≥90s,且距上次反馈 ≥60s
        例:gcc 反复编译失败,模型反复改但仍报错——"啧,真麻烦,这代码怎么过不去"
    - **breakthrough**:之前连续失败 ≥3 次,本次成功
        例:终于编译过了/样例过了——"终于过了,我再用大数据测试看看"
    - **long_silence**:总时长 >3min 且距上次反馈 >2min
        话痨人设可能想插一句,安静人设由 lite 决定不说
        例:静默工作 3 分钟,话痨人设"还在调,你别催哦"

    设计原则:
    - 反馈不及时反而符合人的习惯(没人会每秒报告进度)
    - 顺利时安静工作,不打扰用户
    - 是否真的发出由 progress_cb(lite + 人设)决定,这里只判断"该考虑是否要反馈了"
    """
    # 触发阈值——可根据日志数据调整
    STUCK_FAILS = 5
    STUCK_DURATION_S = 90.0
    STUCK_COOLDOWN_S = 60.0          # stuck 反馈之间最小间隔
    BREAKTHROUGH_PRIOR_FAILS = 3      # 之前至少失败 3 次才算"突破"
    BREAKTHROUGH_COOLDOWN_S = 30.0    # 距上次反馈 30s 内不重复说"过了"
    # #19 修:从 3min 降到 90s,从 2min 间隔改 60s。理由:helper 跑 4-5min 时
    # 用户长时间看不到任何反馈,会觉得 bot 卡死。降阈值后大致每 60-90s 有一次
    # 进度消息,符合用户耐心阈值。是否真的输出仍由人设决定(progress_cb 内部 lite
    # 判断,可能还是选择沉默)。
    LONG_SILENCE_TOTAL_S = 90.0       # 总耗时 >90s 才考虑长静默
    LONG_SILENCE_GAP_S = 60.0         # 距上次反馈 >60s

    def __init__(self, start_time: float):
        self.first_call_at = start_time
        self.last_emit_at = start_time  # 视任务开始为"已经反馈过"——开头静默期
        self.consecutive_fails = 0
        self.first_fail_at: float | None = None
        self.tool_call_count = 0

    def update(self, *, ok: bool, kind: str) -> str | None:
        """更新状态,返回剧情事件名或 None。"""
        now = time.monotonic()
        self.tool_call_count += 1
        elapsed_since_emit = now - self.last_emit_at

        if ok:
            # 突破检测:之前有失败连击,这次过了
            had_fail_streak = self.consecutive_fails >= self.BREAKTHROUGH_PRIOR_FAILS
            self.consecutive_fails = 0
            self.first_fail_at = None
            if had_fail_streak and elapsed_since_emit > self.BREAKTHROUGH_COOLDOWN_S:
                self.last_emit_at = now
                return "breakthrough"
        else:
            # 失败累积
            self.consecutive_fails += 1
            if self.first_fail_at is None:
                self.first_fail_at = now
            # 严重挫折判定
            if (
                self.consecutive_fails >= self.STUCK_FAILS
                and self.first_fail_at is not None
                and (now - self.first_fail_at) > self.STUCK_DURATION_S
                and elapsed_since_emit > self.STUCK_COOLDOWN_S
            ):
                self.last_emit_at = now
                return "stuck"

        # 长时间静默(不论 ok 状态)
        if (
            elapsed_since_emit > self.LONG_SILENCE_GAP_S
            and (now - self.first_call_at) > self.LONG_SILENCE_TOTAL_S
        ):
            self.last_emit_at = now
            return "long_silence"

        return None


# ── 旧名保留为同义（供尚未迁移的调用方） ──
async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    reasoning: str = "high",
    model_spec=None,
) -> Any:
    """单次工具调用响应（不循环）。一般情况下应使用 chat_with_tools_loop。"""
    if model_spec is not None:
        _model = model_spec.model
        _reasoning = model_spec.reasoning
        _cli = _client_for_spec(model_spec)
    else:
        model_spec = _legacy_model_spec(lite=False, reasoning=reasoning)
        _model = model_spec.model
        _reasoning = model_spec.reasoning
        _cli = _client_for_spec(model_spec)
    _log_prompt_cache_shape(
        label="chat_with_tools",
        model=_model,
        messages=messages,
        tools=tools,
    )
    resp = await _retry(
        lambda: _cli.chat.completions.create(
            model=_model,
            messages=messages,
            tools=tools,
            stream=False,
            extra_body=_thinking_extra_body(_reasoning, model_spec.provider if model_spec else None),
        ),
        label="chat_with_tools",
        provider=model_spec.provider if model_spec else None,
    )
    _record_response_usage(resp, model=_model, tag="chat_with_tools")
    return resp.choices[0].message


def _try_extract_json_locally(content: str) -> str | None:
    """从含前缀文字 / markdown 包裹的内容中提取干净 JSON,避免每次都调 LLM cleanup。

    2026-05-02 加(Bug G 修):实测一个任务触发 284 次 finalize.cleanup,每次额外 1
    次 LLM 调用 5-30s。改成两步:本地先尝试,失败再降级 LLM。

    支持的格式(按尝试顺序):
      1. ```json ... ``` / ``` ... ``` 代码块包裹 JSON
      2. 前缀文字 + 裸 JSON(找首个 { 到匹配的 },处理引号/转义)
      3. 全文就是 JSON(顶层括号匹配)

    Returns:
        提取成功 = 干净 JSON 字符串(已通过 json.loads 验证); 失败 = None
    """
    if not content:
        return None
    s = content.strip()

    # ── 案例 1: ```json ... ``` 代码块 ──
    import re
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s)
    if m:
        candidate = m.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass  # 块内 JSON 有问题,继续尝试下一种

    # ── 案例 2: 找首个 { 到匹配的 }(带引号/转义处理) ──
    first_brace = s.find("{")
    if first_brace < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    end_idx = -1
    for i in range(first_brace, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        # 不在字符串里
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx < 0:
        return None
    candidate = s[first_brace:end_idx + 1]
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        return None
