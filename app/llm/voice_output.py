# -*- coding: utf-8 -*-
"""
语音输出决策 — 用 lite 模型分析回复是否应该通过当前语音输出层发送。

规则(按优先级):
  1. 用户明确要求语音回复 + 预估时长 ≤ 60s → 语音
  2. 用户要求语音回复 + 预估时长 > 60s → 不语音,标记"太长"
  3. "帮我读xxx文件" → 这是文件推送场景,不是语音回复(但最终回复短+口语化仍可语音)
  4. 回复很短(约 ≤20 字中文或 ≤15 词英文)且口语化 → 可语音
  5. 其余 → 不语音（默认发文字）

核心原则：除非用户明确要求，否则只有非常短的闲聊才用语音。
语音消息适合：打招呼、简短确认、闲聊接话。
语音消息不适合：任何需要阅读理解的回复、解释、多句对话。

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

import logging
import re
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 语音时长估算: 中文约 3 字/秒, 英文约 2.5 词/秒
_CHAR_PER_SECOND_ZH = 3.0
_WORD_PER_SECOND_EN = 2.5
_MAX_VOICE_SECONDS = 60


@dataclass
class VoiceDecision:
    use_voice: bool = False
    voice_text: str = ""
    too_long: bool = False
    reason: str = ""


# 2026-05-16 Round 14b: round3 三者并行决策结果, 由 _round3_parallel 设置,
# 后置 decide_voice 读. 避免后置重复 LLM 决策.
from contextvars import ContextVar
_round3_parallel_decision: ContextVar[str] = ContextVar(
    "_round3_parallel_decision", default="",
)


# 2026-05-16 Round 13/14: 提到模块级方便 round3 前置调用
_VOICE_REQUEST_HINTS = (
    "用语音", "语音回", "语音说", "发语音", "说给我听", "讲给我听",
    "voice", "by voice", "speak it",
)
_VOICE_FILE_HINTS = (
    "语音文件", "音频文件", "生成语音", "合成语音", "输出语音", "做个语音",
    "做一段语音", "生成音频", "合成音频", "输出音频", "tts", "audio file",
    "voice file", "generate audio", "synthesize audio", "make an audio",
)
_LONG_READ_HINTS = (
    "朗读", "读出来", "读给我听", "念出来", "念给我听", "全文读", "通篇读",
    "read aloud", "read it out", "read this to me",
)
_VOICE_REPLY_MARKERS = (
    "回复", "回我", "回答", "response", "reply",
    "说给我听", "讲给我听", "by voice", "speak it",
)
_VOICE_REFUSE_HINTS = (
    "文字回复", "用文字", "文字回我", "纯文字", "别用语音", "别语音",
    "不要语音", "不用语音", "不发语音", "不需要语音", "别合成语音",
    "text only", "no voice", "in text", "text reply",
)


def decide_voice_intent_from_user(user_message: str) -> str:
    """仅看用户消息, 判断 voice 意图. 不调 LLM. 在 round3 之前调用,
    让 round3 的 prompt 据此调整文字风格 (短口语 vs 可结构化).

    这里的 "voice" 只表示 round3 最终回复要不要交给当前语音输出层;
    用户说"生成/输出语音文件/音频文件/TTS"是 round2 工具交付任务,不是 round3 语音回复。

    Returns:
        "demand"  - 用户明确要语音回复
        "refuse"  - 用户明确要文字回复,或只要求生成语音/音频文件
        "neutral" - 无明确意图, 由后置 decide_voice 按文本特征判断
    """
    if not user_message:
        return "neutral"
    msg = user_message.lower()
    # refuse 优先级高于 demand (用户同时含两者意图时, "文字回复" 主导)
    for h in _VOICE_REFUSE_HINTS:
        if h in msg:
            return "refuse"
    if any(h in msg for h in _VOICE_FILE_HINTS):
        if not any(h in msg for h in _VOICE_REPLY_MARKERS):
            return "refuse"
    for h in _VOICE_REQUEST_HINTS:
        if h in msg:
            return "demand"
    return "neutral"


def should_keep_round2_tts_tool(user_message: str) -> bool:
    """Round2 是否应暴露 tts 工具。

    True 只表示用户要文件/音频产物或长文本朗读; 普通"语音回复"应交给 Round3 后置语音发送。
    """
    if not user_message:
        return False
    msg = user_message.lower()
    if any(h in msg for h in _VOICE_FILE_HINTS):
        return True
    if any(h in msg for h in _LONG_READ_HINTS):
        return True
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
    
    # 2026-05-16 Round 14b: 三者并行的决策结果优先
    # 如果 _round3_parallel 已经决定 (lite 判断 plan/persona/上下文), 直接采纳,
    # 避免后置再做重复 LLM 决策；这里只保留语音输出 60s 硬上限。
    parallel_decision = _round3_parallel_decision.get()
    if parallel_decision == "text":
        return VoiceDecision(
            use_voice=False,
            reason="parallel pre-decision: text (lite saw plan/persona/context)",
        )

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

    # 2026-05-11 E4 加: 结构化内容直接跳过 lite
    # 代码块/表格/链接/有序列表 — 这些类型用语音念出来体验极差,
    # 不需要 lite 判断,直接走文字。每次 decide_voice 调用是 1-2s lite + 500ms 网络,
    # 几乎每条回复都跑一次 = 累计很大。
    _text = reply_text.strip()
    _STRUCT_SHORTCUTS = (
        # 代码相关
        ("```", "code_block"),
        ("`", "inline_code"),  # 注:` 也匹配 ```,前面优先
        # 表格(markdown / 直接的竖线表)
        ("\n|", "table"),
        # markdown 标题(语音不该读"井号 X")
        ("\n#", "markdown_heading"),
        ("\n##", "markdown_heading"),
        # 列表(超过 3 个 bullet 不适合语音)
    )
    for marker, reason_tag in _STRUCT_SHORTCUTS:
        if marker in _text:
            return VoiceDecision(
                use_voice=False,
                reason=f"text contains structural element ({reason_tag}); voice unsuitable",
            )
    # URL/邮件 — 语音念 https://... 很怪
    if re.search(r"https?://\S+|[\w.+-]+@[\w.-]+\.\w+", _text):
        return VoiceDecision(
            use_voice=False,
            reason="text contains URL/email; voice unsuitable",
        )
    # 多 bullet 列表(3+ 行以 - 或 * 或 数字. 开头)
    bullet_lines = sum(
        1 for ln in _text.splitlines()
        if re.match(r"^\s*([-*•]|\d+[.)、])\s+", ln)
    )
    if bullet_lines >= 3:
        return VoiceDecision(
            use_voice=False,
            reason=f"text has {bullet_lines} bullet items; voice unsuitable",
        )

    # 2026-05-15 Item 4.2: 完全本地决策, 省 1 次 lite (1.5-2s + 成本)。
    # 上面的结构化短路已经过滤了不适合语音的 95% 情况;留下来的都是
    # 纯文本候选。用户明确要求语音或 parallel 已选 voice 时直接采纳;
    # 否则短口语回复自动语音,更长内容交给前置分流决定。
    # 2026-05-16 实测发现 (trace d8888f03):
    # 用户消息 "测试，输出随意内容的语音文件，文字回复" 含双意图:
    #   1. 输出语音文件 (LLM 主动 tts 工具, 作为 deliverable)
    #   2. 文字回复 (不要自动 voice 合成 round3)
    # 之前只检查 user_demands_voice, 没有反向 user_refuses_voice. 结果 round3 文字
    # 被自动 voice 合成, 用户听到语音 — 违反明确指令.
    # (hints 提到模块级 _VOICE_REQUEST_HINTS / _VOICE_REFUSE_HINTS, 同时供
    # decide_voice_intent_from_user 在 round3 前置使用)
    _user_msg_lower = (user_message or "").lower()
    user_refuses_voice = any(h in _user_msg_lower for h in _VOICE_REFUSE_HINTS)
    user_wants_voice_file = decide_voice_intent_from_user(user_message) == "refuse" and any(
        h in _user_msg_lower for h in _VOICE_FILE_HINTS
    )
    if user_refuses_voice or user_wants_voice_file:
        return VoiceDecision(
            use_voice=False,
            reason="user requested text reply or voice/audio file deliverable, not voice reply",
        )

    user_demands_voice = decide_voice_intent_from_user(user_message) == "demand"
    if voice_preference >= 1.0:
        cleaned = _clean_voice_text(_text)
        if not cleaned.strip():
            return VoiceDecision(use_voice=False, reason="voice text empty after cleanup")
        return VoiceDecision(
            use_voice=True,
            voice_text=cleaned,
            reason="voice preference is 1; voice reply unless over 60s",
        )

    if parallel_decision == "voice" and (user_demands_voice or voice_preference >= 0.4):
        cleaned = _clean_voice_text(_text)
        if not cleaned.strip():
            return VoiceDecision(use_voice=False, reason="voice text empty after cleanup")
        return VoiceDecision(
            use_voice=True,
            voice_text=cleaned,
            reason="parallel pre-decision: voice (lite saw plan/persona/context)",
        )
    if parallel_decision == "voice":
        return VoiceDecision(
            use_voice=False,
            reason="parallel pre-decision voice ignored for neutral/low-preference request",
        )

    # 自动语音的本地启发式:未经过前置分流时,只把明显短口语回复转成语音。
    cjk_count = sum(1 for c in _text if 0x4E00 <= ord(c) <= 0x9FFF)
    if cjk_count >= 5:
        is_short = cjk_count <= 25
    else:
        word_count = len(re.findall(r"\b[\w']+\b", _text))
        is_short = word_count <= 18

    if user_demands_voice:
        if estimated_seconds > _MAX_VOICE_SECONDS:
            return VoiceDecision(
                use_voice=False, too_long=True,
                reason=f"user requested voice but text estimates {estimated_seconds:.0f}s > {_MAX_VOICE_SECONDS}s",
            )
        cleaned = _clean_voice_text(_text)
        if not cleaned.strip():
            return VoiceDecision(use_voice=False, reason="voice text empty after cleanup")
        return VoiceDecision(
            use_voice=True,
            voice_text=cleaned,
            reason="user explicitly requested voice",
        )

    if voice_preference >= 0.4 and is_short and estimated_seconds <= _MAX_VOICE_SECONDS:
        cleaned = _clean_voice_text(_text)
        if not cleaned.strip():
            return VoiceDecision(use_voice=False, reason="voice text empty after cleanup")
        return VoiceDecision(
            use_voice=True,
            voice_text=cleaned,
            reason=f"short conversational reply with voice preference {voice_preference:.2f} ({cjk_count} CJK chars)",
        )

    return VoiceDecision(
        use_voice=False,
        reason=f"reply too long or not conversational (cjk={cjk_count}, est={estimated_seconds:.0f}s)",
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


async def decide_voice_with_context_lite(
    plan,
    persona: str,
    user_message: str,
    recent_messages: list | None = None,
    voice_preference: float = 0.0,
) -> str:
    """三者并行设计中的决策器: lite 模型看 plan + 人设 + 最近对话, 决定 voice/text.
    
    2026-05-16 Round 14b: 与 round3 文字版/语音版并行启动 — 决策出来后
    cancel 败者. 决策本身用 lite 几百 ms 完成, 比 round3 渲染快.
    
    优先用规则短路 (用户明确说 "文字回复"/"语音回复"), 不调 LLM, 几 μs.
    其余 neutral 情况调 lite (几百 ms).
    
    Returns: "voice" / "text"
    """
    # 1. 规则短路 — 用户明确意图
    voice_preference = max(0.0, min(1.0, float(voice_preference or 0.0)))
    if voice_preference <= 0.0:
        return "text"
    if voice_preference >= 1.0:
        return "voice"

    rule_intent = decide_voice_intent_from_user(user_message)
    if rule_intent == "demand":
        return "voice"
    if rule_intent == "refuse":
        return "text"
    
    # 2. neutral → lite 模型决策
    if voice_preference >= 0.95:
        preference_hint = "很高:明显偏向语音,除非回复明显长/结构化/不适合朗读"
    elif voice_preference >= 0.7:
        preference_hint = "偏高:同等情况下优先语音,允许稍长口语回复用语音"
    elif voice_preference >= 0.4:
        preference_hint = "中等:语音和文字都可,按内容是否适合朗读决定"
    elif voice_preference >= 0.15:
        preference_hint = "偏低:仅短闲聊或明显适合朗读时语音"
    else:
        preference_hint = "很低:除非用户明确要求或极短闲聊,否则文字"
    # 2026-05-17 Round 14f 修 (实测 trace 120fa615 决策 2s timeout):
    # 旧版 prompt 包 plan/persona/最近 5 条对话 ≈ 1.5KB → deepseek-v4-flash 偶发抖动到 2s+.
    # 同时 round3 text/voice 已 0.5s 完成, 决策慢反而拖累 TTFT.
    # 极简化: 只看 plan.intent + plan.length_hint + user_msg 末 100 char, 总 ≈ 300 字.
    # recent/persona 不太影响 voice/text 选择, 移除.
    from app.llm import client as _llm
    
    plan_intent = getattr(plan, "intent", "") if plan else ""
    plan_length = getattr(plan, "length_hint", "") if plan else ""
    
    from app.llm import aux_prompts as _aux
    sys_msg = _aux.VOICE_DELIVERY_CLASSIFIER_SYSTEM
    user_prompt = _aux.VOICE_DELIVERY_CLASSIFIER_USER_TEMPLATE.format(
        plan_intent=plan_intent[:150],
        plan_length=plan_length,
        user_message=(user_message or "")[:100],
        voice_preference=voice_preference,
        preference_hint=preference_hint,
    )
    
    try:
        # collect 流式 token, 几个字就够 (我们只要一个词)
        # 2026-05-16 显式 aclose: 早停后底层 streaming 可能还在收 token (浪费 API 资源 +
        # 占连接). 用 generator + finally aclose 确保释放.
        toks: list[str] = []
        stream = _llm.chat_stream(
            [{"role": "system", "content": sys_msg},
             {"role": "user", "content": user_prompt}],
            reasoning="disabled", lite=True,
        )
        try:
            async for t in stream:
                toks.append(t)
                if len("".join(toks)) > 24:  # 早停: 一个词就够
                    break
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass
        raw = "".join(toks).strip().lower()
        return "voice" if "voice" in raw else "text"
    except Exception:
        return "text"  # lite 失败默认安全
