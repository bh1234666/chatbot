"""
不依赖运行时的结构检查：
- 每个模块包含期望的函数/类
- 函数签名形参一致（编排器调用方与被调用方对得上）
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent / "app"


def parse(path):
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def get_funcs(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            kwonly = [a.arg for a in node.args.kwonlyargs]
            out[node.name] = args + kwonly
    return out


def source(*paths):
    return "\n".join((ROOT / path).read_text(encoding="utf-8") for path in paths)


def check(label, cond, detail=""):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}{(' :: ' + detail) if not cond and detail else ''}")
    assert cond, label


# ── memory.hot 的 API ──
hot = get_funcs(parse("memory/hot.py"))
check("hot.append_user_turn", "append_user_turn" in hot)
check("hot.load_user_hot", "load_user_hot" in hot)
check("hot.get_user_hot_overflow", "get_user_hot_overflow" in hot)
check("hot.append_group_event", "append_group_event" in hot)
check("hot.load_group_hot", "load_group_hot" in hot)

# ── memory.warm stub ──
warm = get_funcs(parse("memory/warm.py"))
for name in ["load_user_warm_index", "load_group_warm_index", "expand_warm"]:
    check(f"warm.{name}", name in warm)
warm_src = (ROOT / "memory/warm.py").read_text(encoding="utf-8")
check("warm.compress_overflow (alias)", "compress_overflow" in warm_src)

# ── memory.cold stub ──
cold = get_funcs(parse("memory/cold.py"))
for name in ["topk_cold_user", "topk_cold_group", "expand_cold"]:
    check(f"cold.{name}", name in cold)

# ── memory.kb stub + 新增 ──
kb = get_funcs(parse("memory/kb.py"))
for name in ["topk_kb", "expand_kb", "load_file_index", "index_group_file", "search_files", "cleanup_stale_file_placeholders"]:
    check(f"kb.{name}", name in kb)

# ── memory.archive ──
arc = get_funcs(parse("memory/archive.py"))
for name in ["create_archive", "get_archive", "soft_delete_archive",
             "upsert_persona", "get_persona", "get_persona_full"]:
    check(f"archive.{name}", name in arc)

# ── memory.group_files ──
check("group_files module exists", (ROOT / "memory/group_files.py").exists())
gm2 = get_funcs(parse("memory/group_messages.py"))
for name in ["load_recent", "load_unprocessed", "claim_unprocessed", "release_processing", "mark_processed"]:
    check(f"group_messages.{name}", name in gm2)
gf = get_funcs(parse("memory/group_files.py"))
for name in ["fetch_group_files", "sync_group_files", "fetch_group_file",
             "_create_pending_kb_node", "_bg_download_and_index",
             "_download_file", "_should_extract_content", "_extract_text",
             "_safe_filename", "_heal_pending_nodes"]:
    check(f"group_files.{name}", name in gf)

# ── memory.bot_config ──
check("bot_config module exists", (ROOT / "memory/bot_config.py").exists())
bc = get_funcs(parse("memory/bot_config.py"))
for name in ["get_active_archive", "join_group"]:
    check(f"bot_config.{name}", name in bc)

# ── core.context ──
ctx = get_funcs(parse("core/context.py"))
for name in ["build_base_context", "round1_messages", "round2_messages", "round3_messages"]:
    check(f"context.{name}", name in ctx)
# 参数包含 file_index
ctx_args = ctx["build_base_context"]
expected_args = ["user_name", "current_message", "hot_user", "hot_group",
                 "warm_user_index", "warm_group_index",
                 "cold_user_topk", "cold_group_topk", "kb_topk", "file_index"]
for a in expected_args:
    check(f"build_base_context has '{a}'", a in ctx_args)
# Shared Files section
ctx_src = (ROOT / "core/context.py").read_text(encoding="utf-8")
for kw in ["Shared Files", "file_index", "download_status", "fetch_indexed_file"]:
    check(f"context has '{kw}'", kw in ctx_src)

# ── core.orchestrator ──
orch = get_funcs(parse("core/orchestrator.py"))
orch_entry = get_funcs(parse("core/orchestrator_entry.py"))
orchestrator_facade_src = (ROOT / "core/orchestrator.py").read_text(encoding="utf-8")
check("orchestrator.orchestrate", "orchestrate" in orch or "orchestrator_entry import orchestrate" in orchestrator_facade_src)
check("orchestrator_entry.orchestrate", "orchestrate" in orch_entry)
orch_src = source("core/orchestrator.py", "core/orchestrator_entry.py")
check("orchestrator calls load_file_index", "load_file_index" in orch_src)
check("orchestrator passes file_index to context",
      "file_index=file_index" in orch_src or "file_index=" in orch_src)
check("round2 stage module exists", (ROOT / "core/round2_stage.py").exists())
round2_src = (ROOT / "core/round2_stage.py").read_text(encoding="utf-8")
check("round2 stage table moved out of orchestrator", "R2_STAGE_TABLE: dict" not in orch_src)
check("round2 stage module defines stage table", "R2_STAGE_TABLE" in round2_src)
check("language module exists", (ROOT / "core/language.py").exists())
lang = get_funcs(parse("core/language.py"))
for name in ["detect_user_language", "language_directive"]:
    check(f"language.{name}", name in lang)
check("language helpers moved out of orchestrator", "def _detect_user_language" not in orch_src and "def _language_directive" not in orch_src)
check("recall audit module exists", (ROOT / "core/recall_audit.py").exists())
ra = get_funcs(parse("core/recall_audit.py"))
check("recall_audit.recall_audit_recall_used", "recall_audit_recall_used" in ra)
check("recall audit moved out of orchestrator", "def _recall_audit_recall_used" not in orch_src)
check("bot_log module exists", (ROOT / "core/bot_log.py").exists())
bl = get_funcs(parse("core/bot_log.py"))
check("bot_log.build_bot_log", "build_bot_log" in bl)
check("bot_log moved out of orchestrator", "def _build_bot_log" not in orch_src)
check("message routing module exists", (ROOT / "core/message_routing.py").exists())
mr = get_funcs(parse("core/message_routing.py"))
for name in ["has_implicit_recall_intent", "is_negative_feedback", "is_trivial_message"]:
    check(f"message_routing.{name}", name in mr)
check("message routing moved out of orchestrator",
      "def _has_implicit_recall_intent" not in orch_src and "def _is_negative_feedback" not in orch_src and "def _is_trivial_message" not in orch_src)
check("meta judge state module exists", (ROOT / "core/meta_judge_state.py").exists())
mj = get_funcs(parse("core/meta_judge_state.py"))
for name in ["record_cross_llm_outcome", "should_skip_cross_llm", "reset_cross_llm_outcomes"]:
    check(f"meta_judge_state.{name}", name in mj)
check("meta judge state moved out of orchestrator",
      "def _record_cross_llm_outcome" not in orch_src and "def _should_skip_cross_llm" not in orch_src)
check("plan helpers module exists", (ROOT / "core/plan_helpers.py").exists())
ph = get_funcs(parse("core/plan_helpers.py"))
for name in ["fallback_plan_from_user", "build_recall_hint"]:
    check(f"plan_helpers.{name}", name in ph)
check("plan helpers moved out of orchestrator",
      "def _fallback_plan_from_user" not in orch_src and "def _build_recall_hint" not in orch_src)
check("delegate cleanup module exists", (ROOT / "core/delegate_cleanup.py").exists())
dc = get_funcs(parse("core/delegate_cleanup.py"))
for name in ["cleanup_cross_user_delegate_dirs", "cleanup_old_same_user_delegate_dirs"]:
    check(f"delegate_cleanup.{name}", name in dc)
check("delegate cleanup moved out of orchestrator",
      "def _cleanup_cross_user_delegate_dirs" not in orch_src and "def _cleanup_old_same_user_delegate_dirs" not in orch_src)
check("inline_images module exists", (ROOT / "core/inline_images.py").exists())
ii = get_funcs(parse("core/inline_images.py"))
check("inline_images.scan_inline_images", "scan_inline_images" in ii)
check("inline image scan moved out of orchestrator", "def _scan_inline_images" not in orch_src)
check("workspace_lifecycle module exists", (ROOT / "core/workspace_lifecycle.py").exists())
wl = get_funcs(parse("core/workspace_lifecycle.py"))
check("workspace_lifecycle.delayed_workspace_unregister", "delayed_workspace_unregister" in wl)
check("workspace unregister moved out of orchestrator", "def _delayed_workspace_unregister" not in orch_src)
check("helper_activity module exists", (ROOT / "core/helper_activity.py").exists())
ha = get_funcs(parse("core/helper_activity.py"))
for name in ["scan_active_helpers", "request_active_helpers_finalize"]:
    check(f"helper_activity.{name}", name in ha)
check("helper activity moved out of orchestrator",
      "def _scan_active_helpers" not in orch_src and "def _request_active_helpers_finalize" not in orch_src)
check("pause_snapshot module exists", (ROOT / "core/pause_snapshot.py").exists())
ps = get_funcs(parse("core/pause_snapshot.py"))
check("pause_snapshot.collect_and_save_pause_snapshot", "collect_and_save_pause_snapshot" in ps)
check("pause snapshot moved out of orchestrator", "def _collect_and_save_pause_snapshot" not in orch_src)
check("recovery_log module exists", (ROOT / "core/recovery_log.py").exists())
rl = get_funcs(parse("core/recovery_log.py"))
check("recovery_log.write_recovery_jsonl", "write_recovery_jsonl" in rl)
check("recovery jsonl moved out of orchestrator", "def _write_recovery_jsonl" not in orch_src)
check("user_profile_maintenance module exists", (ROOT / "core/user_profile_maintenance.py").exists())
upm = get_funcs(parse("core/user_profile_maintenance.py"))
check("user_profile_maintenance.bg_user_profile_update", "bg_user_profile_update" in upm)
check("user profile update moved out of orchestrator", "def _bg_user_profile_update" not in orch_src)
check("post_response_maintenance module exists", (ROOT / "core/post_response_maintenance.py").exists())
prm = get_funcs(parse("core/post_response_maintenance.py"))
check("post_response_maintenance.post_response_maintenance", "post_response_maintenance" in prm)
check("post response maintenance moved out of orchestrator", "def _post_response_maintenance" not in orch_src)
check("command_risk module exists", (ROOT / "llm/tools/command_risk.py").exists())
cr = get_funcs(parse("llm/tools/command_risk.py"))
for name in ["analyze_command", "extract_paths", "is_abs_outside", "has_redirect_to_outside", "touches_prev_or_outside"]:
    check(f"command_risk.{name}", name in cr)
check("tool_meta module exists", (ROOT / "llm/tools/tool_meta.py").exists())
tm = get_funcs(parse("llm/tools/tool_meta.py"))
for name in ["schema_name", "tool_meta", "schemas_for_main_thread", "meta_by_name", "validate_aliases"]:
    check(f"tool_meta.{name}", name in tm)


# ── llm.client ──
llm = get_funcs(parse("llm/client.py"))
for name in ["chat_json", "chat_stream", "chat_with_tools"]:
    check(f"llm.{name}", name in llm)
check("llm.tool_pairing module exists", (ROOT / "llm/tool_pairing.py").exists())
tp = get_funcs(parse("llm/tool_pairing.py"))
check("tool_pairing.repair_tool_call_pairing", "repair_tool_call_pairing" in tp)
client_src = (ROOT / "llm/client.py").read_text(encoding="utf-8")
check("tool pairing moved out of client", "def repair_tool_call_pairing" not in client_src and "def _tool_call_id" not in client_src)

# ── tools/registry.py 有 fetch_group_file ──
reg_src = (ROOT / "llm/tools/registry.py").read_text(encoding="utf-8")
for kw in ["FETCH_GROUP_FILE_SCHEMA", "fetch_group_file", "_handle_fetch_group_file",
           "gf_mem"]:
    check(f"registry has '{kw}'", kw in reg_src)

# ── api.group_files ──
check("api.group_files exists", (ROOT / "api/group_files.py").exists())
gf_api_src = (ROOT / "api/group_files.py").read_text(encoding="utf-8")
check("group_files api has sync endpoint", "group-files/sync" in gf_api_src)

# ── main 注册 routers ──
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("main registers group_files router", "group_files.router" in main_src)

# ── bridge lifecycle ──
bridge_src = (Path(__file__).parent.parent / "napcat_bridge.py").read_text(encoding="utf-8")
check("bridge uses lifespan cleanup", "async def _start_cleanup" in bridge_src and "await _start_cleanup()" in bridge_src)
check("bridge does not use deprecated on_event", "@app.on_event" not in bridge_src)

print("\n=== all structural checks passed ===")

