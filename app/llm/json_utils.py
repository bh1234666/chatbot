"""LLM 输出 JSON 的容错解析与修复:截断补全、控制字符/内层引号转义、宽松/严格解析、
本地抽取、工具调用参数归一化。

2026-05-20 重构: 从 llm/client.py 原样抽出。经 extract_analysis --closure 验证自包含
(7 函数, 0 unsafe),仅依赖 stdlib(json)。client.py 通过 re-export 保持兼容,
delegate.py 的 `from app.llm.client import _parse_json_strict` 仍可解析。
"""
import json


def stable_prompt_json(value) -> str:
    """Serialize model-visible compact JSON with deterministic key order.

    Use this for folded tool results, summaries, and other JSON strings that
    are inserted back into LLM messages. API/SSE responses may keep their
    existing formatting when user-facing compatibility matters.

    用于会回流进模型上下文的 JSON，固定 key 顺序与空白，减少前缀抖动。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )



# 2026-05-20 重构: 工具参数 JSON 解析失败时的统一提示(原本在 client.py 与 delegate.py
# 各有一份逐字相同的副本,合并到此共享常量,消除重复)。
TOOL_ARGS_JSON_BROKEN_HINT = (
    "The tool-call arguments were not valid JSON, so the call was not dispatched to the real tool. "
    "Retry with the same intent and valid JSON. Check that newlines, quotes, and backslashes inside strings are escaped correctly. "
    "For long delegate requests, write long file lists or shared instructions to a compact workspace manifest and keep each helper prompt short; "
    "use `framework` for shared context and split a large fan-out into smaller batches.\n"
    "工具参数 JSON 解析失败；按同一意图重试，长 delegate 请求先写清单文件，prompt 保持短，分批派发。"
)

def _escape_control_chars_in_json_strings(raw: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    changed = False
    for ch in raw:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            out.append(ch)
            in_string = not in_string
            continue
        if in_string and ord(ch) < 0x20:
            changed = True
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(f"\\u{ord(ch):04x}")
            continue
        out.append(ch)
    return "".join(out) if changed else raw


def _complete_truncated_json_suffix(raw: str) -> str | None:
    in_string = False
    escaped = False
    stack: list[str] = []
    for ch in raw:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            if in_string:
                escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    suffix = ""
    if in_string:
        suffix += '"'
    suffix += "".join(reversed(stack))
    return suffix or None


def _escape_inner_quotes_in_json_strings(raw: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    changed = False
    n = len(raw)
    for i, ch in enumerate(raw):
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch != '"':
            out.append(ch)
            continue
        if not in_string:
            out.append(ch)
            in_string = True
            continue
        j = i + 1
        while j < n and raw[j] in " \t\r\n":
            j += 1
        if j >= n or raw[j] in ",:}]":
            out.append(ch)
            in_string = False
        else:
            out.append('\\"')
            changed = True
    return "".join(out) if changed else raw


def _normalize_tool_call_args_for_dispatch(raw: str | None) -> tuple[dict, json.JSONDecodeError | None, bool]:
    raw_str = raw or "{}"
    try:
        parsed = json.loads(raw_str)
        if isinstance(parsed, dict):
            return parsed, None, False
        if isinstance(parsed, str):
            try:
                reparsed = json.loads(parsed)
                if isinstance(reparsed, dict):
                    return reparsed, None, True
            except json.JSONDecodeError:
                pass
        return {}, None, False
    except json.JSONDecodeError as e:
        err_msg = str(e)
        repair_candidates: list[str] = []
        if any(marker in err_msg for marker in ("Invalid control character", "Unterminated string", "Expecting ',' delimiter")):
            control_repaired = _escape_control_chars_in_json_strings(raw_str)
            quote_repaired = _escape_inner_quotes_in_json_strings(raw_str)
            both_repaired = _escape_inner_quotes_in_json_strings(control_repaired)
            for candidate in (control_repaired, quote_repaired, both_repaired):
                if candidate != raw_str and candidate not in repair_candidates:
                    repair_candidates.append(candidate)
                suffix = _complete_truncated_json_suffix(candidate)
                if suffix:
                    completed = candidate + suffix
                    if completed not in repair_candidates:
                        repair_candidates.append(completed)
            for candidate in repair_candidates:
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed, None, True
                except json.JSONDecodeError:
                    pass
        if raw_str.startswith('"'):
            stripped = raw_str.strip()
            if stripped.endswith('"'):
                try:
                    unquoted = json.loads(stripped)
                    if isinstance(unquoted, str):
                        parsed = json.loads(unquoted)
                        if isinstance(parsed, dict):
                            return parsed, None, True
                except json.JSONDecodeError:
                    pass
        return {}, e, False


def _try_parse_json(s: str):
    """debug 用：tool result 一般是 JSON 字符串，解析后展示更清晰。"""
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s


def _parse_json_strict(text: str) -> dict:
    """
    宽松 JSON 解析。模型可能返回 ```json fences / 前后多余文字 /
    重复开括号（如 "{\\n\\n{"）等，逐层剥离后解析。
    """
    s = text.strip()
    # 去 markdown 代码围栏
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # 找第一个 { 和最后一个 }
    if not s.startswith("{"):
        i = s.find("{")
        if i >= 0:
            s = s[i:]
    if not s.endswith("}"):
        j = s.rfind("}")
        if j >= 0:
            s = s[: j + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 可能是 "{\n\n{...}" 这种重复开括号——从最后一个 { 重试
        last = s.rfind("{")
        if last > 0:
            s2 = s[last:]
            if not s2.endswith("}"):
                j = s2.rfind("}")
                if j >= 0:
                    s2 = s2[: j + 1]
            return json.loads(s2)
        raise


def _try_extract_json_locally(content: str) -> str | None:
    """本地提取干净 JSON 字符串,失败返回 None。

    用于 finalize 路径降低对 LLM cleanup 的依赖(Bug G 修)。实测一个 hard 任务
    可触发 200+ 次 finalize.cleanup,每次额外调一次 LLM 浪费 5-30 秒。本函数
    试图本地用 _parse_json_strict 剥离前缀文字 / markdown 包裹 / 重复开括号,
    成功则返回 re-serialize 的纯 JSON,让调用方拿到合法 JSON 跳过 LLM cleanup。

    返回:
        str — 解析成功,标准 JSON 字符串(json.loads 必能再次解析)
        None — 解析失败,调用方应降级到 LLM cleanup
    """
    if not content or len(content) < 2:
        return None
    try:
        parsed = _parse_json_strict(content)
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None
