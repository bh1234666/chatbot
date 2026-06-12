from __future__ import annotations


def detect_user_language(message: str) -> str:
    if not message or len(message.strip()) < 5:
        return "en"
    cjk_count = 0
    total_letters = 0
    for ch in message:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            cjk_count += 1
            total_letters += 1
        elif 0x3400 <= cp <= 0x4DBF:
            cjk_count += 1
            total_letters += 1
        elif 0x3000 <= cp <= 0x303F:
            cjk_count += 1
            total_letters += 1
        elif ch.isalpha():
            total_letters += 1
    if total_letters == 0:
        return "en"
    cjk_ratio = cjk_count / total_letters
    if cjk_ratio >= 0.30:
        return "zh"
    if cjk_ratio >= 0.10:
        return "mixed"
    return "en"


def language_directive(lang: str) -> str:
    if lang == "zh":
        return (
            "\n\n## Output Language\n"
            "The user's original message is Chinese. User-facing deliverables should be in Chinese:\n"
            "- Final replies and user-visible summaries should be Chinese.\n"
            "- DOCX/PPTX/XLSX content written through the office tool with write or append actions should use Chinese for titles, paragraphs, table headers, and notes.\n"
            "- Filenames may be English, but file content should be Chinese.\n"
            "- Code comments may be Chinese when comments are useful; identifiers follow programming-language conventions.\n"
            "- Visible chart text such as title, xlabel, ylabel, and legend should be Chinese.\n"
            "- Helper prompts should request Chinese only for user-facing outputs, documents, and visible artifact text.\n"
            "Internal planning, helper reports, and helper-to-main coordination may use whichever language best preserves evidence and task clarity.\n"
            "Established technical terms such as LZ77, Huffman, or BWT may remain in their original form.\n"
            "\n"
            "用户是中文场景，面向用户的回复、文档和图表默认中文；内部规划和 helper 交接不限制语言。\n"
            "\n"
            "## Matplotlib Chinese Text\n"
            "When writing Python plotting scripts with Chinese visible text:\n"
            "1. Set Chinese-capable fonts to avoid missing glyph boxes:\n"
            "```python\n"
            "plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'Arial Unicode MS', 'DejaVu Sans']\n"
            "plt.rcParams['axes.unicode_minus'] = False\n"
            "```\n"
            "2. Avoid Unicode superscript/subscript digits; use matplotlib mathtext or plain text.\n"
            "3. Use mathtext for scientific notation, formulas, and Greek letters, such as `r'$\\alpha$'`, `r'$O(n \\log n)$'`, and `r'$10^6$'`.\n"
            "4. For mixed Chinese and mathtext, concatenate strings, such as `'数据规模 ' + r'$10^6$'`.\n"
            "\n"
            "中文绘图需设置中文字体，数学符号优先用 mathtext，避免字体缺字。"
        )
    if lang == "mixed":
        return (
            "\n\n## Output Language\n"
            "The user's original message mixes Chinese and English with Chinese as the main language. Replies and documents "
            "should default to Chinese; technical terms, code-related English, and English phrases from the user may remain in English.\n\n"
            "中英混合且中文为主时，回复和文档默认中文，技术术语和代码相关英文可保留。"
        )
    if lang == "en":
        # 2026-06-10 Round 7: an empty directive let Chinese-default personas
        # answer English users in Chinese (t3-msg-inbox-triage 20260610_163156:
        # English request, Chinese final reply). Mirror the zh rule.
        return (
            "\n\n## Output Language\n"
            "The user's original message is English. User-facing replies, summaries, and document/report content "
            "should be in English. Internal planning, helper reports, and helper-to-main coordination may use "
            "whichever language best preserves evidence and task clarity.\n\n"
            "用户使用英文；面向用户的回复和文档用英文，内部协调语言不限。"
        )
    return ""
