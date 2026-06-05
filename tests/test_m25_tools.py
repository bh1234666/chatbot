"""
M2.5 结构检查（Round2 工具支持）。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent / "app"


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


# ── 工具模块存在 ──
for path in [
    "llm/tools/__init__.py",
    "llm/tools/_python_worker.py",
    "llm/tools/python_exec.py",
    "llm/tools/registry.py",
]:
    check(f"file: {path}", (ROOT / path).exists())

# ── python_exec 接口 ──
exec_funcs = get_funcs(parse("llm/tools/python_exec.py"))
check("python_exec.run_python", "run_python" in exec_funcs)

# ── registry ──
reg_src = (ROOT / "llm/tools/registry.py").read_text(encoding="utf-8")
# 2026-05-20 重构：schema 定义已抽到 tool_schemas.py。
#   - schema 常量名（PYTHON_TOOL_SCHEMA 等）经 registry re-export 仍在 registry.py 文本中可见；
#   - schema 内容串（'name": "python"' 等）随字典移入 tool_schemas.py。
# 故对 registry.py + tool_schemas.py 合并文本做检查，兼容抽取前后两种布局。
_schemas_path = ROOT / "llm/tools/tool_schemas.py"
_schemas_src = _schemas_path.read_text(encoding="utf-8") if _schemas_path.exists() else ""
_tools_src = reg_src + "\n" + _schemas_src
for kw in [
    "PYTHON_TOOL_SCHEMA", "EXPAND_WARM_SCHEMA", "EXPAND_COLD_SCHEMA",
    "EXPAND_KB_SCHEMA", "FETCH_GROUP_FILE_SCHEMA", "ROUND2_TOOLS",
    'name": "python"', 'name": "expand_warm"',
    'name": "expand_cold"', 'name": "expand_kb"',
    'name": "fetch_group_file"',
]:
    check(f"registry/tool_schemas has {kw}", kw in _tools_src)

reg_funcs = get_funcs(parse("llm/tools/registry.py"))
check("registry.dispatch", "dispatch" in reg_funcs)
check("registry._handle_fetch_group_file", "_handle_fetch_group_file" in reg_funcs)

# registry imports group_files
check("registry imports group_files", "from app.memory import group_files" in reg_src or "gf_mem" in reg_src)

# ── llm.client 新循环 ──
cli_funcs = get_funcs(parse("llm/client.py"))
loop_funcs = get_funcs(parse("llm/client_tools_loop.py"))
check("client.chat_with_tools_loop", "chat_with_tools_loop" in cli_funcs or "chat_with_tools_loop" in loop_funcs)
check("client.chat_with_tools (legacy)", "chat_with_tools" in cli_funcs)

# loop 函数应有 dispatcher 关键字参数
loop_args = cli_funcs.get("chat_with_tools_loop") or loop_funcs["chat_with_tools_loop"]
for arg in ["messages", "tools", "dispatcher", "reasoning", "abort_event", "lite"]:
    check(f"chat_with_tools_loop arg '{arg}'", arg in loop_args)

# ── orchestrator round2 接入工具 ──
orch_src = source("core/orchestrator.py", "core/orchestrator_entry.py")
check("orchestrator imports tool registry",
      "from app.llm.tools.registry import" in orch_src)
check("orchestrator uses chat_with_tools_loop",
      "chat_with_tools_loop" in orch_src)
check("orchestrator passes archive_id to round2",
      "_round2(" in orch_src and "archive_id=req.archive_id" in orch_src)

# round2 函数签名
orch_funcs = get_funcs(parse("core/orchestrator.py"))
r2_args = orch_funcs["_round2"]
for arg in ["base_msgs", "tendency", "archive_id", "group_id", "user_id", "progress_cb"]:
    check(f"_round2 has '{arg}'", arg in r2_args)

# ── round2 prompt 提到工具 ──
ctx_src = (ROOT / "core/context.py").read_text(encoding="utf-8")
rp_src = (ROOT / "core/round_prompts.py").read_text(encoding="utf-8")
# v2 架构:主线程不写代码,FIX_HINT 已从 prompt 移除; ROUND2_SYSTEM_TEMPLATE 迁到 round_prompts.py
for kw in ["expand_cold", "expand_kb", "workspace", "工具", "delegate", "fetch_to_temp"]:
    check(f"round2 prompt mentions '{kw}'", kw in ctx_src or kw in rp_src)

# ── _python_worker 安全配置 ──
worker_src = (ROOT / "llm/tools/_python_worker.py").read_text(encoding="utf-8")
for kw in ["DENIED_NAMES", "SAFE_BUILTINS",
           "RLIMIT_CPU", "RLIMIT_AS",
           "validate", "SecurityError"]:
    check(f"worker has {kw}", kw in worker_src)
# 黑名单关键项必须存在
for bad in ["eval", "exec", "compile", "__import__"]:
    check(f"worker DENIED_NAMES contains '{bad}'",
          bad in worker_src.split("DENIED_NAMES")[1][:500] if "DENIED_NAMES" in worker_src else False)

# ── app.schemas.api 结构 ──
api_src = (ROOT / "schemas/api.py").read_text(encoding="utf-8")
for cls in ["ChatRequest"]:
    check(f"api schema has {cls}", f"class {cls}" in api_src)

print("\nAll checks passed\n")
