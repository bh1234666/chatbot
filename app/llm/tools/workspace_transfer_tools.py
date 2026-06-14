from __future__ import annotations

import json
import os

from app.core import debug
from app.llm.tools import workspace as ws_tool
from app.llm.tools.workspace_utils import _derive_permanent_root


def _compact_string_list(value: object, *, max_items: int = 20, max_chars_each: int = 260) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if len(text) > max_chars_each:
            text = text[:max_chars_each].rstrip() + "..."
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _load_helper_task_contract(workspace_dir: str) -> dict | None:
    if not workspace_dir:
        return None
    path = os.path.join(workspace_dir, ".helper_task_contract.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "error": f"could not read .helper_task_contract.json: {type(exc).__name__}",
        }
    if not isinstance(raw, dict):
        return {"error": ".helper_task_contract.json is not a JSON object"}
    return {
        "task_id": str(raw.get("task_id") or "")[:160],
        "helper_kind": str(raw.get("helper_kind") or "")[:80],
        "helper_mode": str(raw.get("helper_mode") or "")[:80],
        "resume": bool(raw.get("resume")),
        "expected_outputs": _compact_string_list(raw.get("expected_outputs"), max_items=20),
        "write_scopes": _compact_string_list(raw.get("write_scopes"), max_items=16),
        "acceptance_checks": _compact_string_list(raw.get("acceptance_checks"), max_items=16),
        "goal_excerpt": str(raw.get("goal_excerpt") or "")[:1200],
        "source": ".helper_task_contract.json",
        "truth_scope": (
            "Factual helper task envelope persisted at helper start. Use it to recall the delegated boundary; "
            "it is not an automatic success/failure decision."
        ),
    }


def _infer_main_and_temp(workspace_dir: str) -> tuple[str, str, str] | None:
    norm = os.path.normpath(workspace_dir)
    base = os.path.basename(norm)
    parent = os.path.dirname(norm)
    parent_base = os.path.basename(parent)
    if base == ".temp":
        return parent, norm, norm
    if base.startswith("_delegate_") and parent_base == ".temp":
        return os.path.dirname(parent), parent, norm
    main_ws = _derive_permanent_root(norm)
    if main_ws:
        if base.startswith("_delegate_"):
            return main_ws, parent, norm
        return main_ws, norm, norm
    return None


async def handle_commit_to_main(workspace_dir: str, args: dict) -> str:
    args = args or {}
    paths = args.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return json.dumps({
            "ok": False,
            "error": "commit_to_main requires `paths` (non-empty list of strings)",
        })
    paths = [str(p).strip() for p in paths if str(p).strip()]
    if not paths:
        return json.dumps({"ok": False, "error": "no valid paths after stripping"})
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "no active workspace"})

    inferred = _infer_main_and_temp(workspace_dir)
    if not inferred:
        return json.dumps({
            "ok": False,
            "error": (
                f"can't infer main workspace from cwd={workspace_dir} "
                "(layered workspace may be disabled, so commit_to_main is a no-op here; "
                "normal deliverables can still be promoted by plan.deliverables).\n"
                "无法推断主工作区时，本次 commit_to_main 不执行。"
            ),
        }, ensure_ascii=False)

    main_ws, _, _ = inferred
    promoted, skipped, name_remap = ws_tool.promote_to_main(main_ws, workspace_dir, paths)
    return json.dumps({
        "ok": True,
        "action": "commit_to_main",
        "promoted": promoted,
        "skipped": skipped,
        "name_remap": name_remap,
        "main_workspace": main_ws,
        "note": (
            f"Promoted {len(promoted)} / {len(paths)} file(s) to the main workspace. "
            "Skipped files may be missing, internal delegate files, or invalid paths. "
            "Files listed in plan.deliverables are promoted again at conversation end without duplicating existing promotions.\n"
            "已将可提升文件复制到主工作区。"
        ),
    }, ensure_ascii=False)


async def handle_fetch_to_temp(workspace_dir: str, args: dict) -> str:
    args = args or {}
    source = str(args.get("source", "main")).strip().lower()
    paths = args.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return json.dumps({"ok": False, "error": "fetch_to_temp requires `paths` (non-empty list)"})
    if source not in ("main", "prev"):
        return json.dumps({"ok": False, "error": f"source must be 'main' or 'prev', got {source!r}"})
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "no active workspace"})

    inferred = _infer_main_and_temp(workspace_dir)
    if not inferred:
        return json.dumps({"ok": False, "error": f"can't infer workspace layout from cwd={workspace_dir}"})
    main_ws, _, effective_ws = inferred

    normalized_paths = paths
    env_aliases: dict[str, str] = {}
    if source == "main":
        try:
            from app.core.runtime_mode import current_environment, is_environment_mode
            if is_environment_mode() and current_environment() is not None:
                fixed_paths: list[str] = []
                for raw_path in paths:
                    raw_text = str(raw_path or "").replace("\\", "/").strip().strip('"').strip("'")
                    if raw_text == "_env" or raw_text.startswith("_env/"):
                        project_rel = raw_text[5:] if raw_text.startswith("_env/") else ""
                        if project_rel:
                            fixed_paths.append(project_rel)
                            env_aliases[project_rel] = raw_text
                            continue
                    if (
                        raw_text
                        and not raw_text.startswith((".", "/"))
                        and ":" not in raw_text
                        and not raw_text.startswith(("_helpers_shared/", "_delegate_", ".temp/", ".prev/"))
                    ):
                        fixed_paths.append(raw_text)
                        env_aliases[raw_text] = f"_env/{raw_text}"
                        continue
                    fixed_paths.append(raw_path)
                normalized_paths = fixed_paths
        except Exception:
            normalized_paths = paths

    copied, skipped = ws_tool.fetch_to_temp(
        main_ws,
        effective_ws,
        paths=normalized_paths,
        source=source,
    )
    env_copied: list[str] = []
    if source == "main" and env_aliases:
        try:
            from app.core.runtime_mode import current_environment, is_environment_mode
            from app.llm.tools.environment import _handle_fetch
            if is_environment_mode() and current_environment() is not None:
                remaining = []
                for skipped_path in skipped:
                    alias = env_aliases.get(str(skipped_path).replace("\\", "/"))
                    if not alias:
                        remaining.append(skipped_path)
                        continue
                    result = _handle_fetch(effective_ws, {"path": skipped_path})
                    if result.get("ok"):
                        staged = result.get("workspace_path") or alias
                        env_copied.append(staged)
                        copied.append(staged)
                    else:
                        remaining.append(alias)
                skipped = remaining
        except Exception:
            skipped = [env_aliases.get(str(p).replace("\\", "/"), p) for p in skipped]

    skipped_with_suggestions = []
    src_root = main_ws if source == "main" else os.path.join(main_ws, ".prev")
    for p in skipped:
        suggestions = ws_tool._suggest_similar_files(src_root, p, limit=3)
        skipped_with_suggestions.append({
            "path": p,
            "suggestions": [s["path"] for s in suggestions],
        })

    note = f"Copied {len(copied)}/{len(paths)} file(s) from {source} to the current workspace."
    if env_copied:
        note += f" {len(env_copied)} environment path(s) were staged through _env aliases."
    if skipped:
        note += f" {len(skipped)} path(s) were skipped because they were missing or invalid."
        suggestions_summary = [
            f"{entry['path']} -> maybe: {', '.join(entry['suggestions'])}"
            for entry in skipped_with_suggestions
            if entry["suggestions"]
        ]
        if suggestions_summary:
            note += "\nSimilar-name suggestions:\n  " + "\n  ".join(suggestions_summary)
    note += "\n已将可用文件复制到当前工作区，并在缺失时返回相似文件建议。"

    return json.dumps({
        "ok": True,
        "action": "fetch_to_temp",
        "source": source,
        "copied": copied,
        "skipped": skipped,
        "skipped_details": skipped_with_suggestions,
        "normalized_paths": normalized_paths if normalized_paths != paths else None,
        "env_copied": env_copied,
        "note": note,
    }, ensure_ascii=False)


async def handle_recall_thread(workspace_dir: str, args: dict) -> str:
    from app.core.core_processes import get_current_thread_context

    ctx = get_current_thread_context()
    out: dict = {
        "ok": True,
        "action": "recall_thread",
        "role": ctx.role_label if ctx else "unknown",
    }

    if ctx:
        out["original_user_message"] = ctx.user_message or ""
        out["plan"] = {
            "intent": ctx.plan_intent or "(plan is still being generated)",
            "key_points": ctx.plan_key_points,
            "deliverables": ctx.plan_deliverables,
            "markers": dict(getattr(ctx, "plan_markers", None) or {}),
        }
    else:
        out["original_user_message"] = "(thread context is not available)"
        out["plan"] = None

    if workspace_dir:
        helper_contract = _load_helper_task_contract(workspace_dir)
        if helper_contract is not None:
            out["helper_task_contract"] = helper_contract

        todo_path = os.path.join(workspace_dir, ".todo.json")
        if os.path.isfile(todo_path):
            try:
                with open(todo_path, "r", encoding="utf-8") as f:
                    todos_obj = json.load(f)
                if isinstance(todos_obj, dict):
                    items = todos_obj.get("todos") or []
                else:
                    items = todos_obj if isinstance(todos_obj, list) else []
                done = [t for t in items if isinstance(t, dict) and t.get("status") == "completed"]
                in_progress = [t for t in items if isinstance(t, dict) and t.get("status") == "in_progress"]
                pending = [t for t in items if isinstance(t, dict) and t.get("status") == "pending"]
                out["todos"] = {
                    "completed_count": len(done),
                    "in_progress": [(t.get("content") or "")[:80] for t in in_progress],
                    "pending": [(t.get("content") or "")[:80] for t in pending[:10]],
                    "pending_more": max(0, len(pending) - 10),
                }
            except (OSError, json.JSONDecodeError):
                out["todos"] = {"error": "could not read .todo.json"}
        else:
            out["todos"] = {"note": "no todos written yet"}

    try:
        from app.core import agent_state
        status = agent_state.structured_status(debug.current_trace_id() or "")
        out["agent_state"] = {
            "contracts": status.get("contracts") or [],
            "verified_evidence_recent": status.get("verified_evidence_recent") or [],
            "artifacts_ready": status.get("artifacts_ready") or [],
            "blocked_work": status.get("blocked_work") or [],
            "ready_to_resume_work": status.get("ready_to_resume_work") or [],
        }
    except Exception:
        out["agent_state"] = {"note": "agent_state unavailable"}

    out["next_step_hint"] = (
        "After reading original_user_message, plan, and todos, ask yourself: "
        "(1) Is the current tool chain still moving toward the original request? "
        "(2) Is there an in_progress todo? Finish it before opening a new branch. "
            "(3) Do ready artifacts and background work evidence satisfy the acceptance points and expected final deliverables, not merely exist? "
            "(4) For long source materials, prefer coverage summaries and line ranges over loading full evidence into the main context. "
            "(5) If background work stages are complete but a requested final artifact, final document, or verification report is absent, start the next assembly or verification stage. "
        "(6) Only when the original request's acceptance points are satisfied should you produce the plan JSON and stop calling tools.\n\n"
        "先核对原始请求、计划、todo、契约、最终产物和验收覆盖；阶段完成不等于整体完成，长材料优先看摘要和分段范围。"
    )
    return json.dumps(out, ensure_ascii=False)
