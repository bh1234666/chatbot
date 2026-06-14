"""LLM 消息处理与格式化工具:token 估算、时间戳注入、工具历史/结果摘要、思考开关、
续写前缀、assistant 消息序列化、迭代时间线抽取、冗余工具结果软压缩等。

2026-05-20 重构: 从 llm/client.py 原样抽出。经 extract_analysis --closure 验证自包含
(13 函数, 0 unsafe),仅依赖 stdlib(json/logging/typing)。模块自建 logger。
client.py 通过 re-export 保持兼容,调用点零改动。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.llm.json_utils import stable_prompt_json

log = logging.getLogger(__name__)


def _first_present(mapping: dict, *keys: str, default=None):
    for key in keys:
        if key in mapping:
            value = mapping.get(key)
            if value is not None:
                return value
    return default


def _sanitize_delegate_fold_text(value) -> str:
    text = str(value or "")
    replacements = (
        ("helpers_initially_spawned", "processing_records_started"),
        ("helpers_requested", "processing_records_requested"),
        ("helpers_completed", "results_returned"),
        ("helpers_returned", "results_returned"),
        ("helpers_still_running", "processing_records_running"),
        ("helpers_unavailable", "processing_records_unavailable"),
        ("helpers_forked_during_run", "processing_records_started_during_run"),
        ("background_work_started", "processing_records_started"),
        ("background_work_requested", "processing_records_requested"),
        ("background_work_running", "processing_records_running"),
        ("background_work_unavailable", "processing_records_unavailable"),
        ("background_work_started_during_run", "processing_records_started_during_run"),
        ("helper_resource_required", "processing_record_resource_required"),
        ("background_work_resource_required", "processing_record_resource_required"),
        ("helper_runaway_requires_intervention", "processing_record_runaway_requires_intervention"),
        ("background_work_runaway_requires_intervention", "processing_record_runaway_requires_intervention"),
        ("helper_still_running_prompt_dropped", "processing_record_still_running_prompt_dropped"),
        ("background_work_still_running_prompt_dropped", "processing_record_still_running_prompt_dropped"),
        ("helper_producer_self_verified", "output_self_verified"),
        ("producer_self_verified", "output_self_verified"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"\bdelegation\b", "processing step", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdelegated\b", "routed", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdelegating\b", "routing", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdelegate\b", "route", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)[-_ ]owned\b", "generated", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)\s+reports\b", "available evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)\s+report\b", "available evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\s+evidence\b", "evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbackground_work\b", "processing_records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbackground\s+(?:tasks?|work|producers?|branches?)\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\b", "processing record", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelpers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelper\b", "processing record", text, flags=re.IGNORECASE)
    return text


def _is_thinking_enabled(extra_body: dict) -> bool:
    """从 extra_body 判断当前调用是 thinking 还是 thinking_disabled。

    DeepSeek 两种格式兼容:
      - {"thinking": {"type": "disabled"}}     → False
      - {"thinking": {"type": "enabled"}, ...} → True
      - 没有 thinking 字段 → 默认 True(API 默认 enabled)
    """
    if not isinstance(extra_body, dict):
        return True
    th = extra_body.get("thinking")
    if not isinstance(th, dict):
        return True
    return th.get("type", "enabled") != "disabled"


def _build_continuation_prefix_message(collector: _StreamCollector) -> Optional[dict]:
    """从断流的 collector 构造一条 assistant prefix message,用于续写。

    用户提的"直接拼接"方案:把已收到的 partial content / tool_call arguments
    作为 assistant 的"已经写了的开头",让模型从这里续上。

    v19.1:thinking 路径下额外保留 reasoning_content 摘要——让续写模型看到
      "我刚才在想什么",避免重头起 reasoning chain(这正是 think 路径超时的根因)。
      reasoning_content 不能直接以 reasoning_content 字段回传(API 会拒不属于
      原推理段的字段),所以拼到 content 里作为提示文本,模型能读但不算它的推理。

    DeepSeek 限制:
      - /beta 端点的 prefix=True 字段只在 tools 为空时生效(实测 trace 9b3a6a6)
      - 有 tools 时把 partial 当**普通** assistant message 加进去也能起到引导作用
        (模型看到自己上一轮已经"说了一半",自然倾向接下来续完)

    返回 None 表示没有可用的 partial(content + reasoning + tool_calls 都空,不该续)。
    """
    has_content = bool(collector.content)
    has_reasoning = bool(collector.reasoning_content)
    has_tool_calls = bool(collector.tool_calls)

    if not (has_content or has_reasoning or has_tool_calls):
        return None

    parts: list[str] = []

    # v19.1:reasoning_content 摘要拼到最前面(作为提示,不当作真 reasoning)
    # 截断防 prompt 爆炸:think 链 5000+ token 太长,只保留尾部 1500 字符即可
    # (尾部最贴近超时点,最有价值)
    if has_reasoning:
        rc = collector.reasoning_content
        rc_tail = rc[-1500:] if len(rc) > 1500 else rc
        rc_note = (
            f"[上一轮思考被打断的尾段(供续写参考,无需重新思考完整链路):\n"
            f"{rc_tail}\n]"
        )
        parts.append(rc_note)

    if has_content:
        # 已经吐出来的 content 直接作为续写起点
        parts.append(collector.content)

    if has_tool_calls:
        # tool_calls partial 一般 JSON 不完整,转成"我刚开始调 X 工具,参数已写到 ..."
        # 让模型看到自己的上下文,自然接续
        for idx in sorted(collector.tool_calls.keys()):
            tc = collector.tool_calls[idx]
            fn = tc["function"]
            tcs_summary = (
                f"\n[上次输出被中断,我已开始调用工具 `{fn['name']}`,"
                f"参数已写到:\n```\n{fn['arguments']}\n```\n"
                f"现在我从中断处继续完成同一工具调用(整合上面已写参数+剩余,"
                f"重新发出完整 tool_call)。]"
            )
            parts.append(tcs_summary)

    if not parts:
        return None

    return {
        "role": "assistant",
        "content": "".join(parts),
    }


# ── thinking 配置辅助 ────────────────────────────────────────
def _thinking_extra_body(reasoning: str, provider=None) -> dict[str, Any] | None:
    """根据 reasoning 档位构造 thinking 相关的 extra_body 字段。

    reasoning:
      - "disabled" → 关闭思考（最快）
      - "high" → 默认思考强度
      - "max" → 最强思考（agentic 任务用，速度更慢但规划质量更高）
      - "low" / "medium" → 官方文档明确说会被映射为 "high"，传了无意义

    provider=None 时保留 DeepSeek 行为（向后兼容）。
    非 DeepSeek provider（GPT 等）返回 None——这些 API 不支持 thinking 自定义体。
    """
    if provider is not None and provider.name != "deepseek":
        return None
    if reasoning == "disabled":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "reasoning_effort": reasoning}


def _serialize_assistant_message(msg: Any) -> dict[str, Any]:
    """把 SDK 返回的 assistant message 序列化为可回传给 API 的 dict。

    官方文档要求工具调用轮次必须完整回传 reasoning_content（即使为空），
    否则后续请求 400。优先用 SDK 的 model_dump（保留所有字段，包括 SDK
    可能新增的扩展字段），失败时降级为手工重组并显式补 reasoning_content。
    """
    # 路径 A：Pydantic 模型的标准序列化——最可靠，保留所有字段
    if hasattr(msg, "model_dump"):
        try:
            d = msg.model_dump(exclude_none=False, exclude_unset=False)
            # 确保 reasoning_content 存在（None 时也用空字符串占位以满足 API 要求）
            if "reasoning_content" not in d or d.get("reasoning_content") is None:
                rc = getattr(msg, "reasoning_content", None)
                if rc is None:
                    model_extra = getattr(msg, "model_extra", None) or {}
                    rc = model_extra.get("reasoning_content")
                d["reasoning_content"] = rc or ""
            # 2026-05-08 Fix(Bug 4): 空 / None / 非 list 的 tool_calls 都会触发 API 400
            # "Invalid 'messages[N].tool_calls': empty array. Expected an array
            # with minimum length 1, but got an empty array instead."
            # 旧版只处理 list==[], 加固为 None / 非 list 也一并删除字段。
            _tc = d.get("tool_calls")
            if _tc is None or not isinstance(_tc, list) or len(_tc) == 0:
                d.pop("tool_calls", None)
            return d
        except Exception:
            log.exception("model_dump failed; fallback to manual reconstruction")

    # 路径 B：手工重组（兜底）
    tool_calls_raw = getattr(msg, "tool_calls", None) or []
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": getattr(msg, "content", "") or "",
    }
    # 2026-05-08 Fix: 只在有工具调用时才加 tool_calls 字段，
    # 空数组会导致 API 400
    if tool_calls_raw:
        entry["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls_raw
        ]
    rc = getattr(msg, "reasoning_content", None)
    if rc is None:
        model_extra = getattr(msg, "model_extra", None) or {}
        rc = model_extra.get("reasoning_content")
    # 即使为空也要带，避免触发 API 400
    entry["reasoning_content"] = rc or ""
    return entry


def _summarize_tool_history(msgs: list[dict]) -> str:
    """把工具调用历史压成纯文本，给 lite 模型重写 JSON 用。
    保留：用户原始请求 + 每轮工具名+成功/失败+关键产出。
    """
    lines: list[str] = []
    # 抽取用户原始消息
    for m in msgs:
        if m.get("role") == "user":
            content = str(m.get("content", ""))[:600]
            lines.append(f"[用户请求]\n{content}\n")
            break

    lines.append("[工具调用历史]")
    iter_idx = 0
    for m in msgs:
        role = m.get("role", "")
        if role == "assistant":
            tool_calls = m.get("tool_calls", []) or []
            for tc in tool_calls:
                iter_idx += 1
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "?")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                # 提炼几个关键字段
                hint = ""
                if name == "workspace":
                    action = args.get("action", "")
                    if action == "write":
                        hint = f"write {args.get('path','?')}"
                    elif action == "run":
                        hint = f"run {str(args.get('command',''))[:80]}"
                    else:
                        hint = action
                elif name == "python":
                    code = str(args.get("code", ""))[:60].replace("\n", " ")
                    hint = f"python: {code}"
                else:
                    hint = name
                lines.append(f"  iter{iter_idx}: {hint}")
        elif role == "tool":
            content = str(m.get("content", ""))
            try:
                r = json.loads(content)
                if isinstance(r, dict):
                    if r.get("ok") is True:
                        # 提炼成功产出
                        if "stdout" in r:
                            out = str(r.get("stdout", ""))[:120].replace("\n", " ")
                            lines.append(f"    → ok stdout: {out}")
                        elif "path" in r:
                            lines.append(f"    → ok wrote: {r['path']}")
                        else:
                            lines.append(f"    → ok")
                    elif r.get("ok") is False:
                        err = str(r.get("error", "?"))[:100]
                        lines.append(f"    → FAIL: {err}")
            except Exception:
                lines.append(f"    → (raw: {content[:80]})")

    return "\n".join(lines)[:6000]  # cap to 6KB


def _extract_iteration_timeline(msgs: list[dict]) -> str:
    """从 msgs 抽取每轮工具调用的简短摘要，返回时间线文本。
    用于 meta_judge 评估。只保留最近 8 轮（一轮 = 一次 assistant tool_calls）。
    """
    # 先定位最近 8 个 assistant-with-tool_calls 的起始位置
    assistant_indices = [
        i for i, m in enumerate(msgs)
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    start_from = assistant_indices[-8] if len(assistant_indices) > 8 else 0

    lines: list[str] = []
    iter_idx = 0
    pending_tool_calls: list[tuple[str, dict]] = []

    for m in msgs[start_from:]:
        role = m.get("role", "")
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "?")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                pending_tool_calls.append((name, args))
        elif role == "tool":
            content = str(m.get("content", ""))
            ok, summary = _tool_result_signal(content)
            if pending_tool_calls:
                name, args = pending_tool_calls.pop(0)
                iter_idx += 1
                action = _short_action_desc(name, args)
                status = "✓" if ok else "✗"
                lines.append(f"  {iter_idx:2d}. {action} {status} {summary}")

    return "\n".join(lines)


def _tool_result_signal(content: str) -> tuple[bool, str]:
    """从 tool result 抽出 ok 标志 + 短摘要。

    兼容三种格式:
    1. 原始 ok=True 结果: 读 stdout / path
    2. 原始 ok=False 结果: 读 error
    3. 折叠后的 _folded 结果(_fold_old_tool_messages 加的): 读 summary / error

    2026-05-02 part14:优先抽 test_summary 字段(workspace.run 加的 PASS/FAIL 摘要),
    比 stdout 截断前 60 字更有用 — 让 meta_judge 看到"测试一直 fail" 模式。
    """
    try:
        r = json.loads(content)
        if isinstance(r, dict):
            ok = r.get("ok") is True
            if r.get("task_ok") is False or r.get("incomplete_count") or r.get("resource_required_count"):
                bits = []
                if r.get("incomplete_count"):
                    bits.append(f"incomplete={r.get('incomplete_count')}")
                if r.get("resource_required_count"):
                    bits.append(f"resource_required={r.get('resource_required_count')}")
                if r.get("failed_count"):
                    bits.append(f"failed={r.get('failed_count')}")
                return False, ("processing blocked " + ", ".join(bits))[:80]
            # part14 加:test_summary 优先(它已经是 PASS/FAIL 提炼)
            test_summary = r.get("test_summary")
            if test_summary:
                return ok, str(test_summary)[:80].replace("\n", " ")
            if ok:
                # 折叠后会有 summary 字段,优先读
                summary = r.get("summary") or r.get("path") or r.get("stdout", "")
                return True, str(summary)[:60].replace("\n", " ")
            else:
                err = r.get("error", "")
                return False, str(err)[:60].replace("\n", " ")
    except Exception:
        pass
    return False, content[:40]


def _short_action_desc(name: str, args: dict) -> str:
    """Tool 调用的极简描述,给 meta_judge 看。"""
    if name == "python":
        code = str(args.get("code", ""))[:50].replace("\n", " ")
        return f"py[{code}]"
    if name == "workspace":
        action = args.get("action", "")
        if action == "write":
            return f"write {args.get('path', '?')}"
        if action == "run":
            return f"run {str(args.get('command', ''))[:60]}"
        return f"ws.{action}"
    if name == "delegate":
        n = len(args.get("tasks", []))
        return f"processing_records({n} tasks)"
    return name


def _estimate_msgs_token_size(msgs: list[dict]) -> int:
    """粗略估算 messages 总 token 数。

    经验比例: 中文 ~0.5 token/字, 英文 ~0.25 token/字。混合内容用 0.4 兜底。
    精度无所谓 — 只用于"是否接近 context 上限"的预警判断。
    """
    total_chars = 0
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
        # tool_calls 里也有内容
        for tc in (m.get("tool_calls") or []):
            args = (tc.get("function") or {}).get("arguments", "")
            if isinstance(args, str):
                total_chars += len(args)
    return int(total_chars * 0.4)


def _soft_compact_redundant_tool_results(msgs: list[dict]) -> int:
    """软压缩:针对**特定工具**的语义级冗余折叠。

    2026-05-02 part10 (A1):比 _fold_old_tool_messages 更细粒度——按工具名
    检测明显的冗余模式,折叠旧的保留新的。
    场景:
      - **read_file 同文件多次读**:旧的 result 被新的覆盖范围(start_line/end_line)包含 → 旧的折叠
      - **workspace.run 同命令重复**:同一 command 重复跑 ≥3 次 → 最早的 N-2 条折叠
      - **delegate 完成结果**(已 forced finalize 拿过 plan)→ 折叠成简短摘要

    幂等:已含 _folded / _superseded 字段的不再处理。
    返回:压缩后估算的 token 数。

    设计取舍:
      - **保守**:只折叠"明显冗余"的(同文件同范围、同命令、已抽过 excerpt 的)
      - **保留语义信息**:折叠后保留 `_summary` 字段告诉模型曾经做过什么
      - **不动最近 1 轮**:模型可能基于上一轮 tool result 决定下一步
    """
    if not msgs or len(msgs) < 4:  # 太少不值得压
        return _estimate_msgs_token_size(msgs)

    # 1. 收集所有 tool message + 对应的 assistant tool_call(找出工具名 + args)
    # tool message 的 tool_call_id 关联到上一条 assistant 的 tool_calls 数组
    # 我们要按工具名分组,所以先建 (idx → tool_name, args) 映射
    tool_meta: dict[int, tuple[str, dict]] = {}
    pending_calls: dict[str, tuple[str, dict]] = {}  # tool_call_id → (name, args)
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
                tool_meta[i] = pending_calls.pop(tc_id)

    # 边界:只压最近 1 轮之前的(留最新一轮的完整 tool result 给模型决策下一步)
    last_assistant_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
            last_assistant_idx = i
            break

    # 2. 按工具名收集 tool result 索引
    by_tool: dict[str, list[int]] = {}  # tool_name → [msg_idx, ...]
    for idx, (name, _args) in tool_meta.items():
        if idx >= last_assistant_idx:  # 跳过最后一轮
            continue
        if msgs[idx].get("_folded") or msgs[idx].get("_superseded"):
            continue
        by_tool.setdefault(name, []).append(idx)

    folded = 0

    # 3. read_file 冗余检测:同一 path 多次读,旧的覆盖范围被新的包含 → 旧的折叠
    rf_indices = by_tool.get("read_file", [])
    if len(rf_indices) >= 2:
        # 按 path 分组
        path_reads: dict[str, list[int]] = {}
        for i in rf_indices:
            args = tool_meta[i][1]
            path = str(args.get("path", "")).strip()
            if path:
                path_reads.setdefault(path, []).append(i)

        for path, read_idxs in path_reads.items():
            if len(read_idxs) < 2:
                continue
            # 算每次读的范围(start_line, end_line)
            ranges = []
            for i in read_idxs:
                args = tool_meta[i][1]
                s = int(args.get("start_line", 1)) if args.get("start_line") else 1
                e = args.get("end_line", -1)
                try:
                    e = int(e) if e != -1 else 999_999
                except (ValueError, TypeError):
                    e = 999_999
                ranges.append((i, s, e))

            # 老 read 范围若被新 read 完全包含 → 老的折叠为 superseded 占位
            for i_old, s_old, e_old in ranges[:-1]:
                for i_new, s_new, e_new in ranges:
                    if i_new <= i_old:
                        continue
                    if s_new <= s_old and e_new >= e_old:
                        # i_old 被 i_new 完全覆盖
                        old_msg = msgs[i_old]
                        if old_msg.get("_folded") or old_msg.get("_superseded"):
                            break
                        try:
                            old_msg["content"] = stable_prompt_json({
                                "_superseded": True,
                                "_folded": True,
                                "summary": f"read_file {path} [L{s_old}-{e_old}]"
                                           f" — 已被后续 [L{s_new}-{e_new}] 包含",
                            })
                            old_msg["_folded"] = True
                            old_msg["_superseded"] = True
                            folded += 1
                        except Exception:
                            pass
                        break  # 这条 old 只压一次

    # 4. workspace.run / bash 同命令重复:同 command 跑 ≥3 次,只保留最近 2 个完整,前面的折叠
    # 2026-05-03 加 bash:bash 工具走同样的 command 字段,行为一致
    run_indices = by_tool.get("workspace", []) + by_tool.get("run", []) + by_tool.get("bash", [])
    if run_indices:
        cmd_runs: dict[str, list[int]] = {}
        for i in run_indices:
            args = tool_meta[i][1]
            # workspace tool 的 command 字段(action="run" 时)
            action = str(args.get("action", "")).strip()
            cmd = str(args.get("command", "")).strip()
            if action == "run" or (cmd and not action):
                if cmd:
                    cmd_runs.setdefault(cmd, []).append(i)

        for cmd, idxs in cmd_runs.items():
            if len(idxs) < 3:
                continue
            # 折叠最早的 idxs[:-2],保留最近 2 个
            for i_old in idxs[:-2]:
                old_msg = msgs[i_old]
                if old_msg.get("_folded"):
                    continue
                try:
                    # 抽 returncode 和 stdout 末尾几个字符做 summary
                    parsed = json.loads(old_msg.get("content") or "{}")
                    rc = parsed.get("returncode") if isinstance(parsed, dict) else None
                    stdout_tail = ""
                    if isinstance(parsed, dict):
                        so = parsed.get("stdout") or ""
                        if isinstance(so, str) and so:
                            stdout_tail = so.strip().split("\n")[-1][:80]
                    cmd_short = cmd[:80] + ("..." if len(cmd) > 80 else "")
                    old_msg["content"] = stable_prompt_json({
                        "_folded": True,
                        "_redundant": "same_command_repeated",
                        "summary": f"重复跑 `{cmd_short}` (returncode={rc})",
                        "stdout_tail": stdout_tail,
                    })
                    old_msg["_folded"] = True
                    folded += 1
                except Exception:
                    pass

    # 5. delegate 已用过的结果折叠:helper_excerpts 已在主线程闭包提取过,这里
    # 也可以折叠老 delegate result(orchestrator P5 链路保证 excerpt 已抽出来)。
    # 保守:只压 ≥10KB 的 delegate 结果,且不是最近 1 个 delegate 调用。
    all_delegate_indices = [
        idx for idx, (name, _args) in sorted(tool_meta.items())
        if name == "delegate"
    ]
    if len(all_delegate_indices) >= 2:
        latest_delegate_idx = all_delegate_indices[-1]
        for i_old in all_delegate_indices:
            if i_old == latest_delegate_idx or i_old >= last_assistant_idx:
                continue
            old_msg = msgs[i_old]
            if old_msg.get("_folded"):
                continue
            content = old_msg.get("content", "")
            if not isinstance(content, str) or len(content) < 10_000:
                continue
            try:
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    continue
                # 抽简短统计；helpers_completed 表示返回结果数，不等于成功完成数。
                results_returned = _first_present(parsed, "results_returned", "helpers_completed", default="?")
                results_returned = _first_present(parsed, "results_returned", "helpers_completed", default="?")
                success_count = parsed.get("success_count", 0)
                background_running = _first_present(
                    parsed,
                    "processing_records_running",
                    "background_work_running",
                    "helpers_still_running",
                    default=0,
                )
                task_ok = parsed.get("task_ok")
                incomplete_count = parsed.get("incomplete_count")
                resource_required_count = parsed.get("resource_required_count")
                task_status = parsed.get("_task_status")
                any_stuck = parsed.get("any_stuck", False)
                old_msg["content"] = stable_prompt_json({
                    "_folded": True,
                    "_redundant": "processing_record_excerpts_already_extracted",
                    "summary": _sanitize_delegate_fold_text(
                        f"processing records returned_results={results_returned}, success_count={success_count}, "
                        f"still_running={background_running}, any_stuck={any_stuck}; "
                        f"task_ok={task_ok}, incomplete={incomplete_count}, "
                        f"resource_required={resource_required_count}; "
                        "详细后台工作报告已被主线程抽 excerpt 给 round3"
                    ),
                    "task_ok": task_ok,
                    "success_count": success_count,
                    "results_returned": results_returned,
                    "processing_records_running": background_running,
                    "incomplete_count": incomplete_count,
                    "resource_required_count": resource_required_count,
                    "_task_status": task_status,
                    "_evidence_policy": _sanitize_delegate_fold_text(parsed.get("_evidence_policy")),
                })
                old_msg["_folded"] = True
                folded += 1
            except Exception:
                pass

    # 6. bash / workspace.run 长 stdout 折叠(2026-05-03 加):
    # 即使命令不重复,大块 grep / find / 编译输出经常 ≥20KB,模型用过一次就不再需要原文。
    # 老 bash result 只要不是最近一条,且 content ≥20KB → 折叠到 returncode + stdout 末尾几行。
    # 跟 #4 不冲突:#4 折叠重复命令的老条,#6 折叠所有大输出的老条(覆盖范围更广)。
    bash_run_indices = (
        by_tool.get("workspace", []) + by_tool.get("run", []) + by_tool.get("bash", [])
    )
    if len(bash_run_indices) >= 2:
        for i_old in bash_run_indices[:-1]:  # 除最近的
            old_msg = msgs[i_old]
            if old_msg.get("_folded"):
                continue
            content = old_msg.get("content", "")
            if not isinstance(content, str) or len(content) < 20_000:
                continue
            try:
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    continue
                rc = parsed.get("returncode")
                stdout = parsed.get("stdout") or ""
                stderr = parsed.get("stderr") or ""
                # 保留 stderr 前 500 字 + stdout 末 500 字(常含编译失败行号 / 测试结果)
                stderr_head = stderr[:500] if isinstance(stderr, str) else ""
                stdout_tail = stdout[-500:] if isinstance(stdout, str) else ""
                # 命令拿 args 里的
                cmd = str(tool_meta[i_old][1].get("command", ""))[:80]
                old_msg["content"] = stable_prompt_json({
                    "_folded": True,
                    "_redundant": "large_bash_output_archived",
                    "summary": f"`{cmd}` returncode={rc} (原 stdout {len(stdout)} 字符已折叠)",
                    "stderr_head": stderr_head,
                    "stdout_tail": stdout_tail,
                })
                old_msg["_folded"] = True
                folded += 1
            except Exception:
                pass

    # 7. commit_to_main 折叠(2026-05-03 加):
    # 同一文件被反复 commit(模型不确定时多次保险) → 只保留最后一次,前面折叠。
    # 不像 bash,commit 没有大 stdout 风险,但调多了会刷屏。
    commit_indices = by_tool.get("commit_to_main", [])
    if len(commit_indices) >= 3:
        for i_old in commit_indices[:-2]:
            old_msg = msgs[i_old]
            if old_msg.get("_folded"):
                continue
            try:
                parsed = json.loads(old_msg.get("content") or "{}")
                promoted = parsed.get("promoted") or []
                old_msg["content"] = stable_prompt_json({
                    "_folded": True,
                    "_redundant": "earlier_commit_superseded",
                    "summary": f"早期 commit_to_main 提升了 {promoted[:3]}",
                })
                old_msg["_folded"] = True
                folded += 1
            except Exception:
                pass

    if folded:
        log.info("soft_compact: folded %d redundant tool result(s)", folded)
    return _estimate_msgs_token_size(msgs)


def _now_iso() -> str:
    """当前时间 ISO 字符串(秒精度,本地时区)。"""
    from datetime import datetime as _dt
    return _dt.now().isoformat(timespec="seconds")


def _inject_tool_timestamps(
    result: str,
    *,
    started_at_iso: str,
    finished_at_iso: str,
    elapsed_sec: float,
    mode: str = "full",
) -> str:
    """给 tool result 注入 _ts_iso / _started_at_iso / _tool_elapsed_sec 三字段。

    L7-1 (2026-05-09): 支持 minimal 模式 — 仅慢(>5s)或失败时注入,节省 token。

    设计:
    - result 通常是 JSON object 字符串(各 tool handler 都 json.dumps)→ parse → 注入 → re-dump
    - 不是 JSON 或不是 dict → 包成 {"raw": <原文>, ...}(罕见情况,确保模型仍看到时间戳)
    - 失败兜底返回原 result(绝不让时间戳注入失败影响主路径)

    字段名都用下划线前缀,避免和工具自身字段冲突。
    """
    # L7-1: minimal 模式下,快速成功的 tool call 不注入时间戳
    if mode == "minimal" and elapsed_sec <= 5.0:
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
            if isinstance(parsed, dict) and parsed.get("ok") is not False:
                return result  # 快速成功,不注入
        except (json.JSONDecodeError, ValueError):
            pass

    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, ValueError):
        # 非 JSON,包一层 envelope
        try:
            return json.dumps({
                "_ts_iso": finished_at_iso,
                "_started_at_iso": started_at_iso,
                "_tool_elapsed_sec": elapsed_sec,
                "raw": result if isinstance(result, str) else str(result),
            }, ensure_ascii=False)
        except Exception:
            return result  # 最后兜底
    if not isinstance(parsed, dict):
        # 不是 dict(可能是 list/scalar,极罕见)
        try:
            return json.dumps({
                "_ts_iso": finished_at_iso,
                "_started_at_iso": started_at_iso,
                "_tool_elapsed_sec": elapsed_sec,
                "data": parsed,
            }, ensure_ascii=False)
        except Exception:
            return result
    # 正常路径:dict 顶层加三字段
    parsed.setdefault("_ts_iso", finished_at_iso)
    parsed.setdefault("_started_at_iso", started_at_iso)
    parsed.setdefault("_tool_elapsed_sec", elapsed_sec)
    try:
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        # JSON 失败(理论上 parsed 来自 json.loads 不会),回退原文
        return result


def _safe_progress(progress_cb, iteration: int, msgs: list[dict], event: str | None = None):
    """安全调用 progress 回调，异常不传播。

    event: 剧情节点类型,如 'stuck' / 'breakthrough' / 'long_silence',
        让 progress_cb 内部根据事件生成不同口吻的反馈。
    """
    # Progress callbacks may run in a background task after the tool loop has
    # already appended later messages. Capture the list when scheduling so the
    # generated update describes the event that scheduled it.
    snapshot = list(msgs)

    async def _run() -> None:
        try:
            await progress_cb(iteration, snapshot, event)
        except Exception:
            pass

    return _run()
