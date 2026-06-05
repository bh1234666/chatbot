"""
记忆内容消毒。

温/冷记忆条目最终会以 system 段或 [SYSTEM_MEMORY_INJECTION] user 消息形式
回到 LLM 上下文。如果其中混入指令性语言、Markdown 代码块、URL 等，下次
LLM 可能错误执行。我们做两道防线：
  1. 生成 prompt 中明确要求"第三人称陈述、禁止指令、禁止 URL、禁止代码块"
  2. 生成后程序级消毒（本模块），剥离明显的危险模式

消毒不追求 100%——LLM 能 prompt-injection 的方式很多——但能挡住绝大多数
意外注入的"指令式语句"和"伪装命令"。
"""
from __future__ import annotations
import re


# 命令式句首词(中英),被检测到则削弱
# Patch 15 (2026-05-02): 增加角色扮演式注入识别(原版只覆盖"请帮我/忽略/from now on"等,
# 漏掉"你扮演 X / 假装你是 Y / 角色:Z / act as / pretend / system:"等 prompt 注入常见模式)
_IMPERATIVE_PREFIXES = re.compile(
    r"^\s*(?:"
    # ── 中文:覆盖式 ──
    r"请|帮我|帮忙|你需要|你必须|"
    r"忽略[^,,\s]{0,12}(?:指令|提示|prompt|规则|要求|约定|系统|设定|人设)|"
    r"忘记[^,,\s]{0,12}(?:指令|提示|prompt|规则|要求|设定|人设|身份)|"
    r"不再[^,,\s]{0,12}(?:遵守|遵循|按照|使用|是)|"
    r"切换[^,,\s]{0,12}(?:角色|人设|身份|模式|系统)|"
    r"重置[^,,\s]{0,12}(?:角色|人设|系统|提示|对话|context)|"
    r"从现在起|"
    # ── 中文:角色扮演式(新增) ──
    r"你?现在[^,,\s]{0,12}(?:是|扮演|代表|担任|开始)|"
    r"你?(?:扮演|要扮演|开始扮演|来扮演|从现在起?是)|"
    r"假(?:装|设|如)\s*你?(?:是|扮演)|"
    r"你的?(?:新|新的)?(?:角色|人设|身份|名字|设定)\s*[:：是为]|"
    r"(?:角色|人设|身份|设定)\s*[:：]|"
    # ── 英文:覆盖式(扩展) ──
    r"please\s+|"
    r"you\s+(?:must|need\s+to|should|are\s+now|will\s+now)|"
    r"(?:from\s+now\s+on|ignore|disregard|forget)[\s,]|"
    # ── 英文:角色扮演式(新增) ──
    r"(?:act|pretend|roleplay|imagine\s+you)\s+(?:as|like|to\s+be)?|"
    r"(?:system|user|assistant)\s*[:：]|"
    r"new\s+(?:role|persona|character|instructions?)|"
    r"override\s+(?:your\s+)?(?:role|instructions?|prompt|persona)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Markdown 代码块
_FENCE = re.compile(r"```[\s\S]*?```")

# URL(保守:http/https + 裸域名)
# Patch 15 (2026-05-02): TLD 列表从 6 个扩到 ~40 个,覆盖常见新顶级域(.dev/.app/.co/.gov 等)
# 和国别域名(.uk/.de/.com.cn 等),防止"evil.dev/payload" 这种 URL 漏过 sanitize。
# 注意:裸域名分支的 \S* 不能贪婪吃后续内容,否则像 "report.com.txt" 这种文件名
# 会被整体替换掉、连带吃掉右括号和后面文字。这里改为:
#   - 完整 URL:必须有 http(s):// schema
#   - 裸域名:仅当后面紧跟空白、字符串结尾,或常见标点(中英括号/标点)时才匹配,
#     不允许后面紧跟 `.<word>` 这种文件扩展名形态。
_URL = re.compile(
    # http(s) URL — 显式 schema,不依赖 \b
    r"https?://\S+"
    r"|"
    # 裸域名 — 用 lookbehind 显式排除前面是 ascii 字母数字/连字符的情况,
    # 避免和 \b 在 Unicode (Chinese is word char) 下失效的问题。
    # 中文上下文里 "中文google.com" 现在能匹配到 google.com 了。
    r"(?<![a-zA-Z0-9-])"
    r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.(?:"
    # 通用顶级域
    r"com|net|org|edu|gov|mil|info|biz|"
    # 国家代码(子级 com.cn / co.uk 等放前面优先匹配)
    r"com\.cn|net\.cn|org\.cn|gov\.cn|edu\.cn|com\.hk|com\.tw|co\.uk|co\.jp|"
    r"cn|hk|tw|jp|kr|sg|us|uk|de|fr|ru|au|ca|in|br|"
    # 流行新顶级域
    r"io|ai|app|dev|co|me|tv|cc|xyz|online|site|tech|store|club|live|today|news|blog"
    r")"
    r"(?:/[^\s）)」』】>]*)?"
    r"(?=$|[\s）)」』】>，。；：!?,;:])"
)


def sanitize_memory_text(text: str, *, max_len: int = 500) -> str:
    """
    清理记忆字段（headline/summary/internal_hint）。

    1. 去除 markdown 代码块（仅保留占位说明）
    2. 替换 URL 为占位
    3. 在命令式句首插入"用户曾说："让其失去命令效力
    4. 折行、截断

    2026-05-11 F2/F3 修:
      - _disarm 不再用 "（用户曾表达：" 这种永不闭合的开括号——多次匹配会留下
        嵌套未闭合括号让模型困惑。改用前后对称的方括号标记 "[用户语:...]"。
      - URL 正则 \\b 在中文边界失效(中文是 word char),改用 lookbehind/lookahead
        显式排除前后是 ascii 字母数字的情况。
    """
    s = text.strip()

    # 代码块替换为简短占位
    s = _FENCE.sub("[代码片段]", s)

    # URL 替换
    s = _URL.sub("[链接]", s)

    # 命令式句首加防护前缀（让指令变成"用户曾表达"的引述）
    # 用对称方括号,不会留下未闭合标点
    def _disarm(m: re.Match) -> str:
        return "[用户曾表达]" + m.group(0)
    s = _IMPERATIVE_PREFIXES.sub(_disarm, s)

    # 折叠空白
    s = re.sub(r"\s+", " ", s).strip()

    # 截断
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def sanitize_headline(text: str) -> str:
    return sanitize_memory_text(text, max_len=40)


def sanitize_summary(text: str) -> str:
    return sanitize_memory_text(text, max_len=400)


def sanitize_hint(text: str) -> str:
    return sanitize_memory_text(text, max_len=120)


def sanitize_narration(text: str) -> str:
    """群组事件转写。同 summary 但更短。"""
    return sanitize_memory_text(text, max_len=200)