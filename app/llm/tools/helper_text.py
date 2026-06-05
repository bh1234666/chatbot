"""helper 相关纯文本工具:语言提示、验证失败摘录、prompt 相似度、从 prompt 推断预期产出文件,
以及配套常量(产物扩展名 / 中英产出动词 / 模板名)。

2026-05-20 重构: 从 llm/tools/delegate.py 原样抽出。经 extract_analysis --closure 验证
自包含(8 符号含4常量, 0 unsafe),无任何 import 依赖。delegate.py 通过 re-export 兼容。
"""


# 产出/生成动词(中英) — 文件名出现在这些动词附近即视为产物
_PRODUCE_VERBS_CN = (
    "生成", "产出", "写入", "保存", "创建", "输出", "导出",
    "完成", "得到", "返回", "提供", "做出", "写出", "存到",
    "写一", "写个", "做一", "做个", "建一", "建个",
)


_PRODUCE_VERBS_EN = (
    "produce", "generate", "create", "output", "save", "write",
    "export", "build", "make", "emit",
)


# 文件名扩展名(白名单)
_PRODUCT_FILE_EXTS = (
    "csv", "tsv", "json", "jsonl",
    "xlsx", "xls", "xlsm", "docx", "pdf", "pptx",
    "png", "jpg", "jpeg", "svg",
    "c", "cpp", "h", "py", "ts", "js", "go", "rs",
    "md", "html", "txt", "yaml", "yml",
    "exe", "o", "a", "so", "dll",
)


# 模板/占位文件名(不算具体产物)
_TEMPLATE_NAMES = {
    "xxx.csv", "data.csv", "output.csv", "result.csv", "results.csv",
    "chart.png", "image.png", "fig.png", "file.txt", "input.txt",
    "common.h", "main.c", "test.c", "test.py", "example.py",
    "your_file.csv", "filename.txt", "out.txt",
}


def _infer_expected_outputs_from_prompt(prompt: str) -> list[str]:
    """从主线程 prompt 自动推断这个 helper 应该产出哪些文件。

    保守原则: 宁可不推断(空列表), 也不要 false positive 列出 helper 不会产的文件。

    匹配模式:
    - "生成 paper.docx" → paper.docx
    - "保存为 results_avl.csv" → results_avl.csv
    - "produce chart1.png" → chart1.png
    - "写一个 bench_skiplist.c" → bench_skiplist.c
    - 多产物: "生成 paper.docx, report.pptx 和 results.xlsx"

    返回去重后的文件列表(最多 20 个)。
    """
    if not prompt:
        return []

    import re as _re
    found: list[str] = []
    seen: set[str] = set()

    def _add_output(value: str) -> None:
        candidate = str(value or "").strip().strip("`'\".,;:()[]{}")
        candidate = candidate.replace("\\", "/").lstrip("./")
        if not candidate:
            return
        basename = candidate.rsplit("/", 1)[-1].lower()
        if basename in _TEMPLATE_NAMES:
            return
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    # 模式: 产出动词 + 任何字符 (≤ 30 个) + 文件名
    # 文件名定义: [a-zA-Z][\w\-]*\.<ext>
    _filename_re = _re.compile(
        r"(?<![\w./-])((?:_env/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z][\w.-]{0,80}\.(?:"
        + "|".join(_PRODUCT_FILE_EXTS) + r"))\b"
    )

    # 找所有产出动词出现的位置 + 之后 50 字符内的文件名
    all_verbs = list(_PRODUCE_VERBS_CN) + list(_PRODUCE_VERBS_EN)
    verb_positions = []
    plow = prompt
    for v in all_verbs:
        idx = 0
        while True:
            pos = plow.find(v, idx)
            if pos < 0:
                break
            verb_positions.append(pos + len(v))
            idx = pos + len(v)

    # 在每个动词后的 100 字符窗口内找文件名
    for vp in verb_positions:
        window = prompt[vp:vp + 100]
        for m in _filename_re.finditer(window):
            _add_output(m.group(1))

    # 另一种模式: prompt 末尾或独立行的文件名(命令式总结)
    # 如 prompt 含 "应当产出 paper.docx" 或最后一段提到"输出文件:\n  paper.docx"
    # 简化: 看 prompt 最后 200 字符内是否独占一行的文件名
    tail = prompt[-200:]
    # 独立行/markdown 列表里的文件名 (- foo.csv 或 * foo.csv)
    for m in _re.finditer(
        r"(?:^|\n)\s*(?:[-*•]\s+|输出文件|产出|产物|deliverables?[:.]?)\s*[`']?([a-zA-Z][\w\-]+\.(?:" +
        "|".join(_PRODUCT_FILE_EXTS) + r"))[`']?",
        tail, _re.IGNORECASE | _re.MULTILINE
    ):
        _add_output(m.group(1))

    tail_path_re = _re.compile(
        r"(?<![\w./-])((?:_env/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z][\w.-]{0,80}\.(?:"
        + "|".join(_PRODUCT_FILE_EXTS) + r"))\b",
        _re.IGNORECASE,
    )
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*", "+")) or "deliverable" in stripped.lower():
            for m in tail_path_re.finditer(stripped):
                _add_output(m.group(1))

    return found[:20]  # 上限 20 个


def _helper_lang_hint(user_lang: str) -> str:
    """根据用户原 message 语言返回 helper system prompt 末尾的语言硬约束。

    2026-05-02 part12 (Bug C):trace 74b1295b 实测 helper 调 office.write 写
    docx/pptx/xlsx 默认走英文学术风,即使主线程模型在中文用户场景。
    给 helper 显式注入语言约束。
    """
    if user_lang == "zh":
        return (
            "\n\n"
            "## Output Language\n"
            "The user's original message is Chinese. User-facing deliverables should be in Chinese:\n"
            "- DOCX/PPTX/XLSX content written through office tools: Chinese titles, paragraphs, table headers, and notes.\n"
            "- Filenames may remain English, but file content should be Chinese.\n"
            "- Visible chart text such as titles and axis labels should be Chinese.\n"
            "- Code comments may be Chinese when comments are useful.\n"
            "Internal helper reports and coordination notes may use whichever language best preserves evidence and task clarity.\n"
            "Keep established technical terms in their original form when appropriate, such as LZ77, Huffman, or BWT.\n"
            "\n"
            "用户是中文场景，面向用户的报告、文档和图表默认中文；内部报告和交接不限制语言。\n"
            "\n"
            "## Matplotlib Chinese Text\n"
            "When writing plotting scripts with Chinese visible text:\n"
            "1. Set Chinese-capable fonts: `plt.rcParams['font.sans-serif']="
            "['Microsoft YaHei', 'SimHei', 'Arial Unicode MS', 'DejaVu Sans']` "
            "+ `plt.rcParams['axes.unicode_minus'] = False`\n"
            "2. Avoid Unicode superscript/subscript digits in visible labels; use mathtext such as `r'n=$10^6$'` or plain `n=10^6`.\n"
            "3. Use mathtext for scientific notation and mathematical symbols, such as `r'$\\alpha$'` and `r'$O(n \\log n)$'`.\n"
            "\n"
            "中文绘图需设置中文字体，数学符号优先用 mathtext，避免字体缺字。"
        )
    if user_lang == "mixed":
        return (
            "\n\n## Output Language\n"
            "The user's original message mixes Chinese and English with Chinese as the main language. Reports and documents "
            "should default to Chinese, while technical terms and code-related English may remain in English.\n\n"
            "中英混合且中文为主时，汇报和文档默认中文，技术术语保留原文。\n"
        )
    return ""


def _extract_verify_fail_excerpt(report: str, max_chars: int = 800) -> str:
    """2026-05-09 Patch 48:从 verify 报告中提取 FAIL/PARTIAL 项摘要。

    verify 报告格式(P46-C 强制):
      VERDICT: FAIL
      ## 验证项 1: ...
      判决: FAIL
      命令: ...
      输出: ...
      理由: ...
      ## 验证项 2: ...
      判决: PASS
      ...

    本函数:
      - 找到所有"## 验证项 N: ..." 块,挑出"判决: FAIL"或"判决: PARTIAL"的
      - 拼接(每项含验证项标题、命令、输出、理由),总长不超过 max_chars
      - 没找到 FAIL 块时,fallback 取报告 ## 总结 段或前 max_chars 字符
      - 极简实现:不搞复杂正则,行级扫描即可

    用于 P48 的 repair_recommendation,让修复 helper 知道具体修哪几项。
    """
    if not report:
        return "(verify 报告为空)"

    lines = report.splitlines()
    fail_blocks: list[str] = []
    cur_block_lines: list[str] = []
    cur_in_fail = False  # 当前是否在 FAIL/PARTIAL 块内
    cur_has_judge = False  # 当前块是否已经看到判决行(用于跳过 PASS 块的剩余内容)

    def _flush():
        if cur_in_fail and cur_block_lines:
            fail_blocks.append("\n".join(cur_block_lines).strip())

    for line in lines:
        ls = line.strip()
        if ls.startswith("## 验证项") or ls.startswith("## 总结"):
            # 新块开始,先把上一块按是否 FAIL 决定保留
            _flush()
            cur_block_lines = [line]
            cur_in_fail = False
            cur_has_judge = False
            # 总结段也保留(含整体 FAIL 信息)
            if ls.startswith("## 总结"):
                cur_in_fail = True
            continue

        if ls.startswith("判决:") and not cur_has_judge:
            cur_has_judge = True
            _judge = ls[len("判决:"):].strip().upper()
            if "FAIL" in _judge or "PARTIAL" in _judge:
                cur_in_fail = True
            cur_block_lines.append(line)
            continue

        # 一般行:加入当前块
        cur_block_lines.append(line)

    _flush()  # flush 最后一块

    if not fail_blocks:
        # fallback:没找到 FAIL 块结构,直接截取整个报告
        return report[:max_chars] + ("..." if len(report) > max_chars else "")

    # 拼接,控总长
    out = ""
    for blk in fail_blocks:
        if len(out) + len(blk) + 2 > max_chars:
            remain = max_chars - len(out) - 4
            if remain > 0:
                out += blk[:remain] + "...\n"
            break
        out += blk + "\n\n"

    return out.strip() or report[:max_chars]


# L10-1 (2026-05-09): trigram 相似度 — 检测"换 task_id 重做相同的事"
def _prompt_similarity(a: str, b: str) -> float:
    """简单相似度:取共同 char-trigram 占比。"""
    if not a or not b:
        return 0.0
    def _trigrams(s: str) -> set:
        s = s.lower()
        return {s[i:i+3] for i in range(len(s) - 2)}
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
