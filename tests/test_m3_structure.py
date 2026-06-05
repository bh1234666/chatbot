"""
M3 结构检查：
- cold 完整 API（含压缩、展开、索引）
- kb 完整 API（含 maybe_compress_kb）
- orchestrator 接入新接口
- context 显示 type
- 运营 API 增加 cold/kb 端点
- schema 已去除 pgvector 依赖
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent / "app"
MIG = Path(__file__).parent.parent / "migrations"


def parse(rel):
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def get_funcs(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            kwonly = [a.arg for a in node.args.kwonlyargs]
            out[node.name] = args + kwonly
    return out


def source(*rels):
    return "\n".join((ROOT / rel).read_text(encoding="utf-8") for rel in rels)


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ── cold 完整 API ──
cold = get_funcs(parse("memory/cold.py"))
for name in [
    "load_cold_user_index", "load_cold_group_index",
    "expand_cold",
    "compress_user_warm_to_cold", "compress_group_warm_to_cold",
    "_compress_warm_to_cold",
    "delete_cold",
    "_eff_sql",
    "topk_cold_user", "topk_cold_group",  # 兼容旧名
]:
    check(f"cold.{name}", name in cold)

# ── kb 完整 API ──
kb = get_funcs(parse("memory/kb.py"))
for name in [
    "load_kb_index", "expand_kb",
    "maybe_compress_kb",
    "load_file_index", "index_group_file", "search_files",
    "topk_kb",  # 兼容旧名
]:
    check(f"kb.{name}", name in kb)

# ── warm 增量：溢出查询 ──
warm = get_funcs(parse("memory/warm.py"))
for name in [
    "get_user_warm_overflow", "get_group_warm_overflow",
    "delete_warm_by_ids",
]:
    check(f"warm.{name}", name in warm)

# ── orchestrator 接入 ──
orch_src = source("core/orchestrator.py", "core/orchestrator_entry.py")
for kw in [
    "cold.load_cold_user_index",
    "cold.load_cold_group_index",
    "kb.load_kb_index",
    "cold.compress_user_warm_to_cold",
    "cold.compress_group_warm_to_cold",
    "kb.maybe_compress_kb",
]:
    check(f"orchestrator uses {kw}", kw in orch_src)
# 三路并行
check("orchestrator gathers M3 tasks",
      "asyncio.gather(" in orch_src and "_user_warm_to_cold()" in orch_src)

# ── context 显示 type ──
ctx_src = (ROOT / "core/context.py").read_text(encoding="utf-8")
check("context shows cold type", '({t})' in ctx_src or '("type")' in ctx_src or "(c.get(\"type\")" in ctx_src)
check("context links expand_cold", "expand_cold" in ctx_src)
check("context links expand_kb", "expand_kb" in ctx_src)

# ── 运营 API 扩展 ──
api_src = (ROOT / "api/memory.py").read_text(encoding="utf-8")
for kw in [
    "/memory/cold", "/memory/cold/expand",
    "/memory/kb", "/memory/kb/expand",
    "ColdExpandRequest",
]:
    check(f"memory api has {kw}", kw in api_src)

# ── config 新增项 ──
cfg_src = (ROOT / "config.py").read_text(encoding="utf-8")
for kw in [
    "cold_user_index_topn", "cold_group_index_topn", "kb_index_topn",
    "kb_compress_threshold", "kb_compress_batch",
    "salience_half_life_days", "salience_access_boost",
    "warm_to_cold_batch",
]:
    check(f"config has {kw}", kw in cfg_src)

# ── schema 调整 ──
sql = (MIG / "001_init.sql").read_text(encoding="utf-8")
# 去 pgvector
check("schema no pgvector ext", "CREATE EXTENSION" not in sql or "vector" not in sql.split("CREATE EXTENSION")[1][:200])
check("schema no embedding column", "embedding " not in sql)
check("schema no ivfflat", "ivfflat" not in sql)
# 新结构
check("schema source_refs", "source_refs" in sql)
check("schema last_access on cold_nodes",
      "last_access" in sql.split("CREATE TABLE IF NOT EXISTS cold_nodes")[1].split(");")[0])
# 三个 scope 索引
for kw in ["idx_cold_user", "idx_cold_group", "idx_cold_kb", "idx_cold_edges_dst"]:
    check(f"schema index {kw}", kw in sql)
# FK ON DELETE CASCADE
check("schema cold_edges FK cascade",
      "ON DELETE CASCADE" in sql and "REFERENCES cold_nodes" in sql)

# ── 函数签名关键参数 ──
for fn in ["compress_user_warm_to_cold", "compress_group_warm_to_cold"]:
    args = cold[fn]
    check(f"{fn} has archive_id", "archive_id" in args)
    check(f"{fn} has group_id", "group_id" in args)

# ── _eff_sql 不出现 SQL 注入风险（只插入 float） ──
import importlib.util, sys
sys.path.insert(0, str(ROOT.parent))
# 直接 exec _eff_sql 函数体
src = (ROOT / "memory/cold.py").read_text(encoding="utf-8")
# 检查 _eff_sql 内只用了 settings.salience_half_life_days * 86400.0
import re
m = re.search(r"def _eff_sql\(\)[^:]*:(.*?)def ", src, re.DOTALL)
assert m, "_eff_sql not found"
fn_body = m.group(1)
check("_eff_sql uses settings only",
      "salience_half_life_days" in fn_body and "user_input" not in fn_body)

# ── tool_registry 已经在 M2.5 接好 dispatch；这里确认它能调到真实实现 ──
reg_src = (ROOT / "llm/tools/registry.py").read_text(encoding="utf-8")
check("registry calls cold_mem.expand_cold",
      "cold_mem.expand_cold(" in reg_src)
check("registry calls kb_mem.expand_kb",
      "kb_mem.expand_kb(" in reg_src)

print("\n=== M3 structural checks passed ===")
