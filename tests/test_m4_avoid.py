"""
M4 软遗忘结构检查：
- schema 加 avoid_mention 字段 + node_user_avoid 表
- cold/kb 索引和展开返回 avoid_mention/avoid_reason
- apply_avoid_mention/list_avoided_for_user/unmark_avoid_for_user 函数
- mark_avoid_mention 工具注册
- viewer_user_id 在群组冷/KB 路径上正确传递
- context 显示 [AVOID] 标记，prompt 教模型如何处理
- 没有 forget/delete-memory 端点（只能"不主动提及"，不能真删）
- 倾向维度增加"遗忘请求"
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


# ── Schema ──
sql = (MIG / "001_init.sql").read_text(encoding="utf-8")
check("schema cold_nodes.avoid_mention", "avoid_mention" in sql and ("BOOLEAN" in sql or "INTEGER NOT NULL DEFAULT 0" in sql))
check("schema cold_nodes.avoid_reason", "avoid_reason" in sql)
check("schema node_user_avoid table", "CREATE TABLE IF NOT EXISTS node_user_avoid" in sql)
check("schema node_user_avoid PK",
      "PRIMARY KEY (archive_id, user_id, node_id)" in sql)
check("schema node_user_avoid FK cascade",
      "REFERENCES cold_nodes(id) ON DELETE CASCADE" in
      sql.split("CREATE TABLE IF NOT EXISTS node_user_avoid")[1].split(";")[0])
check("schema idx_node_avoid_lookup",
      "idx_node_avoid_lookup" in sql)

# ── cold 模块 ──
cold = get_funcs(parse("memory/cold.py"))
for name in [
    "apply_avoid_mention",
    "list_avoided_for_user",
    "unmark_avoid_for_user",
    "_eff_sql_aliased",
]:
    check(f"cold.{name}", name in cold)

# load_cold_group_index / expand_cold 接受 viewer_user_id
g_idx_args = cold["load_cold_group_index"]
check("load_cold_group_index has viewer_user_id",
      "viewer_user_id" in g_idx_args)
exp_args = cold["expand_cold"]
check("expand_cold has viewer_user_id",
      "viewer_user_id" in exp_args)

# ── kb 模块 ──
kb = get_funcs(parse("memory/kb.py"))
for name in ["load_kb_index", "expand_kb"]:
    check(f"kb.{name}", name in kb)
kb_idx_args = kb["load_kb_index"]
check("load_kb_index has viewer_user_id",
      "viewer_user_id" in kb_idx_args)
kb_exp_args = kb["expand_kb"]
check("expand_kb has viewer_user_id",
      "viewer_user_id" in kb_exp_args)

# ── 工具 ──
reg_src = (ROOT / "llm/tools/registry.py").read_text(encoding="utf-8")
# 2026-05-20: schema 定义已抽到 tool_schemas.py。合并双源以兼容(schema 名经 registry re-export
# 仍可见;定义体在 tool_schemas.py)。位置型 split 检查改用「最后一次出现之后」的片段。
_ts_path = ROOT / "llm/tools/tool_schemas.py"
if _ts_path.exists():
    reg_src = reg_src + "\n" + _ts_path.read_text(encoding="utf-8")
check("MARK_AVOID_SCHEMA exists", "MARK_AVOID_SCHEMA" in reg_src)
# v2: MAIN_THREAD_TOOL_METAS 包含 MARK_AVOID_SCHEMA；ROUND2_TOOLS 由 schemas_for_main_thread 生成
check("MAIN_THREAD_TOOL_METAS includes MARK_AVOID_SCHEMA",
      "tool_meta(MARK_AVOID_SCHEMA" in reg_src)
check("ROUND2_TOOLS generated from MAIN_THREAD_TOOLS",
      "ROUND2_TOOLS = MAIN_THREAD_TOOLS" in reg_src)
check("MARK_AVOID_SCHEMA has mark_avoid_mention name",
      '"mark_avoid_mention"' in reg_src.split("MARK_AVOID_SCHEMA")[-1][:300] if "MARK_AVOID_SCHEMA" in reg_src else False)
check("dispatcher handles mark_avoid_mention",
      'name == "mark_avoid_mention"' in reg_src)
check("_handle_mark_avoid exists",
      "_handle_mark_avoid" in reg_src)
check("mark_avoid_mention is async (bg_tasks.schedule)",
      "schedule(" in reg_src)
# expand_cold/expand_kb 把 user_id 传下去做 viewer_user_id
check("dispatch passes user_id to expand_cold",
      "viewer_user_id=user_id" in reg_src)

# ── orchestrator 传 viewer_user_id ──
orch = source("core/orchestrator.py", "core/orchestrator_entry.py")
check("orchestrator passes viewer_user_id to cold group",
      "load_cold_group_index" in orch and "viewer_user_id=req.user_id" in orch)
check("orchestrator passes viewer_user_id to kb",
      "load_kb_index" in orch and "viewer_user_id=req.user_id" in orch)

# ── context 模板 ──
ctx = (ROOT / "core/context.py").read_text(encoding="utf-8")
rp_src = (ROOT / "core/round_prompts.py").read_text(encoding="utf-8")
_ctx_all = ctx + rp_src
check("context shows [AVOID] mark", "[AVOID]" in _ctx_all)
check("context safety section explains AVOID",
      "[AVOID]" in _ctx_all and ("标记" in _ctx_all or "mark" in _ctx_all.lower()))
check("context tells model not to expand AVOID",
      "不要 expand" in _ctx_all or "not be proactively raised" in _ctx_all or "do not expand" in _ctx_all)
check("context warns no lying for AVOID",
      "不撒谎" in _ctx_all or "否认" in _ctx_all or "do not deny" in _ctx_all)

# Round1 倾向维度
check("round1 has 遗忘请求 dimension",
      "遗忘请求" in _ctx_all)

# Round2 工具说明 — mark_avoid_mention 工具由 registry schema 注入，不在上下文模板里
check("round2 mentions mark_avoid_mention tool (in registry schema)",
      "mark_avoid_mention" in (ROOT / "llm/tools/registry.py").read_text(encoding="utf-8"))
check("round2 says not to deep-dive AVOID",
      "不主动展开" in _ctx_all or "不要调 expand" in _ctx_all or "not be proactively raised" in _ctx_all or "do not expand" in _ctx_all)
check("round2 explains memory not deleted",
      "The memory still exists" in _ctx_all or "still acknowledge memory" in _ctx_all or "nodes remain stored" in _ctx_all)

# ── API 端点 ──
api = (ROOT / "api/memory.py").read_text(encoding="utf-8")
for kw in [
    "/avoid-mention",
    "AvoidMentionRequest",
    "request_avoid_mention",
    "list_avoid_mention",
    "cancel_avoid_mention",
]:
    check(f"memory api has {kw}", kw in api)

# ── 关键不变量：没有真正的"删除记忆/forget"端点 ──
all_api_files = list((ROOT / "api").glob("*.py"))
for f in all_api_files:
    text = f.read_text(encoding="utf-8")
    # /forget 端点不允许出现
    check(f"{f.name}: no /forget endpoint",
          '"/forget"' not in text and "/forget" not in text)

# 验证 apply_avoid_mention 的 LLM 系统 prompt 强调"不删除"
cold_src = (ROOT / "memory/cold.py").read_text(encoding="utf-8")
check("apply_avoid_mention prompt: no delete",
      "不会被删除" in cold_src or "不删除" in cold_src)
check("apply_avoid_mention writes to both tables (user direct + node_user_avoid)",
      "node_user_avoid" in cold_src and "avoid_mention = TRUE" in cold_src)

print("\n=== M4 (soft-forget) structural checks passed ===")
