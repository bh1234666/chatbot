"""
进程内累积 LLM 使用指标 — 服务 prefix-cache 命中率 / token 用量监控。

不依赖任何外部 metric 系统,通过 `/metrics` 端点暴露 Prometheus 文本格式。
进程重启清零(单 worker 部署可接受;多 worker 各自暴露,scrape 端聚合)。

设计取舍:
  - 用 threading.Lock 不用 asyncio.Lock —— 计数器更新是亚微秒级,
    任何 caller(同步 / 异步)都能直接调,不需要 await,不会阻塞事件循环。
  - 用 (tag, model) 做 key —— 通过 tag 区分主线程(main) / 各 helper(helper.<id>) /
    专项 lite(round1, voice_decide 等),通过 model 区分主/lite 模型成本。

调用方:
  - `app/llm/client.py` 在 stream 结束时记录上游 usage cache hit/miss。
  - LLM 请求发出前记录本地 prompt shape 估算，便于和真实命中率对照。
"""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()

# key 形如 ("main", "deepseek-v4-pro") / ("helper.<task_id>", "deepseek-v4-flash")
_prompt_tokens: dict[tuple[str, str], int] = defaultdict(int)
_completion_tokens: dict[tuple[str, str], int] = defaultdict(int)
_cache_hit_tokens: dict[tuple[str, str], int] = defaultdict(int)
_cache_miss_tokens: dict[tuple[str, str], int] = defaultdict(int)
_call_count: dict[tuple[str, str], int] = defaultdict(int)

# key 形如 ("tools_loop.iter1.main", "deepseek-v4-pro")
_prompt_shape_static_bytes: dict[tuple[str, str], int] = defaultdict(int)
_prompt_shape_dynamic_bytes: dict[tuple[str, str], int] = defaultdict(int)
_prompt_shape_cacheable_prefix_bytes: dict[tuple[str, str], int] = defaultdict(int)
_prompt_shape_call_count: dict[tuple[str, str], int] = defaultdict(int)


def record_llm_usage(
    *,
    tag: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit: int,
    cache_miss: int,
) -> None:
    """累计一次 LLM 调用的 usage 数据。永不抛异常。

    注:caller 已经在 try/except 里包了这个函数,但内部仍用宽松转型避免
    奇怪的 usage payload 把计数搞乱。
    """
    try:
        key = (str(tag or "unknown"), str(model or "unknown"))
        with _lock:
            _prompt_tokens[key] += int(prompt_tokens or 0)
            _completion_tokens[key] += int(completion_tokens or 0)
            _cache_hit_tokens[key] += int(cache_hit or 0)
            _cache_miss_tokens[key] += int(cache_miss or 0)
            _call_count[key] += 1
    except Exception:
        # metric 永远不影响主流程
        pass


def record_prompt_shape(
    *,
    label: str,
    model: str,
    prompt_static_bytes: int,
    prompt_dynamic_bytes: int,
    cacheable_prefix_bytes: int,
) -> None:
    """Accumulate local prompt-shape estimates for cache diagnostics.

    This is separate from provider usage. It records what the local prompt
    builder believes is stable/dynamic before the request is sent.

    本地 prompt 形状估算，用于和上游真实 cache hit/miss 对照。
    """
    try:
        key = (str(label or "unknown"), str(model or "unknown"))
        with _lock:
            _prompt_shape_static_bytes[key] += int(prompt_static_bytes or 0)
            _prompt_shape_dynamic_bytes[key] += int(prompt_dynamic_bytes or 0)
            _prompt_shape_cacheable_prefix_bytes[key] += int(cacheable_prefix_bytes or 0)
            _prompt_shape_call_count[key] += 1
    except Exception:
        pass


# Prometheus label-value 转义:反斜杠 / 双引号 / 换行需要 escape
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _emit_counter(
    lines: list[str], *, name: str, help_text: str, data: dict[tuple[str, str], int]
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    # 排序保证输出稳定,便于 diff
    for (tag, model), v in sorted(data.items()):
        lines.append(f'{name}{{tag="{_esc(tag)}",model="{_esc(model)}"}} {v}')


def render_prometheus() -> str:
    """渲染 Prometheus exposition format(text/plain; version=0.0.4)。"""
    lines: list[str] = []
    with _lock:
        # 复制后释放锁,渲染本身不需要锁住计数器
        snap_prompt = dict(_prompt_tokens)
        snap_completion = dict(_completion_tokens)
        snap_hit = dict(_cache_hit_tokens)
        snap_miss = dict(_cache_miss_tokens)
        snap_calls = dict(_call_count)
        snap_shape_static = dict(_prompt_shape_static_bytes)
        snap_shape_dynamic = dict(_prompt_shape_dynamic_bytes)
        snap_shape_prefix = dict(_prompt_shape_cacheable_prefix_bytes)
        snap_shape_calls = dict(_prompt_shape_call_count)

    _emit_counter(
        lines, name="llm_prompt_tokens_total",
        help_text="Total prompt tokens sent to LLM",
        data=snap_prompt,
    )
    _emit_counter(
        lines, name="llm_completion_tokens_total",
        help_text="Total completion tokens received from LLM",
        data=snap_completion,
    )
    _emit_counter(
        lines, name="llm_cache_hit_tokens_total",
        help_text="Prompt tokens served from prefix cache",
        data=snap_hit,
    )
    _emit_counter(
        lines, name="llm_cache_miss_tokens_total",
        help_text="Prompt tokens that missed prefix cache",
        data=snap_miss,
    )
    _emit_counter(
        lines, name="llm_call_total",
        help_text="Number of completed LLM streaming calls",
        data=snap_calls,
    )
    _emit_counter(
        lines, name="llm_prompt_shape_static_bytes_total",
        help_text="Total local estimated stable prompt bytes by call shape",
        data=snap_shape_static,
    )
    _emit_counter(
        lines, name="llm_prompt_shape_dynamic_bytes_total",
        help_text="Total local estimated dynamic prompt bytes by call shape",
        data=snap_shape_dynamic,
    )
    _emit_counter(
        lines, name="llm_prompt_shape_cacheable_prefix_bytes_total",
        help_text="Total local estimated cacheable prefix bytes by call shape",
        data=snap_shape_prefix,
    )
    _emit_counter(
        lines, name="llm_prompt_shape_call_total",
        help_text="Number of local prompt-shape observations",
        data=snap_shape_calls,
    )
    return "\n".join(lines) + "\n"


def snapshot() -> dict[str, dict[str, int]]:
    """单元测试 / 内部调试用 — 返回当前累计值的浅拷贝。"""
    with _lock:
        return {
            "prompt_tokens": {f"{t}|{m}": v for (t, m), v in _prompt_tokens.items()},
            "completion_tokens": {f"{t}|{m}": v for (t, m), v in _completion_tokens.items()},
            "cache_hit_tokens": {f"{t}|{m}": v for (t, m), v in _cache_hit_tokens.items()},
            "cache_miss_tokens": {f"{t}|{m}": v for (t, m), v in _cache_miss_tokens.items()},
            "calls": {f"{t}|{m}": v for (t, m), v in _call_count.items()},
            "prompt_shape_static_bytes": {f"{t}|{m}": v for (t, m), v in _prompt_shape_static_bytes.items()},
            "prompt_shape_dynamic_bytes": {f"{t}|{m}": v for (t, m), v in _prompt_shape_dynamic_bytes.items()},
            "prompt_shape_cacheable_prefix_bytes": {f"{t}|{m}": v for (t, m), v in _prompt_shape_cacheable_prefix_bytes.items()},
            "prompt_shape_calls": {f"{t}|{m}": v for (t, m), v in _prompt_shape_call_count.items()},
        }


def reset() -> None:
    """单元测试用,生产无调用方。"""
    with _lock:
        _prompt_tokens.clear()
        _completion_tokens.clear()
        _cache_hit_tokens.clear()
        _cache_miss_tokens.clear()
        _call_count.clear()
        _prompt_shape_static_bytes.clear()
        _prompt_shape_dynamic_bytes.clear()
        _prompt_shape_cacheable_prefix_bytes.clear()
        _prompt_shape_call_count.clear()
