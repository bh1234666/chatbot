"""
M6 结构检查：群文件同步 + KB 索引 + 智能体查询链路。

覆盖：
- group_files 模块完整 API
- synced_files 表结构
- kb 文件索引函数
- bot_config active_archive 管理
- context 文件列表显示
- bridge observe/sync 路径
- api 端点注册
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent / "app"
BRIDGE = Path(__file__).parent.parent / "napcat_bridge.py"
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


# ── group_files 模块 ──────────────────────────────────────────
check("group_files module exists", (ROOT / "memory/group_files.py").exists())
gf = get_funcs(parse("memory/group_files.py"))
gf_src = (ROOT / "memory/group_files.py").read_text(encoding="utf-8")

# 核心函数
for name in [
    "fetch_group_files",      # NapCat API 调用
    "sync_group_files",       # 两阶段同步主入口
    "fetch_group_file",       # 按需提取到工作区
]:
    check(f"group_files.{name}", name in gf)

# 阶段 1：快速索引
for name in ["_create_pending_kb_node"]:
    check(f"group_files.{name}", name in gf)

# 阶段 2：后台下载
for name in ["_bg_download_and_index", "_download_file",
             "_mark_download_failed", "_heal_pending_nodes"]:
    check(f"group_files.{name}", name in gf)

# 内容提取
for name in ["_should_extract_content", "_extract_text",
             "_safe_filename", "_fmt_size"]:
    check(f"group_files.{name}", name in gf)

# 配置常量
for kw in [
    "MAX_DOWNLOAD_SIZE", "MAX_CONTENT_EXTRACT_SIZE",
    "SYNC_COOLDOWN_SEC", "MAX_BG_DOWNLOADS",
    "NAPCAT_URL",
]:
    check(f"group_files has {kw}", kw in gf_src)

# 两阶段设计的关键标识
check("group_files uses download_status pending",
      'download_status": "pending"' in gf_src)
check("group_files updates to done",
      'download_status": "done"' in gf_src)

# ── synced_files 迁移表 ───────────────────────────────────────
mig_files = list(MIG.glob("*.sql"))
check("migrations dir exists", len(mig_files) > 0)
synced_mig = None
for mf in mig_files:
    if "synced_files" in mf.read_text(encoding="utf-8"):
        synced_mig = mf
        break
check("synced_files migration exists", synced_mig is not None)
if synced_mig:
    sql = synced_mig.read_text(encoding="utf-8")
    for kw in ["CREATE TABLE", "synced_files", "archive_id", "group_id", "file_id",
               "workspace_path", "kb_node_id", "PRIMARY KEY"]:
        check(f"synced_files migration has {kw}", kw in sql)

# ── kb.py 文件索引 ────────────────────────────────────────────
kb = get_funcs(parse("memory/kb.py"))
check("kb.load_file_index", "load_file_index" in kb)
check("kb.index_group_file", "index_group_file" in kb)
check("kb.search_files", "search_files" in kb)

kb_src = (ROOT / "memory/kb.py").read_text(encoding="utf-8")
check("kb load_file_index reads download_status",
      "download_status" in kb_src)
check("kb load_file_index reads file_metadata",
      "file_metadata" in kb_src)
check("kb index_group_file uses lite model",
      "lite=True" in kb_src or "lite=" in kb_src)

# ── bot_config ─────────────────────────────────────────────────
check("bot_config exists", (ROOT / "memory/bot_config.py").exists())
bc = get_funcs(parse("memory/bot_config.py"))
for name in ["get_active_archive", "join_group"]:
    check(f"bot_config.{name}", name in bc)

# ── context 文件列表显示 ───────────────────────────────────────
ctx_src = (ROOT / "core/context.py").read_text(encoding="utf-8")
check("context has ## 群组文件 section", "群组文件" in ctx_src)
check("context shows download_status pending", 'pending' in ctx_src and ('下载中' in ctx_src or 'pending files are not fetchable' in ctx_src))
check("context shows download_status failed",
      'failed' in ctx_src and ('下载失败' in ctx_src or 'failed files are unavailable' in ctx_src))
check("context shows indexed file fetch hint", "fetch_indexed_file" in ctx_src or "fetch_group_file" in ctx_src)
check("context shows file metadata fields",
      "filename" in ctx_src and "uploader_name" in ctx_src and "file_size" in ctx_src)

# ── orchestrator 加载 file_index ───────────────────────────────
orch_src = source("core/orchestrator.py", "core/orchestrator_entry.py")
check("orchestrator calls load_file_index",
      "load_file_index(" in orch_src)
check("orchestrator passes file_index to context",
      "file_index=" in orch_src)

# ── tools/registry.py fetch_group_file ─────────────────────────
reg_src = (ROOT / "llm/tools/registry.py").read_text(encoding="utf-8")
# 2026-05-20: schema 定义已抽到 tool_schemas.py。合并双源以兼容(名字经 re-export 仍可见,
# 定义体在 tool_schemas.py)。位置型 split 检查改用「最后一次出现之后」的片段。
_ts_path = ROOT / "llm/tools/tool_schemas.py"
if _ts_path.exists():
    reg_src = reg_src + "\n" + _ts_path.read_text(encoding="utf-8")
check("registry has FETCH_GROUP_FILE_SCHEMA",
      "FETCH_GROUP_FILE_SCHEMA" in reg_src)
check("registry imports group_files",
      "from app.memory import group_files" in reg_src or "gf_mem" in reg_src)
check("registry has fetch_group_file in ROUND2_TOOLS",
      '"fetch_group_file"' in reg_src)
check("registry handler calls fetch_group_file",
      "fetch_group_file(" in reg_src)
# 工具描述提到 download_status
check("registry tool desc mentions download_status",
      "pending entries are not fetchable" in reg_src.split("FETCH_GROUP_FILE_SCHEMA")[-1][:3000]
      if "FETCH_GROUP_FILE_SCHEMA" in reg_src else True)

# ── api 端点 ──────────────────────────────────────────────────
check("api/group_files.py exists", (ROOT / "api/group_files.py").exists())
gf_api = (ROOT / "api/group_files.py").read_text(encoding="utf-8")
check("group_files api has sync endpoint",
      "group-files/sync" in gf_api)
check("group_files api calls sync_group_files",
      "sync_group_files(" in gf_api)

# ── main 注册 ─────────────────────────────────────────────────
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("main registers group_files router", "group_files" in main_src and "router" in main_src)

# ── bridge observe/sync 路径 ───────────────────────────────────
check("bridge exists", BRIDGE.exists())
bridge_src = BRIDGE.read_text(encoding="utf-8")
check("bridge has _sync_group_files_fire_and_forget",
      "_sync_group_files_fire_and_forget" in bridge_src)
check("bridge has _observe_message", "_observe_message" in bridge_src)
check("bridge has _check_participate", "_check_participate" in bridge_src)
# 关键修复：observe 和 sync 使用 active_aid
check("bridge observe uses active_aid",
      "_observe_message(client, active_aid" in bridge_src)
check("bridge sync uses active_aid",
      "_sync_group_files_fire_and_forget(group_id, active_aid)" in bridge_src)

# ── 数据库 schema ─────────────────────────────────────────────
# file_metadata 列在 005_file_kb.sql 中添加
mig005 = MIG / "005_file_kb.sql"
check("005_file_kb.sql exists", mig005.exists())
if mig005.exists():
    sql005 = mig005.read_text(encoding="utf-8")
    check("005_file_kb adds file_metadata", "file_metadata" in sql005)
init_sql = (MIG / "001_init.sql").read_text(encoding="utf-8")
check("cold_nodes has scope column", "scope" in init_sql)
check("cold_nodes has node_type column", "node_type" in init_sql)

print("\n=== M6 (group files sync) structural checks passed ===")
