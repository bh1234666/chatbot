"""代码大纲与文本读取工具:智能解码、按行迭代、首尾截断、路径分词、函数边界定位、
被调用者扫描、C/通用语言大纲提取。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。经 extract_analysis --closure
验证自包含(8 函数, 0 unsafe),仅依赖 stdlib(re)。workspace.py 通过 re-export 兼容。
"""
import re


def _tokenize_path(path: str) -> list[str]:
    """把文件路径按常见分隔符切成 token,小写。
    例: helper_bptree_helper_bptree_bench.c → [helper, bptree, helper, bptree, bench, c]
    """
    import re as _re_mod
    return [t.lower() for t in _re_mod.split(r"[/\\\-_.]+", path) if t]


def _extract_c_outline(content: str) -> dict:
    """从 C/C++ 源码抽 outline。正则匹配函数定义、includes、typedef、#define。"""
    import re as _re_mod
    # 函数定义: 类型 name(args) {  — 通常在行首,排除声明(末尾分号)和宏调用
    func_re = _re_mod.compile(
        r"^[ \t]*(?:static\s+|extern\s+|inline\s+)*"
        r"(?:const\s+)?"
        r"(?:unsigned\s+|signed\s+)?"
        r"(?:[a-zA-Z_][a-zA-Z0-9_]*\s+\*?\s*)+"  # return type
        r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^;{]*\)\s*\{",
        _re_mod.MULTILINE,
    )
    funcs = []
    for m in func_re.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        funcs.append(f"{m.group(1)}() [line {line_no}]")
    includes = _re_mod.findall(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', content, _re_mod.MULTILINE)
    defines = _re_mod.findall(r'^\s*#\s*define\s+([A-Z_][A-Z0-9_]*)', content, _re_mod.MULTILINE)
    typedefs = _re_mod.findall(r'^\s*typedef\s+(?:struct\s+)?(?:[a-zA-Z_][a-zA-Z0-9_]*\s+)+([a-zA-Z_][a-zA-Z0-9_]*)\s*;',
                               content, _re_mod.MULTILINE)
    return {
        "language": "c",
        "functions": funcs[:50],
        "includes": includes[:30],
        "defines": defines[:30],
        "typedefs": typedefs[:20],
    }


def _extract_generic_outline(content: str, *, language_hint: str = "generic") -> dict:
    """通用 fallback: 抓所有以行首关键字开头的"声明性"行。"""
    import re as _re_mod
    out = {"language": language_hint}
    decls = []
    pat = _re_mod.compile(
        r"^[ \t]*(?:export\s+|public\s+|private\s+|static\s+|async\s+)*"
        r"(?:function|def|class|struct|interface|trait|impl|fn|func|type)"
        r"\s+([A-Za-z_][A-Za-z0-9_]*)",
        _re_mod.MULTILINE,
    )
    for m in pat.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        decls.append(f"{m.group(1)} [line {line_no}]")
    out["declarations"] = decls[:50]
    return out


def _iter_text_lines(target: str):
    """流式迭代文件行(UTF-8 容错 + 行尾规范化)。
    yield (line_no_1indexed, line_without_trailing_newline)。
    用于 search_in_file 大文件场景,避免全部加载内存。
    """
    with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
        for i, line in enumerate(f, start=1):
            # 去掉行尾换行符(可能是 \n / \r\n / \r)
            line = line.rstrip("\n").rstrip("\r")
            yield i, line


def _find_function_end(lines: list[str], start_line: int, lang: str) -> int | None:
    """从 start_line 起向后扫,找到函数体结束行号(1-indexed inclusive)。

    C/C++/Java/JS/Go/Rust:扫 `{` 配对(栈式 +1/-1,首个 {开始计数,首次回到 0 时结束)
    Python:看缩进 — 找到 def 行后第一个非空非注释行的缩进作为基准,
            后续行缩进 ≥ 基准则属于函数体,缩进 < 基准则结束。

    起始 line 1-indexed。返回结束 line 1-indexed 含。失败返回 None。
    """
    total = len(lines)
    if start_line > total:
        return None

    if lang == "py":
        # 找 def 行的缩进
        def_idx = start_line - 1
        def_line = lines[def_idx]
        def_indent = len(def_line) - len(def_line.lstrip())
        # 找函数体的第一行(跳过 docstring 和空行后的第一个真实代码行)
        body_indent = None
        for i in range(def_idx + 1, total):
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= def_indent:
                # 函数体还没开始就出现同/低缩进 = 错误位置
                return None
            body_indent = indent
            break
        if body_indent is None:
            return total  # 函数到文件末
        # 从 body 第一行起向后扫,找到第一个非空非注释且缩进 < body_indent 的行
        end = total  # 默认到文件末
        for i in range(def_idx + 1, total):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            if indent < body_indent and not stripped.startswith("#"):
                # 不能是装饰器或同级闭包子句 — 简单检测:看是不是 def/class
                end = i  # i 是 0-indexed,函数体在 i 之前结束(i-1 是最后一行,1-indexed = i)
                break
        return end

    # C/C++/Java/JS/Go/Rust:扫 { } 平衡
    depth = 0
    started = False
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    for i in range(start_line - 1, total):
        line = lines[i]
        for j, ch in enumerate(line):
            # 处理字符串/字符/注释中的 { } 不计入
            if in_line_comment:
                pass  # 行尾自动结束
            elif in_block_comment:
                if ch == "/" and j > 0 and line[j - 1] == "*":
                    in_block_comment = False
            elif in_string:
                if ch == '"' and (j == 0 or line[j - 1] != "\\"):
                    in_string = False
            elif in_char:
                if ch == "'" and (j == 0 or line[j - 1] != "\\"):
                    in_char = False
            else:
                if ch == "/" and j + 1 < len(line):
                    if line[j + 1] == "/":
                        in_line_comment = True
                    elif line[j + 1] == "*":
                        in_block_comment = True
                elif ch == '"':
                    in_string = True
                elif ch == "'":
                    in_char = True
                elif ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
                    if started and depth == 0:
                        return i + 1  # 1-indexed
        in_line_comment = False  # 行尾重置
    return None  # 没找到匹配的 }


def _find_callees_in_file(lines: list[str], start_line: int, end_line: int) -> list[str]:
    """在 [start, end] 范围内提取被调用的符号(函数调用)。

    简单识别:r'(\\w+)\\s*\\(' 模式且不是关键字。
    返回去重的 "name @ Lxxx" 列表(只记第一次出现位置)。

    2026-05-02 part16 修:扫描从 body 内部开始(跳过签名行),避免把函数自己列为 callee。
    body 起点启发式:找第一个 `{` 后的行(C/Java/JS),或第一个非签名行(Python)。
    """
    KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "case",
                "do", "else", "typedef", "struct", "union", "enum",
                "static", "extern", "const", "void", "char", "int", "long",
                "short", "float", "double", "unsigned", "signed", "auto",
                "and", "or", "not", "in", "is", "lambda", "yield",
                "print", "len", "range", "list", "dict", "set", "tuple",
                "str", "bool"}
    # 找 body 起点 — 第一个含 `{` 的行之后,或第一个非签名行
    body_start_idx = start_line - 1  # 0-indexed
    for i in range(start_line - 1, min(end_line, len(lines))):
        if "{" in lines[i] or lines[i].rstrip().endswith(":"):  # C body 或 Python def end
            body_start_idx = i + 1
            break

    seen = {}
    pat = re.compile(r'(?<![\w])([a-zA-Z_]\w*)\s*\(')
    for i in range(body_start_idx, min(end_line, len(lines))):
        line = lines[i]
        for m in pat.finditer(line):
            name = m.group(1)
            if name in KEYWORDS:
                continue
            if name not in seen:
                seen[name] = i + 1
    return [f"{n} @ L{ln}" for n, ln in seen.items()]


def _truncate_head_tail(s: str, max_len: int) -> str:
    """字符串超过 max_len 时,头尾各保留 max_len/2,中间放 [...truncated N chars...]。

    2026-05-02 part10 (A8):workspace.run stdout/stderr 截断从"只保留头"改成"头尾都保留"。
    Python stack trace 的关键 raise 行、make 错误总结、test 失败 summary 都在末尾,
    只保留头会让模型看不到这些。头尾各半:头部看起始进度,尾部看错误结论。
    """
    if len(s) <= max_len:
        return s
    half = (max_len - 60) // 2  # 留 ~60 字符给中间标记
    head = s[:half]
    tail = s[-half:]
    truncated_chars = len(s) - 2 * half
    return f"{head}\n\n[...truncated {truncated_chars} chars...]\n\n{tail}"


def _smart_decode(data: bytes) -> str:
    """优先 UTF-8;失败用系统首选编码(Windows 中文=cp936),最后兜底 latin-1。"""
    if not data:
        return ""
    # 先试 UTF-8 strict;成功就是真 UTF-8
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # 然后试系统首选编码(Win 中文 cp936 / 英文 cp1252 / Linux utf-8)
    try:
        import locale
        enc = locale.getpreferredencoding(False) or ""
        if enc and enc.lower() not in ("utf-8", "utf8"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                pass
    except Exception:
        pass
    # 最后兜底:UTF-8 with replace,不至于完全空白
    return data.decode("utf-8", errors="replace")
