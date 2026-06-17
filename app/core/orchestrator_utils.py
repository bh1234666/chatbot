"""orchestrator 的纯工具函数:round2 计划解析、交付文件名清洗、语音需求判定/抽取、
文本时长估算、用户请求抽取、工具摘要、错误净化、内部/OCR 中间文件分类等。

2026-05-20 重构: 从 core/orchestrator.py 原样抽出。经 extract_analysis --closure 验证
自包含(15 符号含2常量, 0 unsafe),仅依赖 stdlib(json/os/re)。orchestrator.py 通过
re-export 保持兼容。
"""
import json
import os
import re


_RESPONSE_PLAN_KEYS = {
    "intent",
    "key_points",
    "tone",
    "length_hint",
    "avoid",
    "callbacks",
    "internal_note",
    "deliverables",
    "voice_reply_text",
    "voice_reply_file",
    "delivery_partial",
    "upgrade_to_hard",
    "upgrade_to_veryhard",
    "round2_complexity",
    "round2_needs_tools",
    "round2_needs_recall",
}


def _as_text_list(value, *, max_items: int = 12, max_len: int = 220) -> list[str]:
    """Best-effort conversion of loose LLM JSON values into compact text bullets."""
    out: list[str] = []

    def _add(item) -> None:
        if item is None:
            return
        if isinstance(item, (dict, list, tuple)):
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip() + "…"
        if text not in out:
            out.append(text)

    if isinstance(value, dict):
        for k, v in value.items():
            if len(out) >= max_items:
                break
            if isinstance(v, (list, tuple)):
                for item in v:
                    _add(f"{k}: {item}")
                    if len(out) >= max_items:
                        break
            else:
                _add(f"{k}: {v}")
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if len(out) >= max_items:
                break
            _add(item)
    else:
        _add(value)
    return out[:max_items]


def _compact_structured_fact(key: str, value, *, max_len: int = 900) -> str:
    """Preserve task-specific structured facts that do not fit ResponsePlan fields."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return f"{key}: {text}"


def _looks_like_deliverable_name(value: str) -> bool:
    text = (value or "").strip().strip("`'\"")
    if not text or len(text) > 180:
        return False
    if any(sep in text for sep in ("\n", "\r", "\t")):
        return False
    # Filename-ish path with an extension. Keep this permissive for Chinese names.
    return bool(re.search(r"[^\\/:\*\?\"<>\|]+\.[A-Za-z0-9\u4e00-\u9fff]{1,12}$", text))


def _normalize_round2_plan_dict(raw: dict | None) -> dict:
    """Normalize loose task JSON into the ResponsePlan schema.

    Round2 occasionally finishes with useful task-specific JSON such as
    {"status": "...", "project": "...", "plan_steps": [...], "saved_file": "..."}.
    Treating that as a schema miss discards the facts gathered by tools and leaves
    Round3 free to invent. This adapter preserves those facts as ResponsePlan fields
    while leaving proper ResponsePlan JSON unchanged.
    """
    if not isinstance(raw, dict):
        return {}

    normalized = dict(raw)
    has_plan_shape = bool(set(raw) & _RESPONSE_PLAN_KEYS)

    key_points = _as_text_list(raw.get("key_points"), max_items=10)
    standard_fact_keys = (
        ("status", "状态"),
        ("project", "项目"),
        ("summary", "摘要"),
        ("result", "结果"),
        ("message", "说明"),
        ("content", "内容"),
        ("plan_steps", "计划"),
        ("steps", "步骤"),
        ("acceptance_criteria", "验收标准"),
        ("saved_file", "已保存文件"),
        ("output_file", "已保存文件"),
        ("file", "文件"),
        ("offer", "后续可做"),
    )
    if not has_plan_shape:
        for key, label in standard_fact_keys:
            if key not in raw:
                continue
            for value in _as_text_list(raw.get(key), max_items=2):
                if key in {"plan_steps", "steps"} and not value.startswith(label):
                    fact = value
                elif ":" in value and value.split(":", 1)[0] == key:
                    fact = f"{label}: {value.split(':', 1)[1].strip()}"
                else:
                    fact = f"{label}: {value}"
                if fact and fact not in key_points:
                    key_points.append(fact)
                if len(key_points) >= 10:
                    break
            if len(key_points) >= 10:
                break

    for key, value in raw.items():
        if key in _RESPONSE_PLAN_KEYS or key.startswith("_"):
            continue
        if key in {
            "top_12_python_files",
            "top_files",
            "top_n",
            "rankings",
            "rows",
            "table",
            "metrics",
            "statistics",
            "results",
            "data",
        } or isinstance(value, (list, tuple, dict)):
            fact = _compact_structured_fact(key, value)
            if fact and fact not in key_points:
                key_points.append(fact)
            if len(key_points) >= 10:
                break
    if not key_points:
        for key, label in (
            ("plan_steps", "计划"),
            ("steps", "步骤"),
        ):
            if key not in raw:
                continue
            values = _as_text_list(raw.get(key), max_items=8)
            for value in values:
                if key in {"plan_steps", "steps"} and not value.startswith(label):
                    key_points.append(value)
                elif ":" in value and value.split(":", 1)[0] == key:
                    key_points.append(f"{label}: {value.split(':', 1)[1].strip()}")
                else:
                    key_points.append(f"{label}: {value}")
                if len(key_points) >= 10:
                    break
            if len(key_points) >= 10:
                break

    if not key_points and not has_plan_shape:
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            for item in _as_text_list(value, max_items=3):
                key_points.append(f"{key}: {item}")
                if len(key_points) >= 8:
                    break
            if len(key_points) >= 8:
                break

    if key_points:
        normalized["key_points"] = key_points

    if not str(raw.get("intent") or "").strip():
        status = str(raw.get("status") or "").strip()
        project = str(raw.get("project") or "").strip()
        if status and project:
            normalized["intent"] = f"向用户说明{status}: {project}"
        elif status:
            normalized["intent"] = f"向用户说明{status}"
        elif project:
            normalized["intent"] = f"围绕{project}回应用户"
        elif key_points:
            normalized["intent"] = "基于工具执行结果回应用户"

    normalized.setdefault("tone", "自然平和")
    normalized.setdefault("length_hint", "中")
    normalized.setdefault("avoid", [])
    normalized.setdefault("callbacks", [])

    deliverables = _as_text_list(raw.get("deliverables"), max_items=12, max_len=180)
    for key in ("saved_file", "output_file", "file"):
        for item in _as_text_list(raw.get(key), max_items=3, max_len=180):
            if _looks_like_deliverable_name(item) and item not in deliverables:
                deliverables.append(item)
    if deliverables:
        normalized["deliverables"] = deliverables

    note_parts: list[str] = []
    existing_note = str(raw.get("internal_note") or "").strip()
    if existing_note:
        note_parts.append(existing_note)
    if not has_plan_shape:
        note_parts.append("round2 final JSON used task-specific schema; preserved fields for Round3")
    for key in ("project", "status", "saved_file", "acceptance_criteria"):
        if raw.get(key):
            for item in _as_text_list(raw.get(key), max_items=2, max_len=120):
                note_parts.append(f"{key}={item}")
                break
    if note_parts:
        normalized["internal_note"] = " | ".join(note_parts)[:300]

    normalized.setdefault("voice_reply_text", "")
    normalized.setdefault("voice_reply_file", "")
    normalized.setdefault("upgrade_to_hard", False)
    normalized.setdefault("upgrade_to_veryhard", False)
    route_complexity = str(raw.get("round2_complexity") or "").strip().lower()
    if route_complexity in {"medium", "hard"}:
        normalized["round2_complexity"] = route_complexity
    else:
        normalized["round2_complexity"] = None
    for key in ("round2_needs_tools", "round2_needs_recall"):
        if isinstance(raw.get(key), bool):
            normalized[key] = raw.get(key)
        else:
            normalized[key] = None
    return normalized


def _plan_dict_from_round2_text(content: str) -> dict:
    """round2 final output 不是 JSON 时构造兜底 plan。

    2026-05-15 修:旧版无论 content 是否真有内容,都返回一个"承认内部出错并请用户
    稍后再发一次"的道歉 plan,把模型已经生成的好内容(实际是给用户的回复,只是
    格式不对)直接丢弃。trace 12:17 真实例子:模型在 round2 iter 2 直接产出了
    一段完整漂亮的角色回复(看图 → 角色化点评 → 反问邀请),但因为不是 JSON 就被
    替换成一句"刚才处理时内部出错"的尴尬话术。

    新逻辑:
      - 内容是实质性自然语言(够长 + 含字母/CJK) → 包装成 plan, 把内容塞进
        key_points, 让 round3 基于此措辞生成最终回复(语气会被人设模型微调,
        但事实和句意保留)。
      - 内容为空 / 仅控制字符 / 长度过短 → 退回到原始的"内部出错"道歉。

    返回的 dict 含可选字段 `_passthrough_text`:外层 round2 拿到后看到这个字段,
    把原始内容透传给 round3 作为强参考。round3 现有路径已 honor plan.intent /
    key_points / tone,不需要额外改造也能产出可用回复。
    """
    text = (content or "").strip()

    # 防御性剥离 chat_json prefill 的孤立 "{" 前导
    if text.startswith("{") and "}" not in text[:200]:
        text = text[1:].lstrip()

    def _looks_like_real_reply(s: str) -> bool:
        if len(s) < 80:
            return False
        # 含字母 / CJK 字符(排除"全空白 + 控制符"那种 131 char 假阳)
        for ch in s:
            if ch.isalpha() or 0x4E00 <= ord(ch) <= 0x9FFF:
                return True
        return False

    if not _looks_like_real_reply(text):
        return {
            "intent": "简短说明本轮没有可靠完成用户委托,请用户确认是否继续或重新发起",
            "key_points": [text[:120] or "这次没有可靠完成用户委托"],
            "tone": "坦诚带点歉意",
            "length_hint": "短",
            "avoid": ["对用户任务内容作分析", "假装已完成任务"],
            "callbacks": [],
            "internal_note": "round2 JSON 重写失败且内容也无实质自然语言,降级到道歉 plan",
            "deliverables": [],
            "upgrade_to_hard": False,
            "upgrade_to_veryhard": False,
        }

    # 有实质内容 → 透传给 round3。
    # length_hint 按字符数大致映射:短/中/长 ≈ <100 / 100-300 / >300。
    if len(text) < 100:
        length_hint = "短"
    elif len(text) < 300:
        length_hint = "中"
    else:
        length_hint = "长"

    # snippet 上限 800 字符,够覆盖绝大多数 round2 直接产出的回复,又不至于让
    # round3 system 消息膨胀。
    snippet = text[:800]

    return {
        "intent": "直接呈现 round2 已经产出的回应给用户",
        "key_points": [
            snippet,
            "保留原回应的内容主线和事实点,不重新组织",
        ],
        "tone": "保持人设原本的口吻",
        "length_hint": length_hint,
        "avoid": [
            "假装新做了一次工作",
            "把原回应拆改为完全不同的句式",
        ],
        "callbacks": [],
        "internal_note": (
            f"round2 final iter 直接输出了完整回应({len(text)} 字符);"
            f"plan 由 fallback 路径基于该内容推断"
        ),
        "deliverables": [],
        "upgrade_to_hard": False,
        "upgrade_to_veryhard": False,
    }


def _clean_deliverable_filenames(deliverables: list[str]) -> list[str]:
    """清理 deliverables 列表：去掉模型混入的文件名后描述文字。

    模型常在 deliverables 里写 "paper.docx — 完整论文(209段)" 而不是纯文件名。
    各种分隔符模式依次尝试，提取文件名部分。不匹配的条目原样保留。

    Do not remove model-selected files here. Boundary warnings are factual
    evidence for a later model decision, while this function only normalizes
    names.
    """
    import re
    _SEP = re.compile(r'\s*[—–\-]\s+')  # em dash / en dash / hyphen
    cleaned = []
    for d in deliverables:
        d = d.strip()
        if not d:
            continue
        m = _SEP.search(d)
        if m:
            candidate = d[:m.start()].strip()
            if '.' in candidate and '/' not in candidate:
                cleaned.append(candidate)
                continue
        # 没有标准分隔符时检查是否是纯文件名(不含空格)
        if ' ' not in d and '.' in d:
            cleaned.append(d)
        elif ' ' in d:
            # "filename.docx description text" 取第一个含扩展名的词
            parts = d.split()
            fn = next((p for p in parts if '.' in p and 2 < len(p) < 200), None)
            if fn:
                cleaned.append(fn)
            else:
                cleaned.append(d)  # 无法解析,原样保留
        else:
            cleaned.append(d)
    return cleaned


def _filter_voice_instruct(raw: str) -> str:
    """过滤 voice_instruct 中不支持的项，只保留 OmniVoice 有效的 instruct tags。"""
    if not raw:
        return ""
    _VALID = frozenset({
        "female", "male", "child", "teenager", "young adult", "middle-aged",
        "elderly", "very low pitch", "low pitch", "moderate pitch",
        "high pitch", "very high pitch", "whisper", "american accent",
        "british accent", "australian accent", "chinese accent",
        "canadian accent", "indian accent", "korean accent",
        "portuguese accent", "russian accent", "japanese accent",
        "河南话", "陕西话", "四川话", "贵州话", "云南话", "桂林话",
        "济南话", "石家庄话", "甘肃话", "宁夏话", "青岛话", "东北话",
    })
    raw = raw.replace("，", ",")
    items = [x.strip() for x in raw.split(",")]
    filtered = [x for x in items if x.lower() in _VALID]
    return ", ".join(filtered)


def _strip_voice_instruct(persona: str) -> str:
    if not persona:
        return ""
    return re.sub(r"^\s*voice_instruct\s*:.*(?:\r?\n)?", "", persona, flags=re.IGNORECASE | re.MULTILINE)


def _extract_voice_instruct(persona: str) -> str:
    """从人设内容中提取 voice_instruct 字段,过滤掉无效项。

    2026-05-09 Patch 18: 旧版 `line.startswith("voice_instruct:")` 严格前缀,
    "  voice_instruct: ..." (前导空白)或 "Voice_Instruct: ..." (大小写不一致)
    都不识别 → 人设作者疑惑为啥音色配置无效。改为 regex 容错。
    """
    if not persona:
        return ""
    _voice_re = re.compile(r"^\s*voice_instruct\s*:\s*(.*)$", re.IGNORECASE)
    for line in persona.splitlines():
        m = _voice_re.match(line)
        if m:
            return _filter_voice_instruct(m.group(1).strip())
    return ""


def _is_voice_demanded(user_message: str) -> bool:
    """检查用户是否明确要求语音回复。"""
    if not user_message:
        return False
    msg = user_message.lower()
    keywords = [
        "用语音", "语音回复", "语音回我", "说给我听", "语音告诉我",
        "用说的", "语音输出", "语音说", "用声音", "用嘴说",
    ]
    return any(kw in msg for kw in keywords)


def _estimate_text_duration(text: str) -> float:
    """估算文本的 TTS 语音时长(秒)。

    2026-05-09 Patch 19: 旧版本是 voice_output._estimate_duration 的复制粘贴。
    现在直接委托过去保持单一真相源。逻辑/系数(中文 3 字/秒,英文 2.5 词/秒)同。
    """
    from app.llm.voice_output import _estimate_duration
    return _estimate_duration(text)


def _extract_tool_summary(msgs: list[dict]) -> str:
    """从消息历史中提取最近工具调用的摘要（progress 反馈用）。

    设计：把 tool_call 和它对应的 tool_result 配对成一行，比"调用…\n成功"两行
    密度高一倍。同名工具连续失败折叠成"× N 次"，避免刷屏。
    取最近 30 条（覆盖约 10-15 轮工具循环）。
    """
    # tool_call_id → (name, brief)
    pending: dict[str, tuple[str, str]] = {}
    lines: list[str] = []
    last_failure: tuple[str, str] | None = None  # (key, line index marker)
    last_failure_count = 0

    for m in msgs[-30:]:
        role = m.get("role", "")
        if role == "assistant":
            for tc in m.get("tool_calls", []) or []:
                tcid = tc.get("id", "")
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                brief = _brief_tool_desc(name, args)
                if tcid:
                    pending[tcid] = (name, brief)
        elif role == "tool":
            tcid = m.get("tool_call_id", "")
            name, brief = pending.pop(tcid, ("?", ""))
            content = m.get("content", "")
            try:
                r = json.loads(content) if isinstance(content, str) else content
            except Exception:
                r = content
            ok = r.get("ok") if isinstance(r, dict) else None
            if ok is True:
                # 2026-05-07: delegate 结果展开 per-helper 状态,避免 lite 模型看到
                # "✓ 分派 2 个并行任务" 就以为一切顺利(实际 helper 可能还在跑/超时/卡死)
                if name == "delegate":
                    line = _summarize_delegate_result(r, brief)
                else:
                    line = f"✓ {brief or name}"
                last_failure = None
                lines.append(line)
            elif ok is False:
                # 2026-05-07: 错误信息清洗 — 去掉技术堆栈/API 细节,只保留人能看懂的部分
                raw_err = str(r.get("error", "?"))
                err = _sanitize_error_for_progress(raw_err)
                line = f"✗ {brief or name} — {err}"
                # 同一处连续失败折叠
                key = f"{name}|{err[:30]}"
                if last_failure and last_failure[0] == key:
                    last_failure_count += 1
                    # 替换最后一行带计数
                    lines[-1] = f"✗ {brief or name} — {err}  (× {last_failure_count + 1})"
                else:
                    last_failure = (key, line)
                    last_failure_count = 0
                    lines.append(line)
            else:
                lines.append(f"· {brief or name}")
                last_failure = None

    # 还在 pending 的(tool_call 还没收到 result)
    for name, brief in pending.values():
        lines.append(f"… {brief or name}（运行中）")

    if not lines:
        return "（暂无工具调用）"
    return "\n".join(lines)


_INTERNAL_PROGRESS_TERMS = (
    "ocr", "helper", "工具", "跨模型", "编码任务", "fast", "balanced",
    "accurate", "档位", "engine", "round2", "round3", "tts", "守卫",
)


def _progress_brief_has_internal_terms(text: str) -> bool:
    lower = (text or "").lower()
    return any(term in lower for term in _INTERNAL_PROGRESS_TERMS)


def _brief_tool_desc(name: str, args: dict) -> str:
    """为工具调用生成简短中文描述（progress 反馈用）。覆盖全部 14 个工具。"""
    # 文件局部读写
    if name == "inspect_file":
        return f"预检 {str(args.get('path', ''))[:60]}"
    if name == "read_file":
        path = str(args.get("path", ""))[:60]
        s, e = args.get("start_line"), args.get("end_line")
        rng = f" 行 {s}-{e}" if s or e else ""
        return f"读 {path}{rng}"
    if name == "edit_file":
        path = str(args.get("path", ""))[:60]
        return f"改 {path}"
    if name == "insert_in_file":
        path = str(args.get("path", ""))[:60]
        line = args.get("after_line", "?")
        return f"在 {path} 第 {line} 行后插入"
    if name == "search_in_file":
        path = str(args.get("path", ""))[:40]
        pat = str(args.get("pattern", "?"))[:40]
        return f"在 {path} 找「{pat}」"
    # 执行 / 创建
    if name == "python":
        return "执行计算代码"
    if name == "workspace":
        action = args.get("action", "?")
        path = str(args.get("path", ""))[:60]
        if action == "write":
            return f"写文件 {path}"
        if action == "run":
            cmd = str(args.get("command", ""))[:80]
            return f"运行 {cmd}"
        if action == "mkdir":
            return f"创建目录 {path}"
        return f"workspace {action}"
    # 并行
    if name == "delegate":
        tasks = args.get("tasks", [])
        task_ids = []
        internal_task_seen = False
        for t in tasks:
            tid = str(t.get("task_id", "?"))
            kind = str(t.get("kind", ""))
            if kind in ("ocr", "tts") or _progress_brief_has_internal_terms(tid):
                internal_task_seen = True
                continue
            task_ids.append(tid)
        if internal_task_seen and not task_ids:
            return f"处理 {len(tasks)} 个子任务"
        if internal_task_seen:
            return f"处理 {len(tasks)} 个子任务"
        return f"分派 {len(tasks)} 个并行任务:{task_ids}"
    # 记忆 / 检索
    if name == "expand_warm":
        return f"展开 {len(args.get('ids', []))} 条温记忆"
    if name == "expand_cold":
        return f"展开 {len(args.get('ids', []))} 条长期记忆"
    if name == "expand_kb":
        return f"展开 {len(args.get('ids', []))} 条知识库"
    if name == "search_files":
        return f"搜索文件:{args.get('query', '?')}"
    if name in {"fetch_indexed_file", "fetch_group_file"}:
        nid = str(args.get("kb_node_id", ""))[:20]
        return f"提取索引文件 {nid}"
    if name == "mark_avoid_mention":
        return f"标记 {len(args.get('topics', []))} 个回避话题"
    return ""


def _summarize_delegate_result(r: dict, brief: str) -> str:
    """从 delegate 返回的 JSON 中提取 per-helper 状态摘要。"""
    action = str(r.get("action") or "").strip().lower()
    if action == "spawn_async":
        spawned = r.get("spawned") or []
        return f"已启动 {len(spawned)} 个后台子任务"
    if action == "poll":
        polled = r.get("polled") or []
        counts: dict[str, int] = {}
        for item in polled:
            if isinstance(item, dict):
                status = str(item.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
        if counts:
            bits = "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
            return f"查询子任务状态: {bits}"
        return f"查询 {len(polled)} 个子任务状态"
    if action == "collect":
        requested = r.get("helpers_requested", 0)
        completed = r.get("helpers_completed", 0)
        running = r.get("helpers_still_running", 0)
        unavailable = r.get("helpers_unavailable", 0)
        result_ids: list[str] = []
        failed_ids: list[str] = []
        for item in r.get("results") or []:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("task_id") or "").strip()
            if not tid or _progress_brief_has_internal_terms(tid):
                continue
            terminal = str(item.get("terminal_reason") or "").strip().lower()
            outputs = item.get("outputs_check") or {}
            outputs_complete = outputs.get("outputs_complete") if isinstance(outputs, dict) else None
            producer_verified = outputs.get("producer_self_verified") is True if isinstance(outputs, dict) else False
            quality_blocked = bool(outputs.get("quality_blocked")) if isinstance(outputs, dict) else False
            if (
                producer_verified
                or (
                    item.get("ok") is True
                    and terminal == "completed"
                    and outputs_complete is not False
                    and not quality_blocked
                )
            ):
                result_ids.append(tid)
            elif item.get("ok") is False or terminal in {"failed", "interrupted", "stuck", "timeout", "crashed"}:
                failed_ids.append(tid)
        running_ids: list[str] = []
        for item in r.get("still_running") or []:
            if isinstance(item, dict):
                tid = str(item.get("task_id") or "").strip()
            else:
                tid = str(item).strip()
            if tid and not _progress_brief_has_internal_terms(tid):
                running_ids.append(tid)

        parts = [f"收到 {completed}/{requested} 个子任务结果"]
        if result_ids:
            parts.append(f"已完成: {', '.join(result_ids[:6])}")
        if failed_ids:
            parts.append(f"失败: {', '.join(failed_ids[:4])}")
        if running_ids:
            parts.append(f"仍在运行: {', '.join(running_ids[:6])}")
        if running:
            parts.append(f"{running} 个仍在运行")
        if unavailable:
            parts.append(f"{unavailable} 个不可收集")
        return "，".join(parts)
    if action == "wait_any":
        winner = r.get("winner_task_id")
        if winner:
            return f"等到子任务完成: {winner}"
        return "等待子任务返回超时，已更新状态"
    if action == "status":
        summary = r.get("summary") or {}
        return (
            "子任务概览: "
            f"运行 {summary.get('running', 0)}，"
            f"卡住 {summary.get('stuck', 0)}，"
            f"完成 {summary.get('done_ok', 0)}，"
            f"失败 {summary.get('done_failed', 0)}"
        )

    results = r.get("results", [])
    if not results:
        return f"✓ {brief}"
    parts: list[str] = []
    internal_seen = False
    for h in results[:6]:  # 最多展示 6 个 helper
        tid = h.get("task_id", "?")
        if _progress_brief_has_internal_terms(str(tid)):
            internal_seen = True
            tid = "子任务"
        h_ok = h.get("ok")
        elapsed = h.get("elapsed_sec")
        interrupted = h.get("interrupted")
        stuck = h.get("stuck")
        if interrupted:
            parts.append(f"{tid}=中断")
        elif stuck:
            parts.append(f"{tid}=卡死")
        elif h_ok is True:
            ts = f"{elapsed:.0f}s" if elapsed else ""
            parts.append(f"{tid}=✓{ts}")
        elif h_ok is False:
            parts.append(f"{tid}=✗")
        else:
            parts.append(f"{tid}=…")
    still = r.get("helpers_still_running", 0)
    if still:
        parts.append(f"+{still}运行中")
    total_s = r.get("total_elapsed_seconds")
    ts = f"({total_s:.0f}s)" if total_s else ""
    if internal_seen:
        done = sum(1 for h in results if h.get("ok") is True)
        failed = sum(1 for h in results if h.get("ok") is False)
        if failed:
            return f"子任务 {done} 完成、{failed} 失败{ts}"
        return f"子任务 {done or len(results)} 完成{ts}"
    return f"分派 {ts}: {', '.join(parts)}" if parts else f"✓ {brief}"


def _sanitize_error_for_progress(raw: str) -> str:
    """清洗错误信息:去技术堆栈,保留人能看懂的一句。≤60 字。"""
    if not raw:
        return "?"
    # 截取第一个有意义的分句(以句号/换行/逗号分隔)
    for sep in ("。", "\n", "；", "，", ". "):
        idx = raw.find(sep)
        if 10 < idx < 120:
            raw = raw[:idx]
            break
    # 去掉常见技术噪音
    for noise in ("batch_timeout_majority", "escalation_advice", "Traceback",
                  "KeyError:", "TypeError:", "asyncio.", "__traceback__"):
        raw = raw.replace(noise, "")
    raw = raw.strip().replace("  ", " ")
    return raw[:60] if len(raw) > 60 else raw


def _extract_user_request(msgs: list[dict]) -> str:
    """从消息中提取用户原始请求。"""
    for m in msgs:
        if m.get("role") == "user":
            content = str(m.get("content", ""))
            if "当前发言" in content:
                parts = content.split("当前发言")
                if len(parts) > 1:
                    return parts[-1].strip()[:300]
            return content[:300]
    return "（未知请求）"


# 2026-05-12 P15.F: 内部文件黑名单(无论 score 多高都不作为产物)
# 病因(实测 23:46 trace): 旧交付 fallback 在 plan.deliverables 为空时硬补
# 8 个全是 .rewrite_count.json/.todos_call_count.json 等元数据 → 用户拿到一堆垃圾。
# 这些文件是 helper 内部计数器/标识, 不该出现在交付物里。
_AUTOFIX_INTERNAL_BLACKLIST_PATTERNS = (
    # helper 内部元数据/计数器(P15.F catch 的就是这堆)
    ".rewrite_count.json",
    ".todos_call_count.json",
    ".session_tag",
    ".snapshot",
    "_run_count.json",
    "_call_count.json",
    # 中间编译产物(非用户要的最终产物)
    "bench_out.txt",
    "build_output.txt",
    "compile_log.txt",
    # 助手测试中间文件
    "_test_fix",
    "_test_v",
    # 系统生成文件
    ".helper_state",
    ".task_meta",
    # OCR 原始结果(内部中间文件, 不应直接发给用户;
    # 主进程或 edit helper 应基于这些结果生成最终用户消息)
    "ocr_result",
)


def _is_internal_file(fname_low: str) -> bool:
    """判定文件是否是 helper/系统内部文件, 不该作为用户交付物。"""
    basename = os.path.basename(fname_low)
    if basename.endswith(".txt") and (
        basename.startswith("ocr_")
        or "_ocr" in basename
        or "ocr_" in basename
        or "ocr" in basename and any(s in basename for s in ("question", "result", "raw", "report"))
    ):
        return True
    _, ext = os.path.splitext(basename)
    patterns = _AUTOFIX_INTERNAL_BLACKLIST_PATTERNS
    if ext in {".mp3", ".wav", ".ogg"}:
        patterns = tuple(p for p in patterns if p not in {"_test_fix", "_test_v"})
    return any(p in basename or p in fname_low for p in patterns)


_INTERNAL_DELIVERABLE_DIRS = {
    "_delegate_",
    "_helpers_shared",
    "_shared",
    "_env",
    "_tool_results",
    "_scratch",
    "_downloaded_media",
    "__pycache__",
}

_INTERNAL_DELIVERABLE_BASENAMES = {
    "_session_manifest.json",
    ".helper_summary.txt",
}

_INTERNAL_DELIVERABLE_REGEXES = (
    re.compile(r"^\.helper_.*", re.IGNORECASE),
    re.compile(r"^helper_.*", re.IGNORECASE),
    re.compile(r"^(?:.*_)?framework_contract.*\.(?:md|txt|json)$", re.IGNORECASE),
    re.compile(r"^_py_cmd_[0-9a-f]{6,}\.py$", re.IGNORECASE),
    re.compile(r".*__analysis_output\.txt$", re.IGNORECASE),
    re.compile(r".*_analysis_output\.txt$", re.IGNORECASE),
    re.compile(r".*_analyse(?:\d+)?_out\.txt$", re.IGNORECASE),
    re.compile(r".*_analyse_full\.txt$", re.IGNORECASE),
)


def _is_internal_deliverable_file(name: str) -> bool:
    """Return True for helper evidence, manifests, probes, and staging files.

    This is a user-visible artifact boundary, not a content sanitizer. It keeps
    internal workflow evidence out of plan.deliverables while still allowing
    explicit user-facing files such as analysis_report.md, report.docx, charts,
    source files, and datasets.

    产物边界：helper 证据、会话清单、探测脚本和暂存路径不作为用户可见交付物。
    """
    raw = str(name or "").strip().strip("`'\"")
    if not raw:
        return True
    normalized = raw.replace("\\", "/")
    lowered = normalized.lower()
    parts = [p for p in lowered.split("/") if p]
    basename = os.path.basename(lowered)
    if not basename:
        return True
    if basename in _INTERNAL_DELIVERABLE_BASENAMES:
        return True
    # `.temp/` is the controlled staging area for fresh artifacts before they
    # are promoted. Keep other dot paths internal.
    if any(part.startswith(".") and part != ".temp" for part in parts):
        return True
    if any(part in _INTERNAL_DELIVERABLE_DIRS or part.startswith("_delegate_") for part in parts[:-1]):
        return True
    if any(rx.match(basename) for rx in _INTERNAL_DELIVERABLE_REGEXES):
        return True
    return _is_internal_file(lowered)


_AUTOFIX_OCR_INTERMEDIATE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _is_ocr_intermediate_image(fname_low: str) -> bool:
    normalized = fname_low.replace("\\", "/")
    basename = os.path.basename(normalized)
    stem, ext = os.path.splitext(basename)
    if ext not in _AUTOFIX_OCR_INTERMEDIATE_IMAGE_EXTS:
        return False
    if "/_downloaded_media/" not in f"/{normalized}":
        return False
    if stem.endswith(("_top", "_bot", "_bottom")):
        return True
    return re.search(r"(^|[_-])slice[_-]?\d*$", stem) is not None
