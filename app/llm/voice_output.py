# -*- coding: utf-8 -*-
"""
语音输出决策 — 用 lite 模型分析回复是否应该通过当前语音输出层发送。

规则(按优先级):
  1. 语音功能关闭、空回复、超过语音通道时长上限 → 不发送语音
  2. voice_reply_preference 精确为 0/1 时作为人设硬边界
  3. 其余情况由 Round3 语音/文字 classifier 根据用户、人设、计划和内容舒适度决定

核心原则：语音倾向是给 LLM 的连续人设事实，不是本地阈值；本地逻辑只做硬安全边界、
精确 0/1 人设边界。classifier 不可用时不合成本地 voice/text 决策，只保留已可见的文字回复。
用户显式语音/文字意图也是 classifier 事实，不是本地 voice/text 短路。

语音回复时不发文字。

使用:
    from app.llm.voice_output import decide_voice, VoiceDecision

    decision = await decide_voice(
        reply_text=final_reply,
        user_message=user_msg,
        persona=persona_content,
    )
    if decision.use_voice:
        # 生成 TTS 并推送语音
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from app.config import settings

log = logging.getLogger(__name__)

# 语音时长估算: 中文约 3 字/秒, 英文约 2.5 词/秒
_CHAR_PER_SECOND_ZH = 3.0
_WORD_PER_SECOND_EN = 2.5
_MAX_VOICE_SECONDS = 60
_VOICE_CLASSIFIER_TIMEOUT_SEC = max(0.5, float(getattr(settings, "voice_classifier_timeout_sec", 3.0) or 3.0))


@dataclass
class VoiceDecision:
    use_voice: bool = False
    voice_text: str = ""
    too_long: bool = False
    reason: str = ""
    llm_decision_available: bool = True


# 2026-05-16 Round 14b: round3 三者并行决策结果, 由 _round3_parallel 设置.
# 2026-06-13: voice 预判只选择生成侧; 最终发送语音前还要用实际回复文本做 LLM 复核.
from contextvars import ContextVar
_round3_parallel_decision: ContextVar[str] = ContextVar(
    "_round3_parallel_decision", default="",
)
_round3_voice_route_snapshot: ContextVar[dict | None] = ContextVar(
    "_round3_voice_route_snapshot", default=None,
)


def _voice_preference_hint(voice_preference: float) -> str:
    """Model-visible label for the stable persona voice setting."""
    voice_preference = max(0.0, min(1.0, float(voice_preference or 0.0)))
    return (
        f"continuous preference {voice_preference:.2f}: higher means the persona is more willing "
        "to use voice for short conversational turns; lower means voice should need clearer "
        "user/context support. Level guide for the classifier: below 0.20 = very low voice willingness "
        "(default to text for neutral greetings, identity answers, thanks, acknowledgements, and ordinary short chat "
        "unless the current user explicitly asks for a voice reply); 0.20-0.39 = low voice willingness "
        "(voice needs clear current-turn support); 0.40-0.69 = balanced voice willingness; "
        "0.70-0.79 = high voice willingness; 0.80+ = strong voice willingness "
        "(strongly favor voice for short conversational replies and brief spoken statuses, unless the current user "
        "asks for text or the final reply is not comfortable to hear because it is too long, dense, structured, "
        "copyable, or revisitable). "
        "This value is evidence for the classifier, not a local automatic threshold."
    )


def _compact_voice_classifier_context(persona: str = "", recent_messages: list | None = None) -> tuple[str, str]:
    """Small persona/recent-context facts for the LLM voice classifier."""
    persona_text = re.sub(r"\s+", " ", str(persona or "")).strip()
    if len(persona_text) > 360:
        persona_text = persona_text[:360].rstrip() + "..."
    if not persona_text:
        persona_text = "(none)"

    recent_lines: list[str] = []
    for item in list(recent_messages or [])[-4:]:
        if isinstance(item, dict):
            role = str(item.get("role") or item.get("speaker") or item.get("name") or "").strip()
            content = str(item.get("content") or item.get("message") or item.get("text") or "").strip()
        else:
            role = str(getattr(item, "role", "") or getattr(item, "speaker", "") or getattr(item, "name", "") or "").strip()
            content = str(getattr(item, "content", "") or getattr(item, "message", "") or getattr(item, "text", "") or "").strip()
        content = re.sub(r"\s+", " ", content)
        if not content:
            continue
        if len(content) > 120:
            content = content[:120].rstrip() + "..."
        recent_lines.append(f"{role or 'message'}: {content}")
    recent_context = "\n".join(recent_lines) if recent_lines else "(none)"
    return persona_text, recent_context


def _parse_voice_classifier_label(raw: str) -> str:
    """Parse the classifier protocol without treating explanatory text as a label."""
    text = str(raw or "").strip().lower()
    match = re.match(r"^(voice|text)\b", text)
    if not match:
        return "unavailable"
    return match.group(1)


def _parse_voice_classifier_json(raw: object) -> str:
    if not isinstance(raw, dict):
        return "unavailable"
    for key in ("delivery", "decision", "mode", "answer"):
        value = raw.get(key)
        parsed = _parse_voice_classifier_label(str(value or ""))
        if parsed in {"voice", "text"}:
            return parsed
    return "unavailable"


async def _decide_voice_with_json_classifier(_llm, sys_msg: str, user_prompt: str) -> str:
    """Ask the LLM for the voice/text route as strict JSON."""
    messages = [
        {
            "role": "system",
            "content": (
                sys_msg
                + "\nReturn strict JSON only: {\"delivery\":\"voice\"} or {\"delivery\":\"text\"}."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]
    try:
        raw = await asyncio.wait_for(
            _llm.chat_json(
                messages,
                reasoning="disabled",
                lite=True,
                metrics_tag="json.voice_delivery_classifier",
            ),
            timeout=_VOICE_CLASSIFIER_TIMEOUT_SEC,
        )
    except Exception:
        log.debug("voice classifier json decision failed", exc_info=True)
        return "unavailable"
    decision = _parse_voice_classifier_json(raw)
    if decision == "unavailable":
        log.debug("voice classifier json returned invalid label %r", raw)
    return decision


def _compact_reply_for_voice_review(text: str, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text or "(empty)"


def _compact_plan_items(values: object, *, limit: int = 6, item_limit: int = 120) -> str:
    if values is None:
        return "(none)"
    if isinstance(values, str):
        items = [values]
    else:
        try:
            items = list(values)  # type: ignore[arg-type]
        except TypeError:
            items = [values]
    lines: list[str] = []
    for item in items[:limit]:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        if len(text) > item_limit:
            text = text[:item_limit].rstrip() + "..."
        lines.append(f"- {text}")
    if not lines:
        return "(none)"
    if len(items) > limit:
        lines.append(f"- ... ({len(items) - limit} more)")
    return "\n".join(lines)


def _plan_items(values: object) -> list:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    try:
        return list(values)  # type: ignore[arg-type]
    except TypeError:
        return [values]


_READABLE_REQUEST_MARKERS = (
    "http", "https://", "www.", "url", "link", "webpage", "website", "page",
    "browser", "browse", "open", "inspect", "read", "check", "verify",
    "validate", "analyze", "analyse", "debug", "log", "file", "document",
    "image", "screenshot", "project", "artifact", "report", "table", "code",
    "网页", "网站", "链接", "浏览", "打开", "查看", "检查", "读取", "读",
    "验证", "分析", "调试", "日志", "文件", "文档", "图片", "截图",
    "项目", "工程", "产物", "报告", "表格", "代码", "清单", "列表", "证据",
)


def _request_visibility_evidence(user_message: str = "") -> str:
    msg = re.sub(r"\s+", " ", str(user_message or "")).strip().lower()
    if not msg:
        return ""
    matched: list[str] = []
    for marker in _READABLE_REQUEST_MARKERS:
        marker_l = marker.lower()
        if marker_l.isascii() and marker_l.replace("_", "").isalnum():
            if re.search(rf"(?<![a-z0-9_]){re.escape(marker_l)}(?![a-z0-9_])", msg):
                matched.append(marker)
        elif marker_l in msg:
            matched.append(marker)
    if matched:
        sample = ", ".join(dict.fromkeys(matched[:6]))
        return (
            "current user message contains material/result terms that often need readable "
            f"follow-up ({sample}); treat this as evidence, not an automatic routing rule"
        )
    return ""


def _compact_plan_projection(
    plan,
    *,
    has_user_facing_files: bool = False,
    user_message: str = "",
) -> tuple[str, str, str, str, str]:
    if not plan:
        request_evidence = _request_visibility_evidence(user_message)
        shape = "no response plan; infer from raw user message and recent context"
        if request_evidence:
            shape += f"; request_visibility_evidence={request_evidence}"
        return "", "(none)", "(none)", "(none)", shape

    tone = re.sub(r"\s+", " ", str(getattr(plan, "tone", "") or "")).strip()
    length_hint = re.sub(r"\s+", " ", str(getattr(plan, "length_hint", "") or "")).strip()
    key_points = _plan_items(getattr(plan, "key_points", None))
    avoid = _plan_items(getattr(plan, "avoid", None))
    deliverables = _plan_items(getattr(plan, "deliverables", None))
    delivery_partial = _plan_items(getattr(plan, "delivery_partial", None))

    key_count = len(key_points)
    deliverable_count = len(deliverables)
    partial_count = len(delivery_partial)
    shape_bits = [
        f"length_hint={length_hint or 'unspecified'}",
        f"key_points={key_count}",
        f"deliverables={deliverable_count}",
        f"partial_deliveries={partial_count}",
        f"user_facing_files={'yes' if has_user_facing_files else 'no'}",
    ]
    if key_count:
        shape_bits.append("round3 is expected to cover the listed key points")
    if deliverable_count or partial_count or has_user_facing_files:
        shape_bits.append("reply may need readable file/status references")

    shape_facts = _project_reply_shape_facts(
        plan,
        has_user_facing_files=has_user_facing_files,
        user_message=user_message,
    )
    predicted_envelope = str(shape_facts.get("predicted_output_envelope") or "").strip()
    if predicted_envelope:
        shape_bits.append(f"predicted_output_envelope={predicted_envelope}")
    request_evidence = str(shape_facts.get("request_visibility_evidence") or "").strip()
    if request_evidence:
        shape_bits.append(f"request_visibility_evidence={request_evidence}")

    return (
        tone,
        _compact_plan_items(key_points),
        _compact_plan_items(avoid),
        _compact_plan_items(deliverables),
        "; ".join(shape_bits),
    )


def _reply_shape_envelope(
    *,
    length_hint: str,
    key_point_count: int,
    deliverable_count: int,
    partial_delivery_count: int,
    has_user_facing_files: bool,
    likely_readable: bool,
    likely_structured: bool,
    likely_multi_sentence: bool,
) -> str:
    """Model-visible final-output envelope; not a local delivery decision."""
    length_l = str(length_hint or "").strip().lower()
    output_items = key_point_count + deliverable_count + partial_delivery_count
    if (
        has_user_facing_files
        or deliverable_count
        or partial_delivery_count
        or likely_structured
    ):
        return "structured_or_revisitable_result"
    if likely_readable:
        return "readable_status_or_evidence_summary"
    if likely_multi_sentence or output_items >= 2 or any(token in length_l for token in ("medium", "long", "中", "长")):
        return "multi_sentence_answer"
    if output_items == 1:
        return "single_fact_short_answer"
    return "short_chat_or_ack"


def _delivery_visibility_evidence(envelope: str) -> str:
    """Model-visible listening/readability evidence, not a local routing rule."""
    if envelope == "structured_or_revisitable_result":
        return (
            "reply is likely to contain structured, file/status, or revisitable details; "
            "voice is a poor prediction for task outcomes, blockers, file/webpage/log statuses, "
            "or details the user needs to read or revisit"
        )
    if envelope == "readable_status_or_evidence_summary":
        return (
            "reply may need readable status or evidence details; task outcomes and blockers should "
            "remain text unless the current user explicitly asks for voice"
        )
    if envelope == "multi_sentence_answer":
        return (
            "reply likely has multiple user-facing facts; compare expected density with listening comfort"
        )
    if envelope == "single_fact_short_answer":
        return "reply likely has one short user-facing fact"
    if envelope == "short_chat_or_ack":
        return "reply likely behaves like short conversational chat"
    return "no plan-derived listening/readability evidence; use current request, previews, and final reply"


def _project_reply_shape_facts(
    plan,
    *,
    has_user_facing_files: bool = False,
    user_message: str = "",
) -> dict[str, object]:
    """Shared facts for route-time prediction and final delivery review."""
    request_evidence = _request_visibility_evidence(user_message)
    if not plan:
        likely_readable = bool(has_user_facing_files or request_evidence)
        envelope = "readable_status_or_evidence_summary" if likely_readable else "unknown_without_plan"
        why = "no response plan; infer from raw user message and final reply"
        if request_evidence:
            why += f"; request_visibility_evidence={request_evidence}"
        return {
            "length_hint": "unspecified",
            "key_point_count": 0,
            "deliverable_count": 0,
            "partial_delivery_count": 0,
            "content_unit_count": 0,
            "has_user_facing_files": bool(has_user_facing_files),
            "likely_readable": likely_readable,
            "likely_structured": False,
            "likely_multi_sentence": False,
            "predicted_output_envelope": envelope,
            "delivery_visibility_evidence": _delivery_visibility_evidence(envelope),
            "request_visibility_evidence": request_evidence,
            "information_boundary": "no response plan; route from raw user message, previews, and final reply",
            "why": why,
        }

    length_hint = re.sub(r"\s+", " ", str(getattr(plan, "length_hint", "") or "")).strip() or "unspecified"
    key_points = _plan_items(getattr(plan, "key_points", None))
    deliverables = _plan_items(getattr(plan, "deliverables", None))
    delivery_partial = _plan_items(getattr(plan, "delivery_partial", None))
    intent = re.sub(r"\s+", " ", str(getattr(plan, "intent", "") or "")).strip()
    joined = " ".join(
        re.sub(r"\s+", " ", str(item or "")).strip()
        for item in [intent, length_hint, *key_points, *deliverables, *delivery_partial]
        if str(item or "").strip()
    ).lower()
    readable_markers = (
        "http", "www.", "url", "link", "file", "filename", "path", "code", "json",
        "csv", "table", "report", "docx", "xlsx", "pdf", "日志", "网页", "文件",
        "链接", "代码", "表格", "报告", "清单", "列表", "状态", "证据", "路径",
        "选项", "对比", "检查", "查看", "验证", "分析", "根因",
    )
    likely_readable = bool(
        has_user_facing_files
        or deliverables
        or delivery_partial
        or request_evidence
        or any(marker in joined for marker in readable_markers)
    )
    likely_structured = bool(
        len(key_points) >= 3
        or deliverables
        or delivery_partial
        or any(marker in joined for marker in ("bullet", "list", "table", "列表", "清单", "表格", "步骤"))
    )
    likely_multi_sentence = bool(
        len(key_points) >= 2
        or likely_structured
        or any(marker in joined for marker in ("long", "详细", "分析", "解释", "对比", "原因", "根因"))
    )
    content_unit_count = len(key_points) + len(deliverables) + len(delivery_partial)
    predicted_output_envelope = _reply_shape_envelope(
        length_hint=length_hint,
        key_point_count=len(key_points),
        deliverable_count=len(deliverables),
        partial_delivery_count=len(delivery_partial),
        has_user_facing_files=has_user_facing_files,
        likely_readable=likely_readable,
        likely_structured=likely_structured,
        likely_multi_sentence=likely_multi_sentence,
    )
    if content_unit_count:
        information_boundary = (
            f"final reply should preserve the same {content_unit_count} planned user-facing "
            "content unit(s); delivery form may change wording, not omit required facts"
        )
    else:
        information_boundary = (
            "final reply has no planned deliverable list; infer content scope from current user message and previews"
        )
    why_bits = [
        f"length_hint={length_hint}",
        f"key_points={len(key_points)}",
        f"deliverables={len(deliverables)}",
        f"partial_deliveries={len(delivery_partial)}",
        f"user_facing_files={'yes' if has_user_facing_files else 'no'}",
        f"content_units={content_unit_count}",
        f"predicted_output_envelope={predicted_output_envelope}",
        f"delivery_visibility_evidence={_delivery_visibility_evidence(predicted_output_envelope)}",
    ]
    if request_evidence:
        why_bits.append(f"request_visibility_evidence={request_evidence}")
    if likely_readable:
        why_bits.append("reply likely benefits from readable/revisitable text")
    if likely_structured:
        why_bits.append("reply likely structured")
    if likely_multi_sentence:
        why_bits.append("reply likely multi-sentence")
    return {
        "length_hint": length_hint,
        "key_point_count": len(key_points),
        "deliverable_count": len(deliverables),
        "partial_delivery_count": len(delivery_partial),
        "content_unit_count": content_unit_count,
        "has_user_facing_files": bool(has_user_facing_files),
        "likely_readable": likely_readable,
        "likely_structured": likely_structured,
        "likely_multi_sentence": likely_multi_sentence,
        "predicted_output_envelope": predicted_output_envelope,
        "delivery_visibility_evidence": _delivery_visibility_evidence(predicted_output_envelope),
        "request_visibility_evidence": request_evidence,
        "information_boundary": information_boundary,
        "why": "; ".join(why_bits),
    }


def _compact_reply_shape_projection(shape: object) -> str:
    if not isinstance(shape, dict):
        return "(none)"
    ordered = [
        "length_hint",
        "key_point_count",
        "deliverable_count",
        "partial_delivery_count",
        "content_unit_count",
        "has_user_facing_files",
        "likely_readable",
        "likely_structured",
        "likely_multi_sentence",
        "predicted_output_envelope",
        "delivery_visibility_evidence",
        "request_visibility_evidence",
        "information_boundary",
        "why",
    ]
    parts: list[str] = []
    for key in ordered:
        if key not in shape:
            continue
        value = shape.get(key)
        if isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = re.sub(r"\s+", " ", str(value or "")).strip()
        if rendered:
            parts.append(f"{key}={rendered}")
    return "; ".join(parts) if parts else "(none)"


def decide_voice_intent_from_user(user_message: str) -> str:
    return "neutral"


def should_keep_round2_tts_tool(user_message: str) -> bool:
    return False


async def decide_voice(
    reply_text: str,
    user_message: str,
    persona: str = "",
    voice_preference: float = 0.0,
) -> VoiceDecision:
    """用 lite 模型判断回复是否应该以语音消息发送。

    Args:
        reply_text: LLM 生成的最终回复文本
        user_message: 用户原始消息
        persona: 人设内容(用于理解角色风格,非必需)

    Returns:
        VoiceDecision with use_voice, voice_text, too_long, reason
    """
    if not reply_text or not reply_text.strip():
        return VoiceDecision(use_voice=False, reason="empty reply")
    
    # 2026-05-16 Round 14b: 三者并行的预判结果先选择 round3 生成侧.
    # 2026-06-13: voice 预判不能直接授权最终发送; 它没有看到最终回复文本.
    # 中间倾向值下, 后置复核用实际回复文本确认 voice/text.
    parallel_decision = _round3_parallel_decision.get()

    voice_preference = max(0.0, min(1.0, float(voice_preference or 0.0)))
    if voice_preference <= 0.0:
        return VoiceDecision(
            use_voice=False,
            reason="voice preference is 0; text reply only",
        )

    estimated_seconds = _estimate_duration(reply_text)
    if estimated_seconds > _MAX_VOICE_SECONDS:
        return VoiceDecision(
            use_voice=False,
            too_long=True,
            reason=f"estimated {estimated_seconds:.0f}s > {_MAX_VOICE_SECONDS}s voice length limit",
        )

    _text = reply_text.strip()

    # Voice/text is decided by the exact 0/1 persona boundary or the Round3
    # LLM classifier. In-between persona preferences and user voice/text
    # wording are evidence for the classifier, not local thresholds.
    # User voice/text wording is intentionally not interpreted here. For
    # 0 < voice_preference < 1, the LLM classifier owns that decision
    # and receives the wording/reply facts as model-visible evidence.
    if voice_preference >= 1.0:
        cleaned = _clean_voice_text(_text)
        if not cleaned.strip():
            return VoiceDecision(use_voice=False, reason="voice text empty after cleanup")
        return VoiceDecision(
            use_voice=True,
            voice_text=cleaned,
            reason="voice preference is 1; voice reply boundary",
        )

    if parallel_decision == "text":
        return VoiceDecision(
            use_voice=False,
            reason="parallel pre-decision: text (lite saw plan/persona/context)",
        )

    if parallel_decision == "unavailable":
        return VoiceDecision(
            use_voice=False,
            reason=(
                "no LLM voice decision available; keeping text reply visible; "
                "voice delivery not authorized "
                f"(est={estimated_seconds:.0f}s, voice_preference={voice_preference:.2f})"
            ),
            llm_decision_available=False,
        )

    if parallel_decision == "voice":
        cleaned = _clean_voice_text(_text)
        if not cleaned.strip():
            return VoiceDecision(use_voice=False, reason="voice text empty after cleanup")
        return VoiceDecision(
            use_voice=True,
            voice_text=cleaned,
            reason="parallel route decision authorized voice",
        )

    # No local persona-threshold decision here. In normal production this
    # function receives `_round3_parallel_decision` from the LLM voice
    # classifier. If that decision is absent, do not synthesize voice locally:
    # the existing text is a visible reply, while voice delivery requires an
    # explicit classifier decision except at exact 0/1 persona boundaries.
    return VoiceDecision(
        use_voice=False,
        reason=(
            "no LLM voice decision available; keeping text reply visible; "
            "voice delivery not authorized "
            f"(est={estimated_seconds:.0f}s, voice_preference={voice_preference:.2f})"
        ),
        llm_decision_available=False,
    )


def _clean_voice_text(text: str) -> str:
    """清洗用于 TTS 朗读的文本。

    去除:
    - 中文全角括号及其内容: （动作描写）/ （心理活动）
    - 英文半角括号及其内容: (action) / (thought)
    - Markdown 标记: **bold**, # heading, * list, ` code, []()
    - URL
    - Preserved: OmniVoice non-verbal tags such as [laughter]/[sigh] and CMU pronunciation brackets such as [B EY1 S]
    """
    return _CleanVoice.run(text)


class _CleanVoice:
    """2026-05-09 Patch 12: 预编译 regex,旧版每次调用都重新编译 ~12 个 pattern。

    短消息(20-200 字)上 lru-cache 不太需要,但 regex compile 本身有 100us 量级开销,
    高 QPS 下会累积。集中到 class 属性一次编译。

    新增 emoji 移除:之前 docstring 承诺了但代码没实现。emoji 念出来是
    "smiley face emoji" 一类的奇怪发音,必须删。

    Unicode 范围参考:
      - U+1F300–U+1F9FF: 大部分 emoji(脸/手/物体/旗帜等)
      - U+2600–U+27BF: 杂项符号(☀️ ⭐ ✨ ❤️ 等)
      - U+1FA70–U+1FAFF: 扩展 emoji
      - U+200D zero-width joiner(组合 emoji 用)
      - U+FE0F variation selector(emoji presentation)
      - 不能误删中文字符(U+4E00-U+9FFF),已避开
    """
    _full_paren = re.compile(r'（[^）]*）')
    _half_paren = re.compile(r'\([^)]*\)')
    _nonverbal_tag = re.compile(
        r'\[(laughter|sigh|confirmation-en|question-en|question-ah|question-oh|question-ei|question-yi|surprise-ah|surprise-oh|surprise-wa|surprise-yo|dissatisfaction-hnn)\]'
    )
    _cmu_pron = re.compile(r'\[([A-Z]{1,3}(?:[0-2])?(?:\s+[A-Z]{1,3}(?:[0-2])?)*)\]')
    _protected_value = re.compile(r'__VOICECTRL_(\d+)__')
    _md_image = re.compile(r'!\[[^\]]*\]\([^)]+\)')
    _md_link = re.compile(r'\[([^\]]+)\]\([^)]+\)')
    _bold_star = re.compile(r'\*\*(.+?)\*\*')
    _bold_under = re.compile(r'__(.+?)__')
    _italic_star = re.compile(r'\*(.+?)\*')
    _italic_under = re.compile(r'_(.+?)_')
    _code_block = re.compile(r'```[^`]*```', re.DOTALL)
    _inline_code = re.compile(r'`([^`]+)`')
    _heading = re.compile(r'^#{1,6}\s*', re.MULTILINE)
    _list_marker = re.compile(r'^[\s]*[-*+]\s+', re.MULTILINE)
    _url = re.compile(r'https?://\S+')
    _empty_full_paren = re.compile(r'（\s*）')
    _empty_half_paren = re.compile(r'\(\s*\)')
    _multi_space = re.compile(r'[ \t\r\f\v]{2,}')
    _multi_newline = re.compile(r'\n{2,}')
    _cjk_period_runs = re.compile(r'。{2,}')
    _cjk_comma_runs = re.compile(r'，{2,}')
    _ellipsis_runs = re.compile(r'(?:…{2,}|\.\.\.+)')
    _ascii_period = re.compile(r'(?<=[一-鿿])\.(?=[一-鿿])')
    _ascii_comma = re.compile(r'(?<=[一-鿿]),(?=[一-鿿])')
    _ascii_question = re.compile(r'(?<=[一-鿿])\?')
    _ascii_exclaim = re.compile(r'(?<=[一-鿿])!')
    _leading_pause = re.compile(r'^[\s。？，、；：,.!?]+')
    _linebreak_after_punct = re.compile(r'([。！？!?；;：:，,、])\s*\n\s*')
    _linebreak_between_cjk = re.compile(r'(?<=[一-鿿])\s*\n\s*(?=[一-鿿])')
    # emoji 范围合集(广覆盖,不动 CJK / ASCII)
    _emoji = re.compile(
        "["
        "\U0001F300-\U0001F9FF"   # symbols & pictographs, transport, regional, etc.
        "\U0001FA70-\U0001FAFF"   # extended-A
        "\U00002600-\U000027BF"   # misc symbols + dingbats
        "\U0001F600-\U0001F64F"   # 表情(emoticons)— 在 1F300-1F9FF 范围内但显式列出
        "\U0001F680-\U0001F6FF"   # transport
        "\U0001F1E6-\U0001F1FF"   # regional indicators (旗帜)
        "\u200d\ufe0f\u20e3"      # ZWJ / VS-16 / keycap combiner
        "]+",
        flags=re.UNICODE,
    )

    @classmethod
    def run(cls, text: str) -> str:
        if not text:
            return ""
        text = unicodedata.normalize("NFKC", text)
        protected: list[str] = []

        def _protect(m: re.Match) -> str:
            protected.append(m.group(0))
            return f"__VOICECTRL_{len(protected) - 1}__"

        text = cls._nonverbal_tag.sub(_protect, text)
        text = cls._cmu_pron.sub(_protect, text)
        # 1. 中文全角括号及内容(角色扮演动作描写)
        text = cls._full_paren.sub('', text)
        # 2. 英文半角括号及内容
        text = cls._half_paren.sub('', text)
        # 3. markdown 图片 ![alt](url)
        text = cls._md_image.sub('', text)
        # 4. markdown 链接 [text](url) → 保留 text
        text = cls._md_link.sub(r'\1', text)
        # 5. bold / italic
        text = cls._bold_star.sub(r'\1', text)
        text = cls._bold_under.sub(r'\1', text)
        text = cls._italic_star.sub(r'\1', text)
        text = cls._italic_under.sub(r'\1', text)
        # 6. 代码块 / 行内代码
        text = cls._code_block.sub('', text)
        text = cls._inline_code.sub(r'\1', text)
        # 7. heading / list 标记
        text = cls._heading.sub('', text)
        text = cls._list_marker.sub('', text)
        # 8. URL
        text = cls._url.sub('', text)
        # 9. Emoji(2026-05-09 新加)
        text = cls._emoji.sub('', text)
        # 10. TTS 断句归一化: 保留句末停顿, 避免换行/ASCII 标点导致中文拆字或连读
        text = text.replace("……", "。")
        text = cls._ellipsis_runs.sub('。', text)
        text = cls._ascii_period.sub('。', text)
        text = cls._ascii_comma.sub('，', text)
        text = cls._ascii_question.sub('？', text)
        text = cls._ascii_exclaim.sub('！', text)
        text = cls._leading_pause.sub('', text)
        text = cls._linebreak_after_punct.sub(r'\1', text)
        text = cls._linebreak_between_cjk.sub('，', text)
        text = text.replace('\n', ' ')
        text = cls._cjk_period_runs.sub('。', text)
        text = cls._cjk_comma_runs.sub('，', text)
        # 11. 空白整理
        text = cls._multi_space.sub(' ', text)
        text = cls._multi_newline.sub('\n', text)
        # 12. 清理被 emoji/括号删空后留下的空括号
        text = cls._empty_full_paren.sub('', text)
        text = cls._empty_half_paren.sub('', text)

        def _restore(m: re.Match) -> str:
            idx = int(m.group(1))
            return protected[idx] if 0 <= idx < len(protected) else ""

        text = cls._protected_value.sub(_restore, text)
        return text.strip()


def _estimate_duration(text: str) -> float:
    """估算文本的语音时长(秒)。"""
    if not text:
        return 0.0
    # 统计中文字符和英文单词
    chinese_chars = len(re.findall(r'[一-鿿]', text))
    # 英文单词: 移除中文后按空格分词
    non_cn = re.sub(r'[一-鿿]', '', text)
    english_words = len(non_cn.split()) if non_cn.strip() else 0
    # 其他字符(标点、数字等)估算为英文单词的 20%
    other_chars = max(0, len(text) - chinese_chars - len(non_cn.replace(' ', '')))
    other_equiv_words = other_chars * 0.15

    zh_seconds = chinese_chars / _CHAR_PER_SECOND_ZH
    en_seconds = (english_words + other_equiv_words) / _WORD_PER_SECOND_EN

    return zh_seconds + en_seconds


def _compact_candidate_output_previews(candidate_previews: object, *, limit: int = 260) -> str:
    """Model-visible early text from Round3 candidates.

    The text candidate is the canonical final reply. The voice candidate is only
    a route-time style probe; it must not be treated as the final content shape.
    """
    if not candidate_previews:
        return "(none; no candidate text was available at route start without waiting)"
    if isinstance(candidate_previews, str):
        text = _compact_reply_for_voice_review(candidate_previews, limit=limit)
        return f"- unknown candidate preview (partial, chars={len(candidate_previews)}): {text}"
    if not isinstance(candidate_previews, dict):
        text = _compact_reply_for_voice_review(str(candidate_previews), limit=limit)
        return f"- unknown candidate preview (partial, chars={len(str(candidate_previews))}): {text}"

    lines: list[str] = []
    for side in ("text", "voice"):
        value = candidate_previews.get(side)
        done_text = ""
        meta_text = ""
        if isinstance(value, dict):
            raw = str(value.get("text") or "").strip()
            raw_chars = value.get("raw_chars")
            visible_chars = value.get("visible_chars")
            done_text = "final" if value.get("done") else "partial"
            truncated_text = ", truncated" if value.get("truncated") else ""
            meta_bits = []
            if isinstance(visible_chars, int):
                meta_bits.append(f"visible_chars={visible_chars}")
            if isinstance(raw_chars, int):
                meta_bits.append(f"raw_chars={raw_chars}")
            meta_bits.append(f"status={done_text}{truncated_text}")
            meta_text = ", ".join(meta_bits)
        else:
            raw = str(value or "").strip()
            done_text = "partial"
            meta_text = f"{done_text}, chars={len(raw)}"
        if raw:
            preview = _compact_reply_for_voice_review(raw, limit=limit)
            if side == "text":
                label = "canonical text candidate preview (final reply content shape)"
            else:
                label = "voice candidate preview (non-canonical style probe)"
            lines.append(f"- {label} ({meta_text}): {preview}")
        else:
            if side == "text":
                label = "canonical text candidate preview"
            else:
                label = "voice candidate preview (non-canonical style probe)"
            lines.append(f"- {label}: unavailable at route start")
    return "\n".join(lines)


async def decide_voice_with_context_lite(
    plan,
    persona: str,
    user_message: str,
    recent_messages: list | None = None,
    voice_preference: float = 0.0,
    has_user_facing_files: bool = False,
    candidate_previews: dict[str, str] | str | None = None,
) -> str:
    """三者并行设计中的决策器: lite 模型看 plan + 人设 + 最近对话, 决定 voice/text.
    
    2026-05-16 Round 14b: 与 round3 文字版/语音版并行启动 — 决策出来后
    cancel 败者. 决策本身用 lite 模型完成, 不用 text/voice 哪边先产出的速度作为决策依据.
    2026-06-13: 决策可读取候选输出的早期文本预览, 让路由侧能看到更接近最终输出的形态事实.
    
    只在 0/1 人设边界本地定死; 用户明确语音/文字意图作为事实交给 lite classifier.
    
    Returns: "voice" / "text" / "unavailable"
    """
    # 1. 设定边界
    voice_preference = max(0.0, min(1.0, float(voice_preference or 0.0)))
    if voice_preference <= 0.0:
        return "text"
    if voice_preference >= 1.0:
        return "voice"
    
    # 2. 中间倾向值 → lite 模型决策
    preference_hint = _voice_preference_hint(voice_preference)
    # 2026-05-17 Round 14f: compact classifier prompt.
    # 2026-06-13: the deadline below is only an availability guard for a
    # broken classifier request, not a "voice generation was slow, choose text"
    # policy. Normal neutral turns wait for the classifier decision before TTS,
    # while canonical Round3 text is already streaming in parallel.
    # Keep classifier input compact, but include persona/recent facts so the
    # LLM can apply character voice preference without local thresholds.
    from app.llm import client as _llm
    
    plan_intent = getattr(plan, "intent", "") if plan else ""
    plan_length = getattr(plan, "length_hint", "") if plan else ""
    plan_tone, plan_key_points, plan_avoid, plan_deliverables, projected_reply_shape = _compact_plan_projection(
        plan,
        has_user_facing_files=has_user_facing_files,
        user_message=user_message,
    )
    projected_reply_shape_facts = _project_reply_shape_facts(
        plan,
        has_user_facing_files=has_user_facing_files,
        user_message=user_message,
    )
    candidate_output_previews = _compact_candidate_output_previews(candidate_previews)
    persona_context, recent_context = _compact_voice_classifier_context(persona, recent_messages)
    
    from app.llm import aux_prompts as _aux
    sys_msg = _aux.VOICE_DELIVERY_CLASSIFIER_SYSTEM
    user_prompt = _aux.VOICE_DELIVERY_CLASSIFIER_USER_TEMPLATE.format(
        plan_intent=plan_intent[:150],
        plan_length=plan_length,
        plan_tone=plan_tone,
        plan_key_points=plan_key_points,
        plan_avoid=plan_avoid,
        plan_deliverables=plan_deliverables,
        projected_reply_shape=projected_reply_shape,
        projected_reply_shape_facts=_compact_reply_shape_projection(projected_reply_shape_facts),
        candidate_output_previews=candidate_output_previews,
        delivery_state="yes" if has_user_facing_files else "no",
        persona_context=persona_context,
        recent_context=recent_context,
        user_message=(user_message or "")[:100],
        voice_preference=voice_preference,
        preference_hint=preference_hint,
    )
    
    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_prompt},
    ]

    # The route classifier runs in parallel with canonical Round3 text, so it
    # does not need a separate low-latency streaming request. A single strict
    # JSON call avoids duplicate provider requests and the stream/json race that
    # made identical route inputs intermittently return unavailable.
    decision = await _decide_voice_with_json_classifier(_llm, sys_msg, user_prompt)
    if decision == "unavailable":
        log.debug(
            "voice classifier unavailable after JSON route request; "
            "no local voice/text fallback"
        )
    return decision
