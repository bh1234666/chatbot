"""office docx 数学公式处理:LaTeX 文本 → OMML runs 转换,CJK 检测/清理、\binom 改写、
裸数学标识符判定、花括号组解析、复杂度分类等。

2026-05-20 重构: 从 llm/tools/office.py 原样抽出。经 extract_analysis --closure 验证
自包含(8 函数, 0 unsafe),仅依赖 stdlib(re)。office.py 通过 re-export 保持兼容。
"""
import re


def _latex_contains_cjk(text: str) -> bool:
    return any(0x4E00 <= ord(c) <= 0x9FFF or 0x3400 <= ord(c) <= 0x4DBF for c in text or "")


def _strip_cjk_text_commands(equation: str) -> tuple[str, list[str]]:
    """Remove CJK text from LaTeX math and return notes that should live outside the formula."""
    notes: list[str] = []

    def repl(m: re.Match) -> str:
        body = (m.group(1) or "").strip()
        if body:
            notes.append(body)
        return ""

    cleaned = re.sub(r"\\(?:text|mathrm|mbox)\{([^{}]*[㐀-䶿一-鿿][^{}]*)\}", repl, equation or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, notes


def _rewrite_choose_to_binom(s: str) -> str:
    """重写 `{X \\choose Y}` 为 `\\binom{X}{Y}`。

    2026-05-18 P191: `\\choose` 是 infix 用法, mathtext 不支持 → 之前会失败到
    text fallback 产生 "n choose k" 残骸文字. \\binom 是标准 LaTeX 而且 mathtext
    支持. 用一个手写括号配对 parser (re 不支持递归).

    支持 case (实测 7/7 通过):
      `{n \\choose k}` → `\\binom{n}{k}`
      `{n+1 \\choose 2}` → `\\binom{n+1}{2}`
      `x + {a \\choose b} + y` → `x + \\binom{a}{b} + y`
      `{x_{n} \\choose y_{m}}` → `\\binom{x_{n}}{y_{m}}` (嵌套花括号 ok)
    """
    if "\\choose" not in s:
        return s
    result = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '{':
            # 找配对的 close brace, 跟踪嵌套深度
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if s[j] == '{':
                    depth += 1
                elif s[j] == '}':
                    depth -= 1
                if depth > 0:
                    j += 1
            if depth == 0:
                inner = s[i + 1:j]
                # 在嵌套深度 0 处找 \choose
                d = 0
                split_at = -1
                for k in range(len(inner)):
                    c = inner[k]
                    if c == '{':
                        d += 1
                    elif c == '}':
                        d -= 1
                    elif d == 0 and inner[k:k + 7] == '\\choose':
                        # 确认是命令边界 (后面不能是字母)
                        if k + 7 >= len(inner) or not inner[k + 7].isalpha():
                            split_at = k
                            break
                if split_at >= 0:
                    top = inner[:split_at].strip()
                    bottom = inner[split_at + 7:].strip()
                    # 递归处理 top/bottom (嵌套 \choose 罕见但安全)
                    top = _rewrite_choose_to_binom(top)
                    bottom = _rewrite_choose_to_binom(bottom)
                    result.append(f"\\binom{{{top}}}{{{bottom}}}")
                    i = j + 1
                    continue
                # 内含没有 \choose at depth 0 → 递归 inner 处理嵌套 case
                result.append('{')
                result.append(_rewrite_choose_to_binom(inner))
                result.append('}')
                i = j + 1
                continue
        result.append(s[i])
        i += 1
    return "".join(result)


def _is_broken_after_strip(plain: str, original: str) -> bool:
    """检测 _latex_plain_fallback 是否输出了明显的 LaTeX 命令残骸。

    2026-05-18 P178: _latex_plain_fallback 用 re.sub(r"\\\\([A-Za-z]+)", r"\\1", ...)
    无差别剥离反斜杠, 把 \\begin{vmatrix} 变成 "beginvmatrix", \\displaystyle 变成
    "displaystyle". 然后 _render_text_formula_to_png 把这垃圾渲染成 PNG, 用户看到
    "beginvmatrix 0 & a & b..." 误以为是渲染失败但文档没有任何错误标记。

    2026-05-18 P178 v2: 收紧检测 — 必须**原文确实含该 LaTeX 命令**才判 broken。
    避免假阳性: 普通文本含 "matrix"/"begin" 单词 (例 `x_{\\text{begin}}`) 不应误判。

    2026-05-18 P192: P192 改造 `_latex_plain_fallback` 为白名单制, 未知命令保留 `\\cmd`
    样式留在输出. 这里加新规则: 若 plain 输出还含**任何裸 \\cmd**, 视为渲染失败 (因为
    白名单已覆盖所有应支持的命令, 残留即未知)。

    这里识别这些残骸, 让调用者改走显式失败路径 (返回 None → caller 用 plain-text
    placeholder "[公式渲染失败: ...]" 而不是错误的 PNG)。
    """
    if not plain:
        return True
    # 1. 这些命令: 原文有 `\cmd` 即判 broken (它们在 mathtext 不支持, 剥反斜杠后会留残骸)
    fatal_commands = (
        "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
        "newcommand", "renewcommand", "newcounter",
        "operatorname", "mathop",
        # 2026-05-18 P181: \limits/\nolimits 应已被 normalize 剥离, 但若剥离失败
        # (例如 helper 用了奇怪格式), 也作为 broken 信号
        "limits", "nolimits",
        # 2026-05-18 P183: 这些命令 normalize 会重写 (\implies/\stackrel/\boxed 等),
        # 但若 normalize 失效或老路径, 都作为 broken 信号
        "stackrel", "xrightarrow", "xleftarrow",
        "overbrace", "underbrace", "boxed",
        # \implies / \iff 剥反斜杠后变 "implies"/"iff" 普通单词太常见易误判, 不加
    )
    for tok in fatal_commands:
        if f"\\{tok}" in original and tok in plain.lower():
            return True

    # 2. \begin{...} / \end{...} 任何环境都不支持 (mathtext 不支持环境语法)
    if "\\begin{" in original or "\\end{" in original:
        return True
    # \begin / \end 无 brace 形式
    if re.search(r"\\(begin|end)(?![A-Za-z])", original):
        return True

    # 3. 老式 LaTeX 环境命令直接调用 (\matrix, \align, \cases 等)
    legacy_env_cmds = ("align", "cases", "gather", "split", "array",
                       "matrix", "vmatrix", "pmatrix", "bmatrix", "Bmatrix")
    for tok in legacy_env_cmds:
        if re.search(rf"\\{tok}(?![A-Za-z])", original):
            return True

    # 4. 2026-05-18 P192: plain 输出还含任何**裸 \cmd** → 渲染失败
    # P192 白名单 _latex_plain_fallback 不再无差别剥反斜杠, 已知命令转 Unicode/保留内容,
    # 未知命令保留原样 `\cmd`. 残留 = 未知命令 → 视为渲染失败 (避免输出 `\unknown` 字面量
    # 给用户看)。Exception: `\\` (反斜杠转空格已经处理过) 不计。
    if re.search(r"\\[A-Za-z]+", plain):
        return True

    return False


def _looks_like_bare_math_identifier(token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\s*=\s*[-+]?\d+(?:\.\d+)?(?:\s*[A-Za-z]+)?)?", t):
        return False
    letters = re.sub(r"[^A-Za-z]", "", t).lower()
    common_words = {
        "a", "an", "and", "or", "to", "in", "on", "by", "for", "with", "from", "the", "is", "are",
        "hz", "baud", "db", "ms", "sec", "s", "type", "baseband", "signal", "spectrum",
    }
    return letters not in common_words


def _read_brace_group(s: str, start: int) -> tuple[str, int] | None:
    """若 s[start] == '{',返回 (大括号内内容, 闭合括号后的索引)。否则返回 None。

    支持嵌套 (依据括号层级匹配),也跳过 \\{ 转义。
    """
    if start >= len(s) or s[start] != '{':
        return None
    depth = 1
    i = start + 1
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            i += 2  # 跳过转义字符 (例如 \{ \})
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return s[start + 1:i], i + 1
        i += 1
    return None  # 不匹配


def _latex_text_to_omml_runs(text: str) -> str:
    """把简单 LaTeX 文本(无 frac/sqrt) 转 OMML runs, 包含 ^{} 和 _{} 结构."""
    def _esc(t):
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    out = []
    s = text
    i = 0
    n = len(s)
    # 暂存非上下标部分到 buffer
    buf = []

    def _flush():
        if buf:
            out.append(f"<m:r><m:t>{_esc(''.join(buf))}</m:t></m:r>")
            buf.clear()

    while i < n:
        c = s[i]
        if c == "^" or c == "_":
            # 上下标: 取出前一个字符作为 base
            tag = "sSup" if c == "^" else "sSub"
            sub_tag = "sup" if c == "^" else "sub"
            if not buf:
                # 没有 base, 跳过
                i += 1
                continue
            base_char = buf.pop()
            _flush()  # 先输出 base 前的部分

            # 取上下标内容
            if i + 1 >= n:
                buf.append(c)
                i += 1
                continue
            if s[i+1] == "{":
                end = s.find("}", i+2)
                if end == -1:
                    buf.append(c)
                    i += 1
                    continue
                content = s[i+2:end]
                i = end + 1
            else:
                content = s[i+1]
                i += 2

            out.append(
                f"<m:{tag}><m:e><m:r><m:t>{_esc(base_char)}</m:t></m:r></m:e>"
                f"<m:{sub_tag}><m:r><m:t>{_esc(content)}</m:t></m:r></m:{sub_tag}>"
                f"</m:{tag}>"
            )
        elif c == "{" or c == "}":
            i += 1  # 透明跳过
        else:
            buf.append(c)
            i += 1

    _flush()
    return "".join(out)


def _classify_latex_complexity(latex: str) -> tuple[str, str]:
    """2026-05-17 P160: 预筛 LaTeX 复杂度,给清晰错误而非默默落到裸文本兜底。

    返回 (level, hint):
      level: "ok" | "png_likely" | "unsupported"
      hint: 若不是 ok,说明为什么 + 建议

    "unsupported" 的会得到清晰错误,不会假装"渲染成功"实际产出乱码。
    """
    s = latex
    # 真正不支持的环境
    # 2026-05-18 P204: cases/pmatrix/bmatrix/vmatrix/matrix/array 现已支持
    # (走 _render_matrix_env_to_png 专用渲染), 从黑名单移除.
    for env in ("\\begin{align}", "\\begin{align*}", "\\begin{aligned}",
                "\\begin{gather}", "\\begin{multline}", "\\begin{equation}"):
        if env in s:
            env_name = env[7:-1]  # "begin{align}" → "align"
            return "unsupported", (
                f"\\begin{{{env_name}}} 环境本工具不支持。"
                f"建议: 拆成多个 equation block (每个独立的 $...$); "
                f"或者把对齐用 prose 句子表达。"
            )
    # \\text{中文} — mathtext 不会渲染中文, 直接报错避免乱码
    if "\\text{" in s:
        # 探测花括号内是否含 ascii 外字符
        m = re.search(r"\\text\{([^{}]*)\}", s)
        if m and any(ord(c) > 127 for c in m.group(1)):
            return "unsupported", (
                "\\text{...} 含非 ASCII 字符。mathtext 无法渲染中文公式标注, "
                "Word OMML 也不支持 \\text。"
                "建议: 把文本注释挪到公式外的 paragraph 文字里, "
                "公式只放数学符号。"
            )
    # 自定义命令 / 环境扩展
    if "\\newcommand" in s or "\\renewcommand" in s or "\\def\\" in s:
        return "unsupported", (
            "\\newcommand / \\def 不支持。"
            "请在 LaTeX 源里把宏展开, 直接写最终公式。"
        )
    return "ok", ""
