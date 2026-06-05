"""
SQL 方言翻译器:PostgreSQL 语法 → SQLite。

从 db/pool.py 抽离出来,理由有二:
  1. 纯 stdlib(只依赖 re / functools),可脱离 aiosqlite / pydantic 独立单元测试。
  2. 把"扫描 SQL 文本"这一**与参数无关**的昂贵步骤(状态机 + 多个正则)缓存起来。

──────────────────────────────────────────────────────────────────────────
为什么缓存是安全的(与旧实现逐字节等价)
──────────────────────────────────────────────────────────────────────────
旧 `_translate_sql(sql, args)` 在单遍扫描里同时干两件事:
  (a) 生成翻译后的 SQL 文本;
  (b) 按出现顺序收集要绑定的实参 out_args。

观察:翻译产物里**唯一依赖实参的部分**是 `= ANY($N)` 的展开
(`IN (NULL)` vs `IN (?,?,…)`,占位符个数 = len(args[N-1])),以及 `$N → ?` 处
要绑哪个实参。除此之外的所有改写(NOW()/INTERVAL/ILIKE/GREATEST/->> 等)都是
**纯文本、与实参无关**。

因此可以把扫描结果编译成一份"计划"(segments):
  - ('lit', text)   —— 原样输出的字面文本(已完成所有纯文本改写)
  - ('dollar', k)   —— 输出 '?' 并绑定 args[k]
  - ('any', k)      —— 按 args[k] 展开成 IN (...) 并绑定其元素

计划**只依赖 sql 字符串**,可用 lru_cache 缓存。套用阶段(translate_sql)按实参
把计划展开成 (sql_text, out_args),再跑一次与旧版完全相同的 _DATE_COL_COMPARE_RE
后处理。最终输出对任意输入都与旧版按位相同 —— 见 tests/test_sql_translate.py 的
差分测试(对一大批 SQL/参数组合断言新旧实现输出相等)。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------
# 正则(从 db/pool.py 原样搬运,语义不变)
# ---------------------------------------------------------------------------

# cast 形如 ::jsonb / ::text / ::text[] / ::bigint / ::bigint[] / ::int / ::int[]
_CAST_RE = re.compile(r'::(?:jsonb|text(?:\[\])?|bigint(?:\[\])?|int(?:\[\])?)')

# = ANY ($N[::cast]) — 容忍内嵌 cast
_ANY_RE = re.compile(
    r'=\s*ANY\s*\(\s*\$(\d+)'
    r'(?:::(?:jsonb|text(?:\[\])?|bigint(?:\[\])?|int(?:\[\])?))?'
    r'\s*\)',
    re.IGNORECASE,
)
_DOLLAR_RE = re.compile(r'\$(\d+)')
_NOW_RE = re.compile(r'\bNOW\s*\(\s*\)', re.IGNORECASE)
_GREATEST_RE = re.compile(r'\bGREATEST\b', re.IGNORECASE)
_LEAST_RE = re.compile(r'\bLEAST\b', re.IGNORECASE)
_ILIKE_RE = re.compile(r'\bILIKE\b', re.IGNORECASE)

# NOW() ± INTERVAL 'N unit' → datetime('now', '±N unit')
_NOW_MINUS_INTERVAL_RE = re.compile(
    r'\bNOW\s*\(\s*\)\s*-\s*INTERVAL\s*\'(\d+)\s+(\w+)\'',
    re.IGNORECASE,
)
_NOW_PLUS_INTERVAL_RE = re.compile(
    r'\bNOW\s*\(\s*\)\s*\+\s*INTERVAL\s*\'(\d+)\s+(\w+)\'',
    re.IGNORECASE,
)

# <col>->>'<key>' → json_extract(<col>, '$.<key>')
_JSON_ARROW_TEXT_RE = re.compile(
    r"([\w.]+)\s*->>\s*'([^']+)'",
)

# 时间列比较 wrap datetime():`<col> [<>]= datetime(` → `datetime(<col>) [<>]= datetime(`
# 必须在 SQL 文本完全组装后(含 ANY 展开)再跑,跑在最终字符串上,与旧版一致。
_DATE_COL_COMPARE_RE = re.compile(
    r'\b(created_at|updated_at|last_access|deleted_at|last_summary_at|upload_time)\s*([<>]=?)\s*datetime\(',
    re.IGNORECASE,
)


# Segment 类型:('lit', str) | ('dollar', int) | ('any', int)
Segment = tuple


@lru_cache(maxsize=2048)
def compile_translation(sql: str) -> tuple[Segment, ...]:
    """把 SQL 字符串编译成与实参无关的 segment 计划(可缓存)。

    扫描逻辑与旧 _translate_sql 完全一致(同一个状态机、同样的匹配优先级),
    唯一区别:遇到 $N / ANY 不读实参,只记 ('dollar', idx) / ('any', idx)。
    所有纯文本改写照常并入字面缓冲。
    """
    segments: list[Segment] = []
    buf: list[str] = []

    def _flush() -> None:
        if buf:
            segments.append(("lit", "".join(buf)))
            buf.clear()

    i = 0
    n = len(sql)
    state: str | None = None  # None / "'" / '"' / 'line_comment' / 'block_comment'

    while i < n:
        ch = sql[i]
        nxt2 = sql[i:i + 2]

        # ── 状态:行注释 ──
        if state == "line_comment":
            buf.append(ch)
            if ch == "\n":
                state = None
            i += 1
            continue

        # ── 状态:块注释 ──
        if state == "block_comment":
            if nxt2 == "*/":
                buf.append("*/")
                state = None
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue

        # ── 状态:字符串字面量 ──
        if state in ("'", '"'):
            quote = state
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:  # SQL 转义 ''
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                state = None
            i += 1
            continue

        # ── 进入注释 / 字符串 ──
        if nxt2 == "--":
            buf.append("--")
            state = "line_comment"
            i += 2
            continue
        if nxt2 == "/*":
            buf.append("/*")
            state = "block_comment"
            i += 2
            continue
        if ch == "'" or ch == '"':
            buf.append(ch)
            state = ch
            i += 1
            continue

        # ── = ANY ($N[::cast]) ── (必须在 _CAST_RE / _DOLLAR_RE 之前)
        m = _ANY_RE.match(sql, i)
        if m:
            idx = int(m.group(1)) - 1
            _flush()
            segments.append(("any", idx))
            i = m.end()
            continue

        # ── 独立 cast 移除 ──
        m = _CAST_RE.match(sql, i)
        if m:
            i = m.end()
            continue

        # ── $N → ? ──
        m = _DOLLAR_RE.match(sql, i)
        if m:
            idx = int(m.group(1)) - 1
            _flush()
            segments.append(("dollar", idx))
            i = m.end()
            continue

        # ── NOW() ± INTERVAL 'N unit' ── (必须在 NOW() 之前)
        m = _NOW_MINUS_INTERVAL_RE.match(sql, i)
        if m:
            _num, _unit = m.group(1), m.group(2).lower()
            buf.append(f"datetime('now', '-{_num} {_unit}')")
            i = m.end()
            continue
        m = _NOW_PLUS_INTERVAL_RE.match(sql, i)
        if m:
            _num, _unit = m.group(1), m.group(2).lower()
            buf.append(f"datetime('now', '+{_num} {_unit}')")
            i = m.end()
            continue

        # ── <col>->>'<key>' → json_extract ──
        m = _JSON_ARROW_TEXT_RE.match(sql, i)
        if m:
            col_expr = m.group(1)
            key = m.group(2)
            buf.append(f"json_extract({col_expr}, '$.{key}')")
            i = m.end()
            continue

        # ── NOW() → datetime('now') ──
        m = _NOW_RE.match(sql, i)
        if m:
            buf.append("datetime('now')")
            i = m.end()
            continue

        # ── GREATEST/LEAST → MAX/MIN ──
        m = _GREATEST_RE.match(sql, i)
        if m:
            buf.append("MAX")
            i = m.end()
            continue
        m = _LEAST_RE.match(sql, i)
        if m:
            buf.append("MIN")
            i = m.end()
            continue

        # ── ILIKE → LIKE ──
        m = _ILIKE_RE.match(sql, i)
        if m:
            buf.append("LIKE")
            i = m.end()
            continue

        # 字面量字符
        buf.append(ch)
        i += 1

    _flush()
    return tuple(segments)


def translate_sql(sql: str, args: tuple) -> tuple[str, tuple]:
    """把 PostgreSQL 风格 SQL+实参翻译成 SQLite 可执行的 SQL+实参。

    与旧 db.pool._translate_sql 行为逐字节等价(见差分测试)。
    扫描结果走 compile_translation 缓存;此函数只做"按实参套用 + 时间列后处理"。
    """
    parts: list[str] = []
    out_args: list[Any] = []

    for seg in compile_translation(sql):
        kind = seg[0]
        if kind == "lit":
            parts.append(seg[1])
        elif kind == "dollar":
            idx = seg[1]
            parts.append("?")
            out_args.append(args[idx] if 0 <= idx < len(args) else None)
        else:  # "any"
            idx = seg[1]
            vals = args[idx] if 0 <= idx < len(args) else []
            if not isinstance(vals, (list, tuple, set, frozenset)):
                vals = [vals]
            vals = list(vals)
            if not vals:
                parts.append("IN (NULL)")
            else:
                parts.append("IN (" + ",".join(["?"] * len(vals)) + ")")
                out_args.extend(vals)

    sql_translated = "".join(parts)

    # 时间列比较 wrap datetime()(跑在最终字符串上,与旧版完全一致)
    sql_translated = _DATE_COL_COMPARE_RE.sub(
        r'datetime(\1) \2 datetime(',
        sql_translated,
    )
    return sql_translated, tuple(out_args)


def cache_info():
    """暴露缓存命中统计,便于运维观测翻译缓存效果。"""
    return compile_translation.cache_info()
