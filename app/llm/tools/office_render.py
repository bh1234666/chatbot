"""office LaTeX 文本渲染/预处理:归一化、简单宏预处理、上下标应用、unicode 渲染、
纯文本回退、latex→OMML 简化等 + 希腊字母/算符常量。

2026-05-20 重构: 从 llm/tools/office.py 原样抽出(13 符号, 0 unsafe)。复用 office_latex
(_rewrite_choose_to_binom/_read_brace_group/_latex_text_to_omml_runs/_is_broken_after_strip)
与 office_pptx(_UNICODE_SUB/SUPERSCRIPT/_script_text);依赖链 render→latex,render→pptx 无环。
"""
from __future__ import annotations

import re

from app.llm.tools.office_latex import (_is_broken_after_strip, _latex_text_to_omml_runs, _read_brace_group, _rewrite_choose_to_binom)
from app.llm.tools.office_pptx import (_UNICODE_SUBSCRIPT, _UNICODE_SUPERSCRIPT, _script_text)


def _normalize_latex_for_render(equation: str) -> str:
    s = (equation or "").strip()
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = re.sub(r"\\(i{1,2})?int(?=\d|[A-Za-z])", lambda m: f"\\{m.group(1) or ''}int_", s)
    s = re.sub(r"\\(sin|cos|tan|log|ln|exp)(?=\^|_)", lambda m: f"\\{m.group(1)}", s)
    s = s.replace("\\left", "").replace("\\right", "")
    s = s.replace("\\Bigl", "").replace("\\Bigr", "").replace("\\bigl", "").replace("\\bigr", "")
    # 2026-05-18 P180: mathtext 不支持 \displaystyle/\textstyle/\scriptstyle 但删了不改语义
    # 2026-05-18 P181 fix: 用 (?![A-Za-z]) 替代 \b — 因为 `\displaystyle_{n=1}` 中
    # s 和 _ 都是 \w 字符, \b 失败 (没有 word→non-word 跳变)。
    s = re.sub(r"\\(displaystyle|textstyle|scriptstyle|scriptscriptstyle)(?![A-Za-z])", "", s)
    # 2026-05-18 P181: \limits/\nolimits 也是 mathtext 不支持但语义无关的修饰符。
    # \limits 在中文数学教材中常见 (Chinese textbook style), 强制 lim/sum/int 的下标
    # 居中 (mathtext 默认就是居中, 所以剥了等价)。
    s = re.sub(r"\\(limits|nolimits|mathstrut|mathchoice)(?![A-Za-z])", "", s)
    # 2026-05-18 P183: 把 mathtext 不支持的常见 LaTeX 命令改写成支持的等价形式
    # 实测 mathtext 不支持但可以改写的命令清单 (用 matplotlib mathtext audit 跑出来):
    #   \implies      → \Rightarrow      (relation)
    #   \iff          → \Leftrightarrow  (relation)
    #   \xrightarrow{...} → \to          (上方文字丢失但箭头保留, 是合理 trade-off)
    #   \xleftarrow{...}  → \leftarrow
    #   \stackrel{a}{b}   → \overset{a}{b}   (overset mathtext 支持)
    #   \underline{x}     → \bar{x}     (顶替 underline, mathtext 不支持 underline)
    #   \overbrace{x}     → x           (drop 装饰, 保留内容)
    #   \underbrace{x}    → x
    #   \boxed{x}         → x           (drop 框, 保留内容)
    # 之前这些都让 mathtext 失败 → 走 text fallback → 产出残骸 PNG。
    s = re.sub(r"\\implies(?![A-Za-z])", r"\\Rightarrow", s)
    s = re.sub(r"\\iff(?![A-Za-z])", r"\\Leftrightarrow", s)
    s = re.sub(r"\\xrightarrow\{[^{}]*\}", r"\\to", s)
    s = re.sub(r"\\xleftarrow\{[^{}]*\}", r"\\leftarrow", s)
    s = re.sub(r"\\stackrel\{([^{}]+)\}\{([^{}]+)\}", r"\\overset{\1}{\2}", s)
    s = re.sub(r"\\underline\{([^{}]+)\}", r"\\bar{\1}", s)
    s = re.sub(r"\\overbrace\{([^{}]+)\}", r"\1", s)
    s = re.sub(r"\\underbrace\{([^{}]+)\}", r"\1", s)
    s = re.sub(r"\\boxed\{([^{}]+)\}", r"\1", s)
    # 2026-05-18 P191: {X \choose Y} → \binom{X}{Y} (mathtext 不支持 infix \choose)
    s = _rewrite_choose_to_binom(s)
    return s


def _render_inline_latex_to_text_only(text: str) -> str:
    """Render inline `$...$` LaTeX to Unicode-only text (no PNG fallback).

    2026-05-18 P184: 用在 heading/list 等不适合嵌入 image 的场景。
    `$...$` → 优先 `_try_unicode_render` 转 Unicode (10^4 → 10⁴);
    失败 → `_latex_plain_fallback` 文字; 若 P178 检测残骸 → 标 `[⚠️ ...]`。
    """
    parts = re.split(r"(\$[^$\n]+\$)", text)
    out: list[str] = []
    for part in parts:
        if part.startswith("$") and part.endswith("$") and len(part) > 2:
            eq = part[1:-1].strip()
            uni = _try_unicode_render(eq)
            if uni is not None:
                out.append(uni)
            else:
                fallback = _latex_plain_fallback(eq)
                if _is_broken_after_strip(fallback, eq):
                    out.append(f"[⚠️ {eq}]")
                else:
                    out.append(fallback)
        else:
            out.append(part)
    return "".join(out)


_GREEK_LETTERS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}


_SIMPLE_OPS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "equiv": "≡",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "infty": "∞",
}


def _preprocess_simple_latex_macros(eq: str) -> str:
    """把简单 LaTeX 宏预处理成 Unicode 友好形式, 让 _try_unicode_render 能接受。

    支持:
    - \\frac{a}{b}    → (a)/(b)   单字符时简化 a/b
    - \\sqrt{x}       → √(x)       单字符 √x
    - \\sqrt[n]{x}    → ⁿ√(x)
    - \\sum_{i}^{n}   → Σ (lossy 但比 PNG 强)
    - \\prod          → ∏
    - \\int           → ∫
    - \\partial       → ∂
    - \\nabla         → ∇
    - {X \\choose Y}  → \\binom{X}{Y} → (X)/(Y) (lossy but readable)

    不能处理的(嵌套 frac、复杂极限) 不动, 让外层 _try_unicode_render 退回 None。
    """
    s = eq
    # 2026-05-18 P191: \choose 重写 (在 \frac 处理前完成, 因为 \binom 可以走 frac 路径)
    if "\\choose" in s:
        s = _rewrite_choose_to_binom(s)
        # \binom{a}{b} 在 _CONSTS 里没有, 但视觉上等价于分式, 走 frac 路径
        s = re.sub(r"\\binom\{([^{}]+?)\}\{([^{}]+?)\}", r"\\frac{\1}{\2}", s)

    # \frac{a}{b} - 处理简单的不嵌套分式
    def _replace_frac(match):
        a = match.group(1).strip()
        b = match.group(2).strip()
        # 嵌套 frac → 放弃, 保留原文(后面会走 PNG)
        if "\\frac" in a or "\\frac" in b or "\\sqrt" in a or "\\sqrt" in b:
            return match.group(0)
        # 单字符可省括号
        if len(a) == 1 and len(b) == 1:
            return f"{a}/{b}"
        return f"({a})/({b})"
    s = re.sub(r"\\(?:d|t)?frac\{([^{}]+?)\}\{([^{}]+?)\}", _replace_frac, s)

    # \sqrt{x} 和 \sqrt[n]{x}
    def _replace_sqrt(match):
        n = match.group(1)
        x = match.group(2).strip()
        if "\\frac" in x or "\\sqrt" in x:
            return match.group(0)
        prefix = ""
        if n:
            # n=3 → ³, n=k → ᵏ (用 superscript)
            if n in "0123456789":
                prefix = _UNICODE_SUPERSCRIPT.get(n, n)
            elif n.isalpha() and n.lower() == "n":
                prefix = "ⁿ"
        if len(x) == 1:
            return f"{prefix}√{x}"
        return f"{prefix}√({x})"
    s = re.sub(r"\\sqrt(?:\[([^\]]+)\])?\{([^{}]+?)\}", _replace_sqrt, s)

    # 单字符常量
    _CONSTS = {
        "\\partial": "∂", "\\nabla": "∇", "\\sum": "Σ", "\\prod": "∏",
        "\\int": "∫", "\\oint": "∮", "\\bullet": "•",
        "\\cdots": "⋯", "\\ldots": "…", "\\cdot": "·",
    }
    for tex, uni in _CONSTS.items():
        s = s.replace(tex, uni)

    return s


def _apply_latex_scripts_to_text(s: str) -> str:
    s = re.sub(r"\^\{([^{}]+)\}", lambda m: _script_text("^", m.group(1)), s)
    s = re.sub(r"_\{([^{}]+)\}", lambda m: _script_text("_", m.group(1)), s)
    s = re.sub(r"\^([A-Za-z0-9+\-=()])", lambda m: _script_text("^", m.group(1)), s)
    s = re.sub(r"_([A-Za-z0-9+\-=()])", lambda m: _script_text("_", m.group(1)), s)
    return s


def _latex_plain_fallback(eq: str) -> str:
    """Best-effort 文字化 LaTeX. 失败时尽量保留可识别痕迹给后续 _is_broken_after_strip 检测。

    2026-05-18 P192: 之前的实现末尾用 `re.sub(r"\\\\([A-Za-z]+)", r"\\1", s)` 无差别剥
    backslash, 把 `\\begin{vmatrix}` 变成 "beginvmatrix" 这种 OCR 残骸样式文本。
    新版改成白名单制: 已知命令转 Unicode 或合理替代, 未知命令**保留 `\\cmd`** 让
    `_is_broken_after_strip` 后续检测能识别为"渲染失败"。
    """
    s = _preprocess_simple_latex_macros(eq or "").strip()

    # ─ 第 1 步: 间距/无 brace 修饰命令 → 去除 (先剥这些, 避免 `\displaystyle\lim`
    # 在 step 2 把 `\lim` 替换后导致 `\displaystyle` 后面变字母, 块住后续剥离). ─
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\:", " ").replace("\\!", "")
    strip_safe = (
        "left", "right",
        "Bigl", "Bigr", "Big", "bigl", "bigr", "big",
        "Biggl", "Biggr", "Bigg", "biggl", "biggr", "bigg",
        "quad", "qquad",
        "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
        "limits", "nolimits", "mathstrut", "mathchoice",
    )
    for cmd in strip_safe:
        repl = " " if cmd in ("quad", "qquad") else ""
        s = re.sub(rf"\\{cmd}(?![A-Za-z])", repl, s)

    # ─ 第 2 步: 已知命令直接转 Unicode 单字符 ─
    replacements = {
        "iiint": "∭", "iint": "∬", "oint": "∮", "int": "∫",
        "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
        "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠",
        "approx": "≈", "equiv": "≡", "rightarrow": "→", "leftarrow": "←", "to": "→",
        "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
        "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "∏",
        "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
        "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
        "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
        "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
        "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ",
        "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
        "varphi": "ϕ", "chi": "χ", "psi": "ψ", "omega": "ω",
        "forall": "∀", "exists": "∃", "nexists": "∄", "in": "∈", "notin": "∉",
        "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇",
        "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
        "perp": "⊥", "parallel": "∥", "mid": "|",
        "ell": "ℓ", "Re": "ℜ", "Im": "ℑ",
        "ldots": "…", "cdots": "⋯",
        "implies": "⇒", "iff": "⇔",
        # 2026-05-18 P192 v2: 函数名 — 写成自身 (剥 backslash 后是合理文字)
        "lim": "lim", "max": "max", "min": "min", "sup": "sup", "inf": "inf",
        "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot",
        "sec": "sec", "csc": "csc", "arcsin": "arcsin", "arccos": "arccos",
        "arctan": "arctan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh",
        "log": "log", "ln": "ln", "exp": "exp",
        "det": "det", "ker": "ker", "dim": "dim", "deg": "deg",
        "gcd": "gcd", "lcm": "lcm", "mod": "mod",
        "argmin": "argmin", "argmax": "argmax",
        # 装饰用作单字符
        "prime": "′", "circ": "°", "bullet": "•", "ast": "∗",
        "ell": "ℓ",  # 重复忽略
    }
    for cmd, plain in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        s = re.sub(rf"\\{cmd}(?![A-Za-z])", plain, s)

    # ─ 第 3 步: 带 brace 的命令 → 保留内容 ─
    s = re.sub(r"\\(?:mathrm|mathit|mathbf|mathsf|mathtt|mathbb|mathcal|mathfrak|text)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:d|t)?frac\{([^{}]+?)\}\{([^{}]+?)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt(?:\[[^\]]+\])?\{([^{}]+?)\}", r"√(\1)", s)
    s = re.sub(r"\\binom\{([^{}]+?)\}\{([^{}]+?)\}", r"C(\1,\2)", s)
    # 装饰命令: 保留内容 (mathtext 也支持但 plain 路径走这里说明失败)
    for cmd in ("hat", "tilde", "bar", "vec", "dot", "ddot", "widehat", "widetilde", "overline", "underline"):
        s = re.sub(rf"\\{cmd}\{{([^{{}}]+)\}}", r"\1", s)
    # 重写过的 (来自 P183) 已经处理过, 但 fallback 再做一次保险
    s = re.sub(r"\\overset\{([^{}]+)\}\{([^{}]+)\}", r"\1/\2", s)
    s = re.sub(r"\\underset\{([^{}]+)\}\{([^{}]+)\}", r"\2(\1)", s)

    # ─ 第 4 步: 上下标处理 ─
    s = re.sub(
        r"([∫∬∭∮])([A-Za-z0-9+\-=()]+)\^\{?([A-Za-z0-9+\-=()]+)\}?",
        lambda m: m.group(1) + _script_text("_", m.group(2)) + _script_text("^", m.group(3)),
        s,
    )
    s = _apply_latex_scripts_to_text(s)

    # ─ 第 5 步: 清理残留 ─
    s = s.replace("$", "")
    s = s.replace("{", "").replace("}", "")
    # 2026-05-18 P192: **不再** `re.sub(r"\\([A-Za-z]+)", r"\1", s)` 这步.
    # 改: 保留 `\cmd` 让 _is_broken_after_strip 在 caller 那边能识别为渲染失败。
    # 唯一例外: 单独 `\\` (换行/无 brace) 转空格, 避免裸 backslash 流出
    s = re.sub(r"\\\\", " ", s)  # double backslash (LaTeX line break)
    return re.sub(r"\s+", " ", s).strip()


def _try_unicode_render(eq: str) -> str | None:
    """尝试把简单 LaTeX 公式转为 Unicode 字符串。

    成功条件: 公式只含数字、字母、空格、`+`/`-`/`*`/`.`、单字符上下标 `^x` `_x`,
             多字符上下标 `^{...}` `_{...}` (内部仅数字/字母/+/-/=),
             已知希腊字母 `\\alpha` 等, 已知操作 `\\times` 等.

    失败时返回 None (调用方走 PNG 渲染).

    例子:
      "10^4"            → "10⁴"
      "10^{4}"          → "10⁴"
      "x_0 + y^2"       → "x₀ + y²"
      "4.716 \\times 10^6" → "4.716×10⁶"
      "\\alpha + \\beta" → "α + β"
      "n^2 / \\log n"    → None (含 \\log/\\frac 等命令, 走 PNG)
    """
    if not eq or not eq.strip():
        return None
    # 2026-05-11 P13c: 先预处理 \frac/\sqrt/\sum 等为 Unicode 友好形式
    s = _preprocess_simple_latex_macros(eq).strip()
    # 太长 → 八成是复杂公式, 走 PNG
    if len(s) > 80:
        return None
    # 含分式/根号/积分等复杂结构(预处理没能消除) → 走 PNG
    if any(cmd in s for cmd in (
        "\\frac", "\\sqrt", "\\prod", "\\lim",
        "\\binom", "\\matrix", "\\begin", "\\end", "\\left", "\\right",
        "\\over", "\\dfrac", "\\tfrac",
    )):
        return None

    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        # 反斜杠开头的命令 \alpha / \times / \log...
        if c == "\\":
            # 尝试匹配最长名字
            j = i + 1
            while j < n and (s[j].isalpha()):
                j += 1
            cmd = s[i+1:j]
            if cmd in _GREEK_LETTERS:
                out.append(_GREEK_LETTERS[cmd])
            elif cmd in _SIMPLE_OPS:
                out.append(_SIMPLE_OPS[cmd])
            elif cmd in ("text", "mathrm"):
                # \text{abc} 提取里面
                if j < n and s[j] == "{":
                    end = s.find("}", j)
                    if end == -1:
                        return None
                    out.append(s[j+1:end])
                    i = end + 1
                    continue
                else:
                    return None
            else:
                # 未知命令 → 退到 PNG
                return None
            i = j
        elif c == "^" or c == "_":
            mapping = _UNICODE_SUPERSCRIPT if c == "^" else _UNICODE_SUBSCRIPT
            if i + 1 >= n:
                return None
            # 单字符 ^x
            if s[i+1] != "{":
                char = s[i+1]
                if char in mapping:
                    out.append(mapping[char])
                    i += 2
                else:
                    return None
            else:
                # ^{xyz}
                end = s.find("}", i + 2)
                if end == -1:
                    return None
                inner = s[i+2:end]
                converted = []
                for ch in inner:
                    if ch in mapping:
                        converted.append(mapping[ch])
                    elif ch.isspace():
                        converted.append(ch)
                    else:
                        return None
                out.append("".join(converted))
                i = end + 1
        elif c == "{" or c == "}":
            # 裸 {} 通常用于分组, 透明跳过
            i += 1
        elif c.isalnum() or c in " .,+-*/=<>()[]\t":
            out.append(c)
            i += 1
        # 2026-05-11 P13c: 允许预处理产生的 Unicode 数学符号通过
        elif c in "√∂∇Σ∏∫∮·⋯…•":
            out.append(c)
            i += 1
        # Unicode 上下标字符 (预处理可能直接产出)
        elif c in "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎":
            out.append(c)
            i += 1
        # 希腊字母 (预处理或前面 \ 命令产出)
        elif c in "αβγδεζηθικλμνξπρστφχψω" "ΓΔΘΛΞΠΣΦΨΩ":
            out.append(c)
            i += 1
        # 常见数学运算符 (预处理或前面 \ 命令产出)
        elif c in "×÷±∓≤≥≠≈≡→←∞":
            out.append(c)
            i += 1
        else:
            # 未知字符 → PNG
            return None

    result = "".join(out)
    # 防御: 结果若仍含 \ 或 { 等说明转换不完整
    if any(ch in result for ch in ("\\", "{", "}")):
        return None
    return result


def _read_subsup(s: str, start: int) -> tuple[str | None, str | None, int]:
    """读取可选的 _{...} 和 ^{...} (任意顺序,最多各 1 次)。

    返回 (sub_content, sup_content, 读完后的索引)。
    单字符/单宏也作为 sub/sup 内容(例如 _x 或 ^\\alpha)。
    """
    sub: str | None = None
    sup: str | None = None
    i = start
    for _ in range(2):
        # 跳过空白
        while i < len(s) and s[i] == ' ':
            i += 1
        if i >= len(s):
            break
        ch = s[i]
        if ch == '_' and sub is None:
            target_idx = i + 1
            sub_val, next_i = _read_sub_or_sup_arg(s, target_idx)
            if sub_val is None:
                break
            sub = sub_val
            i = next_i
        elif ch == '^' and sup is None:
            target_idx = i + 1
            sup_val, next_i = _read_sub_or_sup_arg(s, target_idx)
            if sup_val is None:
                break
            sup = sup_val
            i = next_i
        else:
            break
    return sub, sup, i


def _read_sub_or_sup_arg(s: str, start: int) -> tuple[str | None, int]:
    """读取 _ 或 ^ 后的参数:支持 {group} / 单字符 / \\宏名."""
    if start >= len(s):
        return None, start
    if s[start] == '{':
        g = _read_brace_group(s, start)
        if g is None:
            return None, start
        return g
    if s[start] == '\\':
        m = re.match(r'\\[a-zA-Z]+', s[start:])
        if m:
            return m.group(0), start + len(m.group(0))
        return None, start
    return s[start], start + 1


_NARY_OPS = {
    "sum":    "∑",
    "int":    "∫",
    "iint":   "∬",
    "iiint":  "∭",
    "oint":   "∮",
    "prod":   "∏",
    "coprod": "∐",
    "bigcup": "⋃",
    "bigcap": "⋂",
}


def _latex_to_omml_simple(latex: str) -> str | None:
    """把 LaTeX 转 OMML XML 字符串(可直接嵌入 docx paragraph).

    支持的语法:
    - 文本/数字 + Unicode 转换后的希腊字母/运算符
    - ^{x} 上标,_{x} 下标 → m:sSup / m:sSub
    - \\frac{a}{b} → m:f (可嵌套)
    - \\sqrt{x}, \\sqrt[n]{x} → m:rad (可嵌套)
    - \\sum, \\int, \\prod, \\oint 等带可选 _{}^{} → m:nary
    - \\lim 带可选 _{} → m:limLow
    - \\binom{a}{b} → m:d(m:f with noBar)

    不支持的(\\begin{...} 环境、\\text{...}、矩阵等) → return None,走 PNG.
    """
    s = latex.strip()
    if not s:
        return None

    NS = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'

    def _esc(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _r(text: str) -> str:
        return f"<m:r><m:t>{_esc(text)}</m:t></m:r>"

    # 拒绝明确不支持的语法
    if "\\begin" in s or "\\end{" in s:
        return None
    if "\\text{" in s or "\\textbf{" in s or "\\textit{" in s:
        return None

    # 替换栈:每找到一个 macro 就把它的 OMML XML 存到 stash,在原位置放一个占位 marker
    stash: dict[str, str] = {}
    counter = [0]

    def _make_marker(xml: str) -> str:
        marker = f"\x01OMML{counter[0]}\x01"
        stash[marker] = xml
        counter[0] += 1
        return marker

    def _convert_inner(text: str | None) -> str | None:
        """递归转换子表达式(用于 \\frac 的分子分母、\\sum 的 sub/sup 等)。"""
        if text is None or not text.strip():
            return ""
        scanned = _scan_and_replace(text)
        if scanned is None:
            return None
        return _build_inner_xml(scanned)

    def _scan_and_replace(work: str) -> str | None:
        """循环找到 LaTeX macro,转换成 OMML XML 放入 stash,用 marker 占位。

        从最深层往外逐次处理(由于 _convert_inner 递归调用,自然形成 inside-out)。
        如果遇到不认识的 \\macro,返回 None。
        """
        # 1) 先把简单宏 (\\alpha \\beta \\times \\pm \\cdot 等) 转 Unicode
        work = _preprocess_simple_latex_macros_omml_safe(work)

        progress = True
        while progress:
            progress = False

            # ── \\sum / \\int / \\prod 等大算符 ──
            for op_name, op_char in _NARY_OPS.items():
                pat = re.compile(r'\\' + op_name + r'(?![a-zA-Z])')
                m = pat.search(work)
                if not m:
                    continue
                start_idx, after_macro = m.start(), m.end()
                sub, sup, after = _read_subsup(work, after_macro)
                # 大算符后可选 {body},否则不消耗后续
                body: str | None = None
                if after < len(work) and work[after] == '{':
                    g = _read_brace_group(work, after)
                    if g:
                        body, after = g
                sub_xml = _convert_inner(sub)
                sup_xml = _convert_inner(sup)
                body_xml = _convert_inner(body)
                if sub_xml is None or sup_xml is None or body_xml is None:
                    return None
                sub_hide = "off" if sub is not None else "on"
                sup_hide = "off" if sup is not None else "on"
                naryPr = (
                    f'<m:naryPr><m:chr m:val="{op_char}"/>'
                    f'<m:limLoc m:val="undOvr"/>'
                    f'<m:subHide m:val="{sub_hide}"/>'
                    f'<m:supHide m:val="{sup_hide}"/>'
                    f'</m:naryPr>'
                )
                sub_el = f'<m:sub>{sub_xml or ""}</m:sub>'
                sup_el = f'<m:sup>{sup_xml or ""}</m:sup>'
                e_el = f'<m:e>{body_xml or ""}</m:e>'
                xml = f'<m:nary>{naryPr}{sub_el}{sup_el}{e_el}</m:nary>'
                marker = _make_marker(xml)
                work = work[:start_idx] + marker + work[after:]
                progress = True
                break
            if progress:
                continue

            # ── \\lim_{x \\to a} ──
            m = re.search(r'\\lim(?![a-zA-Z])', work)
            if m:
                start_idx, after_macro = m.start(), m.end()
                sub, _, after = _read_subsup(work, after_macro)
                body: str | None = None
                if after < len(work) and work[after] == '{':
                    g = _read_brace_group(work, after)
                    if g:
                        body, after = g
                sub_xml = _convert_inner(sub)
                body_xml = _convert_inner(body)
                if sub_xml is None or body_xml is None:
                    return None
                limLow = (
                    f'<m:limLow><m:limLowPr><m:ctrlPr/></m:limLowPr>'
                    f'<m:e><m:r><m:t>lim</m:t></m:r></m:e>'
                    f'<m:lim>{sub_xml or ""}</m:lim>'
                    f'</m:limLow>'
                )
                if body_xml:
                    xml = limLow + body_xml
                else:
                    xml = limLow
                marker = _make_marker(xml)
                work = work[:start_idx] + marker + work[after:]
                progress = True
                continue

            # ── \\binom{a}{b} ──
            m = re.search(r'\\binom(?![a-zA-Z])', work)
            if m:
                start_idx, after_macro = m.start(), m.end()
                if after_macro >= len(work) or work[after_macro] != '{':
                    return None
                g1 = _read_brace_group(work, after_macro)
                if g1 is None:
                    return None
                a_text, idx2 = g1
                if idx2 >= len(work) or work[idx2] != '{':
                    return None
                g2 = _read_brace_group(work, idx2)
                if g2 is None:
                    return None
                b_text, after = g2
                a_xml = _convert_inner(a_text)
                b_xml = _convert_inner(b_text)
                if a_xml is None or b_xml is None:
                    return None
                xml = (
                    f'<m:d><m:dPr><m:begChr m:val="("/><m:endChr m:val=")"/>'
                    f'<m:grow m:val="on"/></m:dPr>'
                    f'<m:e><m:f><m:fPr><m:type m:val="noBar"/></m:fPr>'
                    f'<m:num>{a_xml or ""}</m:num>'
                    f'<m:den>{b_xml or ""}</m:den></m:f></m:e></m:d>'
                )
                marker = _make_marker(xml)
                work = work[:start_idx] + marker + work[after:]
                progress = True
                continue

            # ── \\frac{a}{b} / \\dfrac / \\tfrac ──
            m = re.search(r'\\(?:d|t)?frac(?![a-zA-Z])', work)
            if m:
                start_idx, after_macro = m.start(), m.end()
                if after_macro >= len(work) or work[after_macro] != '{':
                    return None
                g1 = _read_brace_group(work, after_macro)
                if g1 is None:
                    return None
                a_text, idx2 = g1
                if idx2 >= len(work) or work[idx2] != '{':
                    return None
                g2 = _read_brace_group(work, idx2)
                if g2 is None:
                    return None
                b_text, after = g2
                a_xml = _convert_inner(a_text)
                b_xml = _convert_inner(b_text)
                if a_xml is None or b_xml is None:
                    return None
                xml = (
                    f'<m:f><m:fPr><m:type m:val="bar"/></m:fPr>'
                    f'<m:num>{a_xml or ""}</m:num>'
                    f'<m:den>{b_xml or ""}</m:den></m:f>'
                )
                marker = _make_marker(xml)
                work = work[:start_idx] + marker + work[after:]
                progress = True
                continue

            # ── \\sqrt[n]{x} / \\sqrt{x} ──
            m = re.search(r'\\sqrt(?![a-zA-Z])', work)
            if m:
                start_idx, after_macro = m.start(), m.end()
                deg: str | None = None
                if after_macro < len(work) and work[after_macro] == '[':
                    end_bracket = work.find(']', after_macro)
                    if end_bracket == -1:
                        return None
                    deg = work[after_macro + 1:end_bracket]
                    after_macro = end_bracket + 1
                if after_macro >= len(work) or work[after_macro] != '{':
                    return None
                g = _read_brace_group(work, after_macro)
                if g is None:
                    return None
                rad_text, after = g
                rad_xml = _convert_inner(rad_text)
                if rad_xml is None:
                    return None
                if deg is not None:
                    deg_xml = _convert_inner(deg)
                    if deg_xml is None:
                        deg_xml = _r(deg)
                    xml = (
                        f'<m:rad><m:deg>{deg_xml}</m:deg>'
                        f'<m:e>{rad_xml or ""}</m:e></m:rad>'
                    )
                else:
                    xml = (
                        f'<m:rad><m:radPr><m:degHide m:val="on"/></m:radPr>'
                        f'<m:e>{rad_xml or ""}</m:e></m:rad>'
                    )
                marker = _make_marker(xml)
                work = work[:start_idx] + marker + work[after:]
                progress = True
                continue

        # 处理完后,如果还有未识别的 \\macro,返回 None
        remaining = re.findall(r'\\([a-zA-Z]+)', work)
        if remaining:
            return None

        return work

    def _build_inner_xml(work: str) -> str:
        """把含 marker 的中间表示构造为 OMML XML。"""
        parts = re.split(r'(\x01OMML\d+\x01)', work)
        xml_parts = []
        for p in parts:
            if not p:
                continue
            if p.startswith('\x01OMML'):
                xml_parts.append(stash.get(p, ""))
            else:
                # 文本含 ^/_ — 走原有的 _latex_text_to_omml_runs
                xml_parts.append(_latex_text_to_omml_runs(p))
        return "".join(xml_parts)

    scanned = _scan_and_replace(s)
    if scanned is None:
        return None
    inner = _build_inner_xml(scanned)
    return f'<m:oMath {NS}>{inner}</m:oMath>'


def _preprocess_simple_latex_macros_omml_safe(eq: str) -> str:
    """只把 \\alpha 等简单宏转 Unicode,不动 \\frac/\\sqrt/\\sum 等结构性宏。

    用于 OMML 转换路径 — 与原 _preprocess_simple_latex_macros 区分,后者会
    把 \\frac{a}{b} 也转成 a/b,丢失了结构信息。

    2026-05-18 P193 OMML unification: 与 inline 路径保持一致, 剥离/重写已知的
    mathtext-不支持-但-语义无关 命令: \\displaystyle, \\textstyle, \\limits,
    \\nolimits, \\implies → \\Rightarrow, \\iff → \\Leftrightarrow.
    """
    s = eq
    # P193 (与 inline 路径 P180/P181/P183/P191 保持一致):
    # 1. 剥离样式修饰符 (mathtext 和 OMML 都不需要)
    s = re.sub(r"\\(displaystyle|textstyle|scriptstyle|scriptscriptstyle)(?![A-Za-z])", "", s)
    s = re.sub(r"\\(limits|nolimits|mathstrut|mathchoice)(?![A-Za-z])", "", s)
    # 2. 重写不支持命令为 OMML 支持的等价
    s = re.sub(r"\\implies(?![A-Za-z])", r"\\Rightarrow", s)
    s = re.sub(r"\\iff(?![A-Za-z])", r"\\Leftrightarrow", s)
    s = re.sub(r"\\xrightarrow\{[^{}]*\}", r"\\to", s)
    s = re.sub(r"\\xleftarrow\{[^{}]*\}", r"\\leftarrow", s)
    # 3. \choose → \binom (P191)
    s = _rewrite_choose_to_binom(s)
    # 装饰类丢弃 (OMML 也不支持)
    s = re.sub(r"\\overbrace\{([^{}]+)\}", r"\1", s)
    s = re.sub(r"\\underbrace\{([^{}]+)\}", r"\1", s)
    s = re.sub(r"\\boxed\{([^{}]+)\}", r"\1", s)

    # 希腊字母
    for name, ch in _GREEK_LETTERS.items():
        s = re.sub(r'\\' + name + r'(?![a-zA-Z])', ch, s)
    # 基础运算符
    for name, ch in _SIMPLE_OPS.items():
        s = re.sub(r'\\' + name + r'(?![a-zA-Z])', ch, s)
    # 其他单字符常量 (不含 \\sum/\\int 等大算符,那些走 m:nary)
    _OMML_CONST = {
        "partial": "∂", "nabla": "∇", "bullet": "•",
        "cdots": "⋯", "ldots": "…",
        "forall": "∀", "exists": "∃", "in": "∈", "notin": "∉",
        "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇",
        "cup": "∪", "cap": "∩", "emptyset": "∅",
        "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
        "land": "∧", "lor": "∨", "lnot": "¬", "neg": "¬",
        "ell": "ℓ", "Re": "ℜ", "Im": "ℑ",
    }
    for name, ch in _OMML_CONST.items():
        s = re.sub(r'\\' + name + r'(?![a-zA-Z])', ch, s)
    # 函数名 — 转成普通文字 (Word 数学公式里函数名应该是直立体)
    # 这些不带 _/^ 时就是普通文本;带 _/^ 时,_latex_text_to_omml_runs
    # 会自然把 sin_x 等当作下标处理 (虽然语义稍偏,但显示正确)。
    _FUNC_NAMES = (
        "sin", "cos", "tan", "cot", "sec", "csc",
        "sinh", "cosh", "tanh",
        "arcsin", "arccos", "arctan",
        "log", "ln", "lg", "exp",
        "max", "min", "sup", "inf",
        "det", "deg", "dim", "ker", "gcd", "lcm",
        "mod", "arg", "Pr",
    )
    for fname in _FUNC_NAMES:
        s = re.sub(r'\\' + fname + r'(?![a-zA-Z])', fname, s)
    # \\, \\; \\: \\! \\quad \\qquad 这类间距 — 转空格
    s = re.sub(r'\\[,;: ]', ' ', s)
    s = re.sub(r'\\quad(?![a-zA-Z])', '  ', s)
    s = re.sub(r'\\qquad(?![a-zA-Z])', '    ', s)
    s = re.sub(r'\\!(?![a-zA-Z])', '', s)
    # \\left \\right (OMML 自己处理括号,这里去掉)
    s = re.sub(r'\\left(?![a-zA-Z])', '', s)
    s = re.sub(r'\\right(?![a-zA-Z])', '', s)
    return s
