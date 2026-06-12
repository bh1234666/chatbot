from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_events(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tool_family(name: str, event: dict[str, Any]) -> str:
    raw = (name or event.get("kind") or "").lower()
    action = _event_action(event)
    if raw in {"expand_warm", "expand_cold", "expand_kb", "mark_avoid_mention"}:
        return "memory"
    if raw.startswith("workspace_") and not action:
        action = raw.split("_", 1)[1]
    if raw == "workspace" or raw.startswith("workspace_"):
        if action == "run":
            return "execute"
        if action in {"write", "append", "mkdir", "delete", "move", "rename", "edit", "patch"}:
            return "edit"
        if action in {"search", "grep"}:
            return "search"
        if action in {"read", "locate", "list", "inspect"}:
            return "read"
    if raw == "workspace_run":
        return "execute"
    if "delegate" in raw or "helper" in raw:
        return "delegate"
    if "memory" in raw:
        return "memory"
    if "todo" in raw or "plan" in raw:
        return "plan"
    if any(k in raw for k in ("search", "grep")):
        return "search"
    if any(k in raw for k in ("read", "list", "tree", "index", "inspect")):
        return "read"
    if any(k in raw for k in ("edit", "write", "patch", "create", "delete", "move")):
        return "edit"
    command_text = _command_text(event).lower()
    args_blob = json.dumps(_event_args(event), ensure_ascii=False).lower()
    if "browser" in raw:
        return "browser"
    browser_evidence = " ".join((raw, command_text, args_blob))
    if any(
        token in browser_evidence
        for token in (
            "playwright",
            "chromium.launch",
            "firefox.launch",
            "webkit.launch",
            "page.goto",
            "page.click",
            "page.fill",
        )
    ) or re.search(
        r"\bnode(?:\.cmd|\.exe)?\b.*\bverify_[\w.-]*(?:browser|web|page|form)[\w.-]*\.(?:cjs|mjs|js)\b",
        browser_evidence,
    ):
        return "browser"
    if raw == "env_run":
        return "execute"
    if any(k in raw for k in ("bash", "shell", "cmd", "command", "pytest", "python")):
        return "execute"
    return "other"


def _is_mutating(name: str, event: dict[str, Any]) -> bool:
    raw = (name or event.get("kind") or "").lower()
    return any(k in raw for k in ("edit", "write", "patch", "create", "delete", "move", "mkdir"))


def _mutating_for_family(family: str, name: str, event: dict[str, Any]) -> bool:
    if family in {"read", "search", "plan", "delegate"}:
        return False
    if family == "edit":
        return True
    return _is_mutating(name, event)


def _event_has_mutation_evidence(event: dict[str, Any]) -> bool:
    preview = _decode_preview(event.get("result_preview"))
    if not preview:
        return False
    if preview.get("project_mutation_fact") or preview.get("project_mutations"):
        return True
    if preview.get("created_project_files") or preview.get("modified_project_files"):
        return True
    action = str(preview.get("action") or "").lower()
    return bool(preview.get("ok") is True and action in {
        "write", "append", "edit", "multi_edit", "insert", "mkdir",
        "env_apply_create", "env_apply_replace", "replace", "create",
    })


def _command_text(event: dict[str, Any]) -> str:
    for key in ("command", "cmd", "argv", "args", "text", "description", "what_doing"):
        value = event.get(key)
        if value:
            if isinstance(value, list):
                return " ".join(str(item) for item in value)
            return str(value)
    raw = event.get("raw")
    return str(raw) if raw else ""


def _ok_success(event: dict[str, Any]) -> bool:
    if _is_guard_routing_fact(event):
        return True
    if isinstance(event.get("ok"), bool):
        return bool(event["ok"])
    if isinstance(event.get("success"), bool):
        return bool(event["success"])
    return not bool(event.get("error") or event.get("errors"))


def _is_acceptance_failure_evidence(event: dict[str, Any]) -> bool:
    preview = _decode_preview(event.get("result_preview"))
    fact = preview.get("acceptance_failure_fact")
    if isinstance(fact, dict) and fact.get("kind") == "acceptance_failure_fact":
        return True
    return False


def _is_guard_routing_fact(event: dict[str, Any]) -> bool:
    preview = _decode_preview(event.get("result_preview"))
    raw_preview = str(event.get("result_preview") or "")
    reason = str(preview.get("blocked_reason") or preview.get("warning") or preview.get("error") or raw_preview)
    args = _event_args(event)
    tool = str(event.get("tool") or event.get("tool_name") or "").lower()
    path = str(args.get("path") or preview.get("blocked_path") or preview.get("path") or "")
    return bool(
        reason and (
            preview.get("delegate_required") is True
            or reason in {
                "main_thread_env_project_write_block",
                "current_turn_explicitly_read_only",
                "environment_workspace_write_not_project_file",
                "main_thread_project_artifact_should_delegate",
                "use_read_file_for_file_reads",
            }
            or preview.get("error_kind") == "use_read_file_for_file_reads"
            or "Main workflow cannot stage" in reason
            or (
                tool == "workspace"
                and (args.get("action") == "write" or preview.get("blocked_path"))
                and path.replace("\\", "/").startswith("_env/")
                and event.get("ok") is False
            )
        )
    )


def _decode_preview(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _event_args(event: dict[str, Any]) -> dict[str, Any]:
    if isinstance(event.get("args"), dict):
        return dict(event["args"])
    preview_args = _decode_preview(event.get("args_preview"))
    if preview_args:
        return preview_args
    return {}


def _event_action(event: dict[str, Any]) -> str:
    args = _event_args(event)
    action = str(args.get("action") or "").strip().lower()
    if action:
        return action
    preview = _decode_preview(event.get("result_preview"))
    action = str(preview.get("action") or "").strip().lower()
    if action:
        return action
    if str(event.get("tool") or event.get("tool_name") or "").lower() == "workspace":
        reason = str(preview.get("blocked_reason") or "").strip()
        if reason in {
            "environment_workspace_write_not_project_file",
            "main_thread_project_artifact_should_delegate",
        } or preview.get("blocked_path"):
            return "write"
    return ""


def _tool_output(event: dict[str, Any]) -> str:
    direct_values = [
        event.get("output"),
        event.get("stdout"),
        event.get("stderr"),
        event.get("result"),
        event.get("content"),
        event.get("message"),
        event.get("error"),
        event.get("errors"),
    ]
    preview = _decode_preview(event.get("result_preview"))
    pieces: list[str] = []
    for value in direct_values:
        if value:
            pieces.append(str(value))
    if preview:
        for key in ("stdout", "stderr", "output", "message", "error"):
            value = preview.get(key)
            if value:
                pieces.append(str(value))
        if not pieces:
            pieces.append(json.dumps(preview, ensure_ascii=False))
    summary = event.get("result_summary")
    if isinstance(summary, dict):
        pieces.append(json.dumps(summary, ensure_ascii=False))
    return "\n".join(pieces)[:4000]


def _event_command_input(event: dict[str, Any]) -> dict[str, Any]:
    args = _event_args(event)
    preview = _decode_preview(event.get("result_preview"))
    command = (
        args.get("command")
        or args.get("python_code")
        or event.get("command")
        or event.get("cmd")
        or preview.get("command")
        or _command_text(event)
    )
    payload = dict(args)
    if "action" not in payload and preview.get("action"):
        payload["action"] = preview["action"]
    if command:
        payload["cmd"] = str(command)
    if "path" not in payload and preview.get("blocked_path"):
        payload["path"] = preview["blocked_path"]
    for key in ("path", "workspace_path", "source_path"):
        if key not in payload and preview.get(key):
            payload[key] = preview[key]
    if preview.get("returncode") is not None:
        payload["returncode"] = preview.get("returncode")
    if preview.get("timed_out") is not None:
        payload["timed_out"] = preview.get("timed_out")
    return payload


def _sanitize_tool_input_for_trajectory(call: dict[str, Any]) -> dict[str, Any]:
    payload = call.get("input") if isinstance(call.get("input"), dict) else {}
    if not payload:
        return {}
    name = str(call.get("name") or "").lower()
    family = str(call.get("family") or "").lower()
    if family != "edit" and not any(token in name for token in ("edit", "write", "patch", "create")):
        return payload
    keep_keys = {
        "_family_evidence",
        "_preserve_family",
        "action",
        "path",
        "paths",
        "workspace_path",
        "source_path",
        "target_path",
        "expected_hash",
        "source",
        "fact",
    }
    sanitized = {key: value for key, value in payload.items() if key in keep_keys}
    if not sanitized and payload.get("cmd"):
        sanitized["cmd"] = payload["cmd"]
    if any(key in payload for key in ("content", "old_str", "new_str", "replacement", "patch")):
        sanitized["_content_omitted"] = "edit body omitted from trajectory target extraction"
    return sanitized or payload


def _lock_classified_family(call: dict[str, Any]) -> None:
    family = str(call.get("family") or "").lower()
    if family in {"browser", "read", "search", "execute", "plan", "delegate", "memory", "cron", "edit"}:
        payload = call.get("input") if isinstance(call.get("input"), dict) else {}
        payload = dict(payload)
        payload.setdefault("_preserve_family", family)
        call["input"] = payload


def _is_trace_tool_event(event: dict[str, Any]) -> bool:
    kind = str(event.get("kind") or "")
    if kind == "main_tool_done":
        return True
    if kind in {"helper_done", "helper_error"}:
        return True
    return False


def _canonical_tool_name(name: str, event: dict[str, Any]) -> str:
    if name == "workspace":
        action = _event_action(event)
        if action:
            return f"workspace_{action}"
    if name in {"env_inventory", "env_list_tree", "env_fetch", "env_diff"}:
        return f"read_{name}"
    return name


def _normalized_workflow_events(workflow: list[Any]) -> list[dict[str, Any]]:
    return [event for event in workflow if isinstance(event, dict) and _is_trace_tool_event(event)]


def _workflow_tool_call(event: dict[str, Any], index: int) -> dict[str, Any]:
    name = str(
        event.get("tool")
        or event.get("tool_name")
        or event.get("kind")
        or event.get("event")
        or "workflow_event"
    )
    name = _canonical_tool_name(name, event)
    guard_routing_fact = _is_guard_routing_fact(event)
    family = _tool_family(name, event)
    success = True if _is_acceptance_failure_evidence(event) else _ok_success(event)
    mutating = False if guard_routing_fact else _mutating_for_family(family, name, event)
    if success is False and not _event_has_mutation_evidence(event):
        mutating = False
    call = {
        "id": f"wf_{index}",
        "name": name,
        "input": _event_command_input(event),
        "output": _tool_output(event),
        "success": success,
        "family": family,
        "mutating": mutating,
        "error": str(event.get("error") or event.get("errors") or ""),
        "_raw_result_preview": event.get("result_preview"),
    }
    call["input"] = _sanitize_tool_input_for_trajectory(call)
    _lock_classified_family(call)
    if isinstance(event.get("result_summary"), dict):
        call["_event_result_summary"] = event["result_summary"]
    return call


def _command_tool_call(event: dict[str, Any], index: int) -> dict[str, Any]:
    cmd = _command_text(event)
    name = "exec_command"
    call = {
        "id": f"cmd_{index}",
        "name": name,
        "input": {"cmd": cmd, **{k: v for k, v in event.items() if k not in {"output", "stdout", "stderr"}}},
        "output": str(event.get("output") or event.get("stdout") or event.get("stderr") or "")[:4000],
        "success": _ok_success(event),
        "family": "execute",
        "mutating": _is_mutating(cmd, event),
        "error": str(event.get("error") or event.get("errors") or ""),
    }
    _lock_classified_family(call)
    return call


def _command_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _command_done_lookup(commands: list[Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in commands:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "done":
            continue
        key = _command_key(_command_text(item))
        if key:
            out[key] = item
    return out


def _merge_missing_command_result(
    call: dict[str, Any],
    command_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if call.get("output") and call.get("error"):
        return call
    cmd = _command_key((call.get("input") or {}).get("cmd"))
    event = command_lookup.get(cmd) if cmd else None
    if not event:
        return call

    merged = dict(call)
    fallback_output = _tool_output(event)
    if fallback_output and not merged.get("output"):
        merged["output"] = fallback_output
    fallback_error = str(event.get("error") or event.get("errors") or "")
    if fallback_error and not merged.get("error"):
        merged["error"] = fallback_error
    if "success" in merged and merged["success"] is True and _ok_success(event) is False:
        merged["success"] = False
    return merged


def _command_driven_browser_call(call: dict[str, Any]) -> bool:
    if call.get("family") != "browser":
        return False
    name = str(call.get("name") or "").lower()
    if not any(key in name for key in ("run", "exec", "bash", "shell", "cmd", "command")):
        return False
    cmd = str((call.get("input") or {}).get("cmd") or (call.get("input") or {}).get("command") or "")
    evidence = " ".join((name, cmd, str(call.get("output") or ""), str(call.get("error") or ""))).lower()
    return bool(
        cmd.strip()
        and (
            "playwright" in evidence
            or "chromium" in evidence
            or re.search(
                r"\bnode(?:\.cmd|\.exe)?\b.*\bverify_[\w.-]*(?:browser|web|page|form)[\w.-]*\.(?:cjs|mjs|js)\b",
                evidence,
            )
        )
    )


def _command_driven_browser_evidence(call: dict[str, Any]) -> dict[str, Any] | None:
    if call.get("success") is False:
        return None
    name = str(call.get("name") or "").lower()
    if name not in {"env_run", "workspace_run", "workspace_run_execute", "exec_command", "bash"}:
        return None
    payload = call.get("input") if isinstance(call.get("input"), dict) else {}
    command = str(payload.get("python_code") or payload.get("command") or payload.get("cmd") or "")
    output = str(call.get("output") or "")
    evidence = " ".join((name, command, output)).lower()
    if not re.search(r"https?://", evidence):
        return None
    output_low = output.lower()
    # 2026-06-11 Round 16/17: browser-evidence signal definitions are shared
    # with the agent runtime (app.llm.tools.browser_evidence_signals) so the
    # scorer and the agent never drift apart again (p35088: agent ran real
    # Playwright page-text extraction; old HTML/status-only markers missed it
    # and tool_fit collapsed to 0).
    from app.llm.tools.browser_evidence_signals import (
        has_browser_automation_signal,
        has_html_or_http_status_signal,
    )
    browser_or_http_signal = (
        has_browser_automation_signal(command, output)
        or has_html_or_http_status_signal(output_low)
    )
    if not browser_or_http_signal:
        return None
    urls: list[str] = []
    for match in re.findall(r"https?://[^\s\"')\]]+", command + "\n" + output):
        _append_unique_path(urls, match)
    return {
        "urls": urls[:12],
        "source": "command_http_browser_evidence",
        "fact": "A successful execution tool call fetched or inspected an HTTP/browser-visible resource. This is non-mutating browser evidence even if the same command also saved a local staging file.",
    }


def _extract_probe_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"""['"]([^'"]+\.(?:db|sqlite|csv|tsv|json|jsonl|txt|md|py|html|xml|yaml|yml))['"]""", text):
        _append_unique_path(paths, match.group(1))
    return paths[:12]


def _command_driven_read_probe(call: dict[str, Any]) -> dict[str, Any] | None:
    if call.get("family") != "execute" or call.get("mutating") or call.get("success") is False:
        return None
    name = str(call.get("name") or "").lower()
    if name not in {"env_run", "workspace_run", "workspace_run_execute", "exec_command", "bash"}:
        return None
    payload = call.get("input") if isinstance(call.get("input"), dict) else {}
    command = str(payload.get("python_code") or payload.get("command") or payload.get("cmd") or "")
    if not command.strip():
        return None
    evidence = " ".join((name, command, str(call.get("output") or ""))).lower()
    if re.search(r"\b(pytest|test|verify_|npm\s+test|cargo\s+test|go\s+test|compile|build)\b", evidence):
        return None
    mutation_signals = (
        ".write_text", ".write_bytes", " mode='w'", ' mode="w"',
        ",'w'", ',"w"', ",'a'", ',"a"', "insert into", "update ", "delete from",
        "create table", "drop table", "alter table", "shutil.copy", "os.remove", ".unlink(",
    )
    if any(signal in evidence for signal in mutation_signals):
        return None
    if re.search(r"\bopen\s*\([^)]*,\s*['\"][wa+]", evidence):
        return None
    read_signals = (
        "select ", "pragma ", "sqlite3.connect", ".read_text", ".read_bytes",
        "open(", "json.load", "csv.reader", "pandas.read_", "path(", ".exists()",
        ".stat()", "os.listdir", "iterdir", "glob(", "print(",
    )
    shell_read = re.search(r"(^|[;&|]\s*)(cat|type|dir|ls|find|grep|rg|head|tail|wc)\b", evidence)
    if not shell_read and not any(signal in evidence for signal in read_signals):
        return None
    paths = _extract_probe_paths(command)
    if not paths and "sqlite3.connect" in evidence:
        paths = ["<database inspected by env_run>"]
    return {
        "paths": paths,
        "source": "non-mutating command inspection",
        "fact": "A successful execution tool call inspected project data/files and produced read evidence without changing project files.",
    }


def _append_unique_path(paths: list[str], value: Any) -> None:
    norm = str(value or "").replace("\\", "/").strip()
    if norm and norm not in paths:
        paths.append(norm)


def _is_internal_staged_metadata_path(value: Any) -> bool:
    norm = str(value or "").replace("\\", "/").strip().lower()
    return norm in {
        "_env/.manifest.json",
        "_env/.resource_manifest.json",
        "_env/project_inventory.md",
    } or norm.startswith("_env/.backups/")


def _append_unique_project_change_path(paths: list[str], value: Any) -> None:
    if _is_internal_staged_metadata_path(value):
        return
    _append_unique_path(paths, value)


def _delegate_file_change_facts(call: dict[str, Any]) -> dict[str, Any] | None:
    if "delegate" not in str(call.get("name") or "").lower():
        return None
    event_summary = call.get("_event_result_summary")
    if not isinstance(event_summary, dict):
        return None
    paths: list[str] = []
    for key in ("copied_project_files", "main_available_files", "staged_project_files"):
        values = event_summary.get(key)
        if isinstance(values, list):
            for value in values:
                _append_unique_project_change_path(paths, value)
    for item in event_summary.get("result_items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("main_available_files", "staged_project_files"):
            values = item.get(key)
            if isinstance(values, list):
                for value in values:
                    _append_unique_project_change_path(paths, value)
        copy_stats = item.get("copy_stats") if isinstance(item.get("copy_stats"), dict) else {}
        for value in copy_stats.get("env_copied_files") or []:
            _append_unique_project_change_path(paths, value)
    if not paths:
        return None
    return {
        "paths": paths[:20],
        "source": "delegate_result_summary",
        "fact": "delegate/helper result reported project-file copies or staged project files available in the main workspace",
    }


def _delegate_self_verification_facts(call: dict[str, Any]) -> dict[str, Any] | None:
    if "delegate" not in str(call.get("name") or "").lower():
        return None
    if call.get("success") is False:
        return None
    event_summary = call.get("_event_result_summary")
    if not isinstance(event_summary, dict):
        return None
    if event_summary.get("task_ok") is not True:
        return None
    if int(event_summary.get("success_count") or 0) <= 0:
        return None
    if any(int(event_summary.get(key) or 0) > 0 for key in (
        "incomplete_count",
        "failed_count",
        "interrupted_count",
        "resource_required_count",
        "quality_blocked_count",
    )):
        return None
    verified_tasks: list[str] = []
    verified_paths: list[str] = []
    for item in event_summary.get("result_items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("ok") is not True:
            return None
        if item.get("outputs_complete") is not True:
            return None
        task_id = str(item.get("task_id") or "").strip()
        if task_id and task_id not in verified_tasks:
            verified_tasks.append(task_id)
        for key in ("main_available_files", "staged_project_files"):
            values = item.get(key)
            if isinstance(values, list):
                for value in values:
                    _append_unique_project_change_path(verified_paths, value)
        copy_stats = item.get("copy_stats") if isinstance(item.get("copy_stats"), dict) else {}
        for value in copy_stats.get("env_copied_files") or []:
            _append_unique_project_change_path(verified_paths, value)
    if event_summary.get("result_items") and not verified_tasks:
        return None
    for key in ("main_available_files", "staged_project_files", "copied_project_files"):
        values = event_summary.get(key)
        if isinstance(values, list):
            for value in values:
                _append_unique_project_change_path(verified_paths, value)
    return {
        "verified_task_ids": verified_tasks[:20],
        "paths": verified_paths[:20],
        "source": "delegate_result_summary",
        "fact": "A clean helper/delegate completion reported outputs_complete=true; helper self-verification is the producer-owned verification boundary for those outputs.",
    }


def _delegate_browser_evidence_facts(call: dict[str, Any]) -> dict[str, Any] | None:
    if "delegate" not in str(call.get("name") or "").lower():
        return None
    if call.get("success") is False:
        return None
    event_summary = call.get("_event_result_summary")
    if not isinstance(event_summary, dict):
        return None
    facts = event_summary.get("browser_evidence_facts")
    if not isinstance(facts, list) or not facts:
        return None
    task_ids: list[str] = []
    urls: list[str] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        task_id = str(fact.get("task_id") or "").strip()
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
        for url in fact.get("urls") or []:
            _append_unique_path(urls, url)
    return {
        "verified_task_ids": task_ids[:20],
        "urls": urls[:12],
        "source": "delegate_result_summary",
        "fact": (
            "A helper/delegate result reported browser-family evidence from browser automation or a host-browser route. "
            "This is non-mutating producer-owned evidence exposed from the helper summary."
        ),
    }


def _execution_project_mutation_facts(call: dict[str, Any]) -> dict[str, Any] | None:
    if call.get("family") != "execute" or call.get("success") is False:
        return None
    name = str(call.get("name") or "").lower()
    if name not in {"env_run", "workspace_run", "workspace_run_execute", "exec_command", "bash"}:
        return None
    preview = _decode_preview(call.get("_raw_result_preview"))
    event_summary = call.get("_event_result_summary")
    if isinstance(event_summary, dict):
        preview = {**preview, **event_summary}
    if not preview:
        return None
    paths: list[str] = []
    mutation_fact = preview.get("project_mutation_fact")
    if isinstance(mutation_fact, dict):
        for key in ("created_project_files", "modified_project_files", "deleted_project_files"):
            values = mutation_fact.get(key)
            if isinstance(values, list):
                for value in values:
                    _append_unique_project_change_path(paths, value)
    project_mutations = preview.get("project_mutations")
    if isinstance(project_mutations, dict):
        for key in ("created", "modified", "deleted", "created_project_files", "modified_project_files"):
            values = project_mutations.get(key)
            if isinstance(values, list):
                for value in values:
                    _append_unique_project_change_path(paths, value)
    for key in ("created_project_files", "modified_project_files", "deleted_project_files"):
        values = preview.get(key)
        if isinstance(values, list):
            for value in values:
                _append_unique_project_change_path(paths, value)
    if not paths:
        return None
    return {
        "paths": paths[:20],
        "source": "execution_project_mutation_fact",
        "fact": "A successful execution tool result reported project file creations or modifications.",
    }


def _helper_owned_apply_verification_facts(call: dict[str, Any]) -> dict[str, Any] | None:
    if call.get("success") is False:
        return None
    name = str(call.get("name") or "").lower()
    if name not in {"env_apply_create", "env_apply_replace"}:
        return None
    payload = call.get("input") if isinstance(call.get("input"), dict) else {}
    preview = _decode_preview(call.get("_raw_result_preview"))
    output_preview = _decode_preview(call.get("output"))
    merged = {**preview, **output_preview}
    action = str(merged.get("action") or name).lower()
    if action not in {"env_apply_create", "env_apply_replace"}:
        return None
    acceptance_fact = merged.get("acceptance_fact")
    helper_owned = False
    if isinstance(acceptance_fact, dict):
        helper_owned = acceptance_fact.get("helper_owned") is True
    if not helper_owned:
        return None
    path = str(merged.get("path") or payload.get("path") or "").strip()
    workspace_path = str(merged.get("workspace_path") or payload.get("workspace_path") or "").strip()
    paths = [value for value in (path, workspace_path) if value]
    return {
        "paths": paths[:8],
        "source": "helper_owned_env_apply",
        "fact": (
            "A successful env apply used a helper-owned staged source. The producer-owned validation boundary "
            "continues after the apply; coordinator acceptance should consume helper/apply facts rather than "
            "re-reading or revalidating content in the main thread."
        ),
    }


def _expand_dual_family_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for call in calls:
        expanded.append(call)
        browser_evidence_facts = _command_driven_browser_evidence(call)
        if browser_evidence_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_browser"
            companion["name"] = f"{call.get('name') or 'tool'}_browser_evidence"
            companion["family"] = "browser"
            companion["mutating"] = False
            companion["success"] = True
            companion["input"] = {
                "_preserve_family": "browser",
                "_family_evidence": "execution fetched or inspected an HTTP/browser-visible resource",
                **browser_evidence_facts,
            }
            companion["output"] = json.dumps(browser_evidence_facts, ensure_ascii=False)
            expanded.append(companion)
        execution_edit_facts = _execution_project_mutation_facts(call)
        if execution_edit_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_edit"
            companion["name"] = f"{call.get('name') or 'tool'}_project_mutations"
            companion["family"] = "edit"
            companion["mutating"] = True
            companion["success"] = bool(call.get("success", True))
            companion["input"] = {
                "_preserve_family": "edit",
                "_family_evidence": "execution result reported project file mutation facts",
                **execution_edit_facts,
            }
            companion["output"] = json.dumps(execution_edit_facts, ensure_ascii=False)
            expanded.append(companion)
        delegate_edit_facts = _delegate_file_change_facts(call)
        if delegate_edit_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_edit"
            companion["name"] = f"{call.get('name') or 'delegate'}_file_changes"
            companion["family"] = "edit"
            companion["mutating"] = True
            companion["success"] = bool(call.get("success", True))
            companion["input"] = {
                "_preserve_family": "edit",
                "_family_evidence": "helper/delegate file-change facts",
                **delegate_edit_facts,
            }
            companion["output"] = json.dumps(delegate_edit_facts, ensure_ascii=False)
            expanded.append(companion)
        delegate_verify_facts = _delegate_self_verification_facts(call)
        if delegate_verify_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_execute"
            companion["name"] = f"{call.get('name') or 'delegate'}_self_verification"
            companion["family"] = "execute"
            companion["mutating"] = False
            companion["success"] = True
            companion["input"] = {
                "_preserve_family": "execute",
                "_family_evidence": "helper/delegate clean completion and outputs_complete facts",
                **delegate_verify_facts,
            }
            companion["output"] = json.dumps(delegate_verify_facts, ensure_ascii=False)
            expanded.append(companion)
        delegate_browser_facts = _delegate_browser_evidence_facts(call)
        if delegate_browser_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_browser"
            companion["name"] = f"{call.get('name') or 'delegate'}_browser_evidence"
            companion["family"] = "browser"
            companion["mutating"] = False
            companion["success"] = True
            companion["input"] = {
                "_preserve_family": "browser",
                "_family_evidence": "helper/delegate browser-family evidence facts",
                **delegate_browser_facts,
            }
            companion["output"] = json.dumps(delegate_browser_facts, ensure_ascii=False)
            expanded.append(companion)
        helper_apply_verify_facts = _helper_owned_apply_verification_facts(call)
        if helper_apply_verify_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_producer_boundary"
            companion["name"] = f"{call.get('name') or 'env_apply'}_producer_boundary"
            companion["family"] = "execute"
            companion["mutating"] = False
            companion["success"] = True
            companion["input"] = {
                "_preserve_family": "execute",
                "_family_evidence": "helper-owned staged apply preserved producer verification boundary",
                **helper_apply_verify_facts,
            }
            companion["output"] = json.dumps(helper_apply_verify_facts, ensure_ascii=False)
            expanded.append(companion)
        read_probe_facts = _command_driven_read_probe(call)
        if read_probe_facts is not None:
            companion = dict(call)
            companion["id"] = f"{call.get('id') or 'tool'}_read"
            companion["name"] = f"{call.get('name') or 'tool'}_read_probe"
            companion["family"] = "read"
            companion["mutating"] = False
            companion["input"] = {
                "_preserve_family": "read",
                "_family_evidence": "execution output contained non-mutating project data/file inspection",
                **read_probe_facts,
            }
            companion["output"] = json.dumps(read_probe_facts, ensure_ascii=False)
            expanded.append(companion)
        if not _command_driven_browser_call(call):
            continue
        companion = dict(call)
        companion["id"] = f"{call.get('id') or 'tool'}_execute"
        companion["name"] = f"{call.get('name') or 'tool'}_execute"
        companion["family"] = "execute"
        companion["mutating"] = False
        companion_input = dict(call.get("input") or {})
        companion_input["_preserve_family"] = "execute"
        companion_input["_family_evidence"] = "command execution for browser automation"
        companion["input"] = companion_input
        expanded.append(companion)
    return expanded


def tool_calls_from_events(workflow: list[Any], commands: list[Any]) -> list[dict[str, Any]]:
    workflow_records = _normalized_workflow_events(workflow)
    tool_calls = [
        _workflow_tool_call(item, index)
        for index, item in enumerate(workflow_records)
    ]
    # Workflow completion events already contain the command, return code and
    # stdout/stderr preview. Raw command stream events are lower-level start/done
    # telemetry and can duplicate commands without their result output.
    if tool_calls:
        command_lookup = _command_done_lookup(commands)
        if command_lookup:
            return _expand_dual_family_calls([
                _merge_missing_command_result(call, command_lookup)
                for call in tool_calls
            ])
        return _expand_dual_family_calls(tool_calls)
    return _expand_dual_family_calls([
        _command_tool_call(item, index)
        for index, item in enumerate(commands)
        if isinstance(item, dict) and str(item.get("kind") or "") == "done"
    ])


def _exportable_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in call.items()
        if not str(key).startswith("_")
    }


def _trace_for_call(run_dir: Path, run_start: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    turn = event.get("turn")
    trace_id = (
        (event.get("meta") or {}).get("trace_id")
        or (event.get("done") or {}).get("trace_id")
        or f"{run_dir.name}-turn-{turn}"
    )
    workflow = event.get("workflow") or []
    commands = event.get("command_events") or []
    tool_calls = tool_calls_from_events(workflow, commands)
    export_tool_calls = [_exportable_tool_call(call) for call in tool_calls]
    tests = []
    for call in tool_calls:
        cmd = str(call.get("input", {}).get("cmd") or "")
        if re.search(r"\b(pytest|compileall|python)\b", cmd) or re.search(
            r"\bnode(?:\.cmd|\.exe)?\b.*\bverify_[\w.-]*\.(?:cjs|mjs|js)\b",
            cmd,
            re.I,
        ):
            tests.append({
                "name": cmd[:200] or call["name"],
                "passed": bool(call.get("success")),
                "exit_code": 0 if call.get("success") else None,
            })
    return {
        "trace_id": str(trace_id),
        "created_at": _iso_utc(),
        "partner_name": "chatbot-local",
        "privacy_tier": "local_private",
        "harness": {
            "type": "chatbot_environment",
            "name": "F:/chatbot stress_tools app_clone",
            "version": "local",
            "entrypoint": "stress_tools/run_app_clone_maintenance.py",
        },
        "model": {
            "provider": "deepseek",
            "name": "configured-by-project-env",
        },
        "config": {
            "approval_mode": "local_harness",
            "sandbox_mode": "app_clone_workspace",
            "duration_min": run_start.get("duration_min"),
        },
        "plugins": [
            {
                "id": "chatbot-environment-tools",
                "name": "Chatbot Environment Tools",
                "enabled": True,
                "tools": sorted({str(call.get("name")) for call in tool_calls if call.get("name")}),
            }
        ],
        "skills": [],
        "prompts": {
            "user": event.get("message") or "",
        },
        "transcript": {
            "messages": [
                {"role": "user", "text": event.get("message") or ""},
                {
                    "role": "assistant",
                    "text": event.get("text") or "",
                    "tool_calls": export_tool_calls,
                },
            ]
        },
        "tests": tests,
        "artifacts": {
            "final_status": "pass" if event.get("ok") and (event.get("quality") or {}).get("ok", True) else "partial",
            "final_message": event.get("text") or "",
            "commands": [
                {"cmd": str(call.get("input", {}).get("cmd") or ""), "success": call.get("success")}
                for call in export_tool_calls
                if call.get("name") == "exec_command"
            ],
            "tests": tests,
        },
        "redaction": {
            "applied": True,
            "policy": "local-no-secrets",
            "notes": "API keys and .env content are not exported.",
        },
        "metadata": {
            "run_dir": str(run_dir),
            "turn": turn,
            "latency_sec": event.get("latency_sec"),
            "quality": event.get("quality"),
            "event_counts": event.get("event_counts"),
            "project": run_start.get("project"),
            "trace_completeness": "full" if tool_calls else "message_only",
        },
    }


def export(run_dir: Path, output: Path | None = None) -> Path:
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        raise FileNotFoundError(events_path)
    events = _load_events(events_path)
    run_start = next((event for event in events if event.get("kind") == "run_start"), {})
    calls = [event for event in events if event.get("kind") == "environment_call"]
    output = output or (run_dir / "clawbench_partner_traces.jsonl")
    with output.open("w", encoding="utf-8") as handle:
        for event in calls:
            handle.write(json.dumps(_trace_for_call(run_dir, run_start, event), ensure_ascii=False) + "\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    out = export(args.run_dir, args.output)
    print(out)


if __name__ == "__main__":
    main()
