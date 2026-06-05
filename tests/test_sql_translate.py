"""
db.sql_translate 的差分(等价性)测试。

策略:把【重构前】的 _translate_sql 原样嵌入此文件作为 oracle(参照实现),
对一大批 SQL/参数组合,断言新实现 translate_sql 的输出与 oracle 逐字节相同。
新实现的缓存路径与非缓存路径若有任何差异,这里都会暴露。

纯 stdlib,可离线运行:`pytest tests/test_sql_translate.py`
"""
import re
from typing import Any

import pytest

from app.db.sql_translate import translate_sql, compile_translation

# ── 参照实现所需正则(与原 pool.py 一致)──
_CAST_RE = re.compile(r'::(?:jsonb|text(?:\[\])?|bigint(?:\[\])?|int(?:\[\])?)')
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
_NOW_MINUS_INTERVAL_RE = re.compile(
    r'\bNOW\s*\(\s*\)\s*-\s*INTERVAL\s*\'(\d+)\s+(\w+)\'', re.IGNORECASE)
_NOW_PLUS_INTERVAL_RE = re.compile(
    r'\bNOW\s*\(\s*\)\s*\+\s*INTERVAL\s*\'(\d+)\s+(\w+)\'', re.IGNORECASE)
_JSON_ARROW_TEXT_RE = re.compile(r"([\w.]+)\s*->>\s*'([^']+)'")
_DATE_COL_COMPARE_RE = re.compile(
    r'\b(created_at|updated_at|last_access|deleted_at|last_summary_at|upload_time)\s*([<>]=?)\s*datetime\(',
    re.IGNORECASE,
)


def _translate_sql_oracle(sql: str, args: tuple) -> tuple[str, tuple]:
    """重构前 db.pool._translate_sql 的逐行拷贝,作为等价性 oracle。"""
    out_parts: list[str] = []
    out_args: list[Any] = []
    i = 0
    n = len(sql)
    state: str | None = None
    while i < n:
        ch = sql[i]
        nxt2 = sql[i:i + 2]
        if state == "line_comment":
            out_parts.append(ch)
            if ch == "\n":
                state = None
            i += 1
            continue
        if state == "block_comment":
            if nxt2 == "*/":
                out_parts.append("*/")
                state = None
                i += 2
                continue
            out_parts.append(ch)
            i += 1
            continue
        if state in ("'", '"'):
            quote = state
            out_parts.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    out_parts.append(sql[i + 1])
                    i += 2
                    continue
                state = None
            i += 1
            continue
        if nxt2 == "--":
            out_parts.append("--")
            state = "line_comment"
            i += 2
            continue
        if nxt2 == "/*":
            out_parts.append("/*")
            state = "block_comment"
            i += 2
            continue
        if ch == "'" or ch == '"':
            out_parts.append(ch)
            state = ch
            i += 1
            continue
        m = _ANY_RE.match(sql, i)
        if m:
            idx = int(m.group(1)) - 1
            vals = args[idx] if 0 <= idx < len(args) else []
            if not isinstance(vals, (list, tuple, set, frozenset)):
                vals = [vals]
            vals = list(vals)
            if not vals:
                out_parts.append('IN (NULL)')
            else:
                out_parts.append('IN (' + ','.join(['?'] * len(vals)) + ')')
                out_args.extend(vals)
            i = m.end()
            continue
        m = _CAST_RE.match(sql, i)
        if m:
            i = m.end()
            continue
        m = _DOLLAR_RE.match(sql, i)
        if m:
            idx = int(m.group(1)) - 1
            out_parts.append('?')
            out_args.append(args[idx] if 0 <= idx < len(args) else None)
            i = m.end()
            continue
        m = _NOW_MINUS_INTERVAL_RE.match(sql, i)
        if m:
            _num, _unit = m.group(1), m.group(2).lower()
            out_parts.append(f"datetime('now', '-{_num} {_unit}')")
            i = m.end()
            continue
        m = _NOW_PLUS_INTERVAL_RE.match(sql, i)
        if m:
            _num, _unit = m.group(1), m.group(2).lower()
            out_parts.append(f"datetime('now', '+{_num} {_unit}')")
            i = m.end()
            continue
        m = _JSON_ARROW_TEXT_RE.match(sql, i)
        if m:
            out_parts.append(f"json_extract({m.group(1)}, '$.{m.group(2)}')")
            i = m.end()
            continue
        m = _NOW_RE.match(sql, i)
        if m:
            out_parts.append("datetime('now')")
            i = m.end()
            continue
        m = _GREATEST_RE.match(sql, i)
        if m:
            out_parts.append("MAX")
            i = m.end()
            continue
        m = _LEAST_RE.match(sql, i)
        if m:
            out_parts.append("MIN")
            i = m.end()
            continue
        m = _ILIKE_RE.match(sql, i)
        if m:
            out_parts.append("LIKE")
            i = m.end()
            continue
        out_parts.append(ch)
        i += 1
    sql_translated = ''.join(out_parts)
    sql_translated = _DATE_COL_COMPARE_RE.sub(r'datetime(\1) \2 datetime(', sql_translated)
    return sql_translated, tuple(out_args)


# ── 测试用例:覆盖工程实际用到的各类 SQL 形态 ──
CASES: list[tuple[str, tuple]] = [
    ("SELECT 1", ()),
    ("SELECT * FROM archives WHERE id = $1", ("a1",)),
    ("SELECT * FROM t WHERE a = $2 AND b = $1", ("x", "y")),  # 乱序 $N
    ("UPDATE cold_nodes SET salience = $2 WHERE id = ANY($1::text[])", (["i1", "i2", "i3"], 0.9)),
    ("SELECT * FROM t WHERE id = ANY($1)", ([],)),                # 空列表 → IN (NULL)
    ("SELECT * FROM t WHERE id = ANY($1)", ("single",)),          # 非列表标量
    ("SELECT * FROM t WHERE id = ANY($1) AND k = $2", ({"s1", "s2"}, "kk")),  # set
    ("SELECT * FROM t WHERE created_at < NOW() - INTERVAL '1 day'", ()),
    ("SELECT * FROM t WHERE updated_at >= NOW() + INTERVAL '30 minutes'", ()),
    ("SELECT GREATEST(a, b), LEAST(c, d) FROM t", ()),
    ("SELECT * FROM t WHERE name ILIKE $1", ("%foo%",)),
    ("SELECT (meta->>'kind')::text FROM cold_nodes WHERE id = $1", ("c1",)),
    ("SELECT json_col->>'a', other->>'b' FROM t", ()),
    ("INSERT INTO t (a, b) VALUES ($1, $2)", ("v1", "v2")),
    # 字符串字面量内的 $N / ANY / NOW() 不应被翻译
    ("SELECT '$1 is ANY(NOW())' AS lit, x = $1 FROM t", ("real",)),
    ("SELECT 'it''s a test' , a = $1 FROM t", (1,)),
    # 注释内不翻译
    ("SELECT $1 -- comment with $2 and NOW()\n, b = $2 FROM t", ("p1", "p2")),
    ("SELECT $1 /* block $2 NOW() ANY($3) */ , c = $2 FROM t", ("p1", "p2")),
    # 时间列比较 wrap datetime + INTERVAL 组合
    ("DELETE FROM cold_nodes WHERE created_at < NOW() - INTERVAL '7 day' AND id = $1", ("c9",)),
    ("SELECT * FROM t WHERE last_access >= datetime('now', '-1 hour')", ()),
    # 越界 $N(防御路径)
    ("SELECT $5 FROM t", ("only_one",)),
    # 多个 ANY
    ("SELECT * FROM t WHERE a = ANY($1) AND b = ANY($2)", (["a", "b"], ["c"])),
    # 复杂真实查询
    (
        "UPDATE cold_nodes SET access_count = access_count + 1, "
        "salience = GREATEST(salience, $2) WHERE id = ANY($1::text[])",
        (["n1", "n2"], 0.5),
    ),
]


@pytest.mark.parametrize("sql,args", CASES)
def test_equivalence_with_oracle(sql, args):
    assert translate_sql(sql, args) == _translate_sql_oracle(sql, args)


@pytest.mark.parametrize("sql,args", CASES)
def test_repeated_calls_are_stable(sql, args):
    """缓存命中路径与首次路径输出一致(连续调用不漂移)。"""
    first = translate_sql(sql, args)
    for _ in range(5):
        assert translate_sql(sql, args) == first


def test_any_length_changes_placeholder_count():
    """同一 SQL、不同列表长度,占位符数量随之变化(缓存按 sql 复用计划但套用正确)。"""
    sql = "SELECT * FROM t WHERE id = ANY($1)"
    s1, a1 = translate_sql(sql, (["x"],))
    s3, a3 = translate_sql(sql, (["x", "y", "z"],))
    assert s1 == "SELECT * FROM t WHERE id IN (?)"
    assert s3 == "SELECT * FROM t WHERE id IN (?,?,?)"
    assert a1 == ("x",) and a3 == ("x", "y", "z")


def test_compile_is_cached():
    sql = "SELECT * FROM t WHERE a = $1 AND b = ANY($2)"
    compile_translation.cache_clear()
    compile_translation(sql)
    compile_translation(sql)
    info = compile_translation.cache_info()
    assert info.hits >= 1
