"""context 构建的纯 helper:n-gram、Jaccard 相似度、当前时间信息、失败图片下载检测、
近期 bot 日志抽取。

2026-05-20 重构: 从 core/context.py 原样抽出。closure 自包含(5 函数, 0 unsafe),
仅依赖 stdlib(datetime)。context.py re-export 兼容,调用点零改动。
"""
from datetime import datetime, timedelta, timezone


def _detect_failed_image_downloads(messages: list[dict]) -> list[dict]:
    """检测 recent_group_messages 中仍没有本地副本的 CQ inline 图片。"""
    import re as _re

    failed: list[dict] = []
    _CQ_IMAGE_PATTERN = _re.compile(r'\[CQ:image,file=([^,\]]+)')
    _LOCAL_IMAGE_PATTERN = _re.compile(r'\[本地image:\s*([^\]\s]+)')

    def _key(name: str) -> str:
        base = str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0].lower()
        for prefix in ("img_", "image_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
        return stem

    local_keys: set[str] = set()
    local_msg_indices: list[int] = []
    for idx, m in enumerate(messages):
        text = m.get("text", "") or m.get("content", "") or ""
        for lm in _LOCAL_IMAGE_PATTERN.finditer(text):
            k = _key(lm.group(1))
            if k:
                local_keys.add(k)
                local_msg_indices.append(idx)

    for idx, m in enumerate(messages):
        text = m.get("text", "") or m.get("content", "") or ""
        if not text:
            continue
        if _LOCAL_IMAGE_PATTERN.search(text):
            continue
        for fm in _CQ_IMAGE_PATTERN.finditer(text):
            fname = fm.group(1).strip()
            k = _key(fname)
            if not fname:
                continue
            if k and any(k in local_key or local_key in k for local_key in local_keys):
                continue
            if any(abs(local_idx - idx) <= 2 for local_idx in local_msg_indices):
                continue
            ts = m.get("created_at", "") or m.get("timestamp", "") or m.get("ts", "")
            sender = m.get("sender_name", "") or m.get("user_name", "") or m.get("sender", "?")
            failed.append({"ts": str(ts)[:16], "sender": sender, "file": fname})
    return failed


def _ngrams(s: str, n: int = 2) -> set[str]:
    """字符 n-gram 集合(用于 Jaccard 相似度)。中文场景默认 bi-gram(2-gram),
    比 tri-gram 更密集,中文事件压缩效果好(实测 trace 779bbcf0:trigram 阈值 0.55
    只折叠 2/38 条;bigram 阈值 0.40 折叠 6/14 条强重复样本)。
    空串返回 {''} 而非空集合,确保 Jaccard 在边界条件下不会 ZeroDivisionError。"""
    s = (s or "").strip()
    if len(s) < n:
        return {s} if s else {""}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _extract_recent_bot_logs(base: list[dict], *, limit: int = 5) -> str:
    """从 base messages 中抽取最近 N 条 assistant 消息里的 bot_log 段。
    返回拼接后的字符串(无 bot_log 时返回空)。"""
    import re as _re_mod
    _BOT_LOG_RE = _re_mod.compile(r"<bot_log>(.*?)</bot_log>", _re_mod.DOTALL)
    _BOT_LOG_BRIEF_RE = _re_mod.compile(r"<bot_log_brief>(.*?)</bot_log_brief>", _re_mod.DOTALL)
    found: list[str] = []

    def _sanitize_private_work_note(text: str) -> str:
        value = str(text or "")
        value = _re_mod.sub(r"\b_delegate_[\w.-]+\b", "work item", value)
        value = _re_mod.sub(r"\binternal_run_[\w.-]+\b", "work item", value)
        replacements = (
            ("INCOMPLETE_HELPER_RESULT", "INCOMPLETE_WORK_RESULT"),
            ("completed-helper", "completed work step"),
            ("helpers=", "work_status="),
            ("helper=", "work_item="),
            ("helper report", "available evidence"),
            ("helper reports", "available evidence"),
            ("helper/tool evidence", "work/tool evidence"),
            ("helper output facts", "output facts"),
            ("helper task", "work step"),
            ("helper-owned", "generated"),
            ("helper_producer_self_verified", "output_self_verified"),
            ("producer_self_verified", "output_self_verified"),
            ("producer-owned", "generated"),
            ("producer evidence", "available evidence"),
            ("producer helpers", "work steps"),
            ("producer helper", "work step"),
            ("producer step", "generation step"),
            ("main process", "coordinator"),
            ("main thread", "coordinator"),
            ("_helpers_shared/", "work material/"),
            ("internal_shared/", "work material/"),
            ("_delegate_", "work_item_"),
            ("internal_run_", "work_item_"),
            ("clean_helper_batch", "clean work batch"),
            ("processing_records", "work_status"),
            ("processing record", "work item"),
        )
        for old, new in replacements:
            value = value.replace(old, new)
            value = value.replace(old.capitalize(), new.capitalize())
        value = _re_mod.sub(r"\bhelpers_(?:still_running|completed)\b", "work_status", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bhelpers\s*([=:])", r"work_status\1", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bbackground_work\s*([=:])", r"work_status\1", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bbackground\s+work\b", "work status", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bprocessing_records\b", "work_status", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bprocessing\s+records\b", "work status", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bprocessing\s+record\b", "work item", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bhelpers\b", "work items", value, flags=_re_mod.IGNORECASE)
        value = _re_mod.sub(r"\bhelper\b", "work item", value, flags=_re_mod.IGNORECASE)
        return value

    def _count_status_items(value: str) -> int:
        raw = str(value or "").strip()
        if not raw:
            return 0
        if raw.isdigit():
            return int(raw)
        stripped = raw.strip("[]{} ")
        if not stripped:
            return 0
        return len([item for item in stripped.split(",") if item.strip()])

    def _summarize_private_work_status(key: str, value: str) -> str:
        key_l = str(key or "").lower()
        raw = str(value or "")
        if key_l == "helpers_still_running":
            return f"running:{_count_status_items(raw)}"
        if key_l == "helpers_completed":
            return f"done:{_count_status_items(raw)}"

        counts: dict[str, int] = {}
        for label, body in _re_mod.findall(r"\b(done|running|failed|stuck|aborted)\s*:\s*\[([^\]]*)\]", raw, flags=_re_mod.IGNORECASE):
            items = [item.strip() for item in body.split(",") if item.strip()]
            normalized = "failed" if label.lower() in {"failed", "stuck", "aborted"} else label.lower()
            counts[normalized] = counts.get(normalized, 0) + len(items)
        if not counts and raw.strip():
            counts["present"] = _count_status_items(raw)
        if not counts:
            counts["present"] = 0
        order = ("done", "running", "failed", "present")
        return ",".join(f"{label}:{counts[label]}" for label in order if label in counts)

    def _compact_bot_log(inner: str) -> str:
        parts: list[str] = []
        for kv in (inner or "").split(" | "):
            kv_strip = kv.strip()
            if kv_strip.startswith((
                "intent=", "key_points=", "deliverables=", "delivery_partial=",
                "in_main=", "helpers=", "helpers_still_running=", "helpers_completed=",
                "background_work=", "processing_records=", "note=", "aborted=", "complexity=",
            )):
                if "=" in kv_strip:
                    key, value = kv_strip.split("=", 1)
                    value = value[:200] + ("..." if len(value) > 200 else "")
                    if key in {
                        "helpers", "helpers_still_running", "helpers_completed",
                        "background_work", "processing_records",
                    }:
                        parts.append(f"work_status={_summarize_private_work_status(key, value)}")
                        continue
                    else:
                        key = _sanitize_private_work_note(key)
                    parts.append(f"{key}={_sanitize_private_work_note(value)}")
                else:
                    parts.append(_sanitize_private_work_note(kv_strip))
        return f"<bot_log_brief>{' | '.join(parts)}</bot_log_brief>" if parts else ""

    # base 第一条是 system, 后面是 user/assistant 交错; 找历史 user 块。
    # 新版 context 使用英文 "Conversation History"，旧版/部分测试可能仍是中文标题。
    for m in base:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, str):
            continue
        if "## 对话历史" not in content and "## Conversation History" not in content:
            continue
        matches: list[tuple[int, str]] = []
        for match in _BOT_LOG_RE.finditer(content):
            compacted = _compact_bot_log(match.group(1))
            if compacted:
                matches.append((match.start(), compacted))
        for match in _BOT_LOG_BRIEF_RE.finditer(content):
            inner = _sanitize_private_work_note(match.group(1).strip())
            if inner:
                matches.append((match.start(), f"<bot_log_brief>{inner}</bot_log_brief>"))
        for _, compacted in sorted(matches, key=lambda item: item[0]):
            found.append(compacted)
    # 取最近 limit 条
    if not found:
        return ""
    recent = found[-limit:]
    return "\n\n".join(recent)


# ── 工具 ────────────────────────────────────────────────────
def _current_time_info() -> str:
    """返回当前时间字符串。

    2026-05-12 P48: 时间精度从秒级 (%H:%M:%S) 降到分钟级 (%H:%M)。
    病因: prompt cache 命中率 85% 主要被秒级时间破坏 — 每次 LLM 调用 system prompt
    都包含不同的秒数, 跨任务/跨进程 prefix 100% miss。
    降级到分钟级后:
      - 同一分钟内的所有任务共享 system prompt prefix → cache 命中
      - 群聊机器人对秒级时间精度无实际需求
      - LLM 仍能感知日期/时刻级时间, 不影响业务逻辑
    预期: 跨任务 cache 命中率从 85% → 95%+
    """
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=8)
    return (
        f"UTC:{now_utc.strftime('%Y-%m-%d %H:%M')}  "
        f"本地(北京 UTC+8):{now_local.strftime('%Y-%m-%d %H:%M')}  "
        f"星期{['一','二','三','四','五','六','日'][now_local.weekday()]}"
    )
