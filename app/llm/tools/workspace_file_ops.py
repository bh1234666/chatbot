"""Workspace file read/write/edit/search handlers."""
from __future__ import annotations

import os
from pathlib import Path
import hashlib

from app.core.filesystem import FileKind, FileRegistry, FileStatus, PathZone, classify_path
from app.llm.tools.output_spill import write_tool_output_spill
from app.llm.tools.workspace_utils import _derive_permanent_root


def _sync_workspace_globals() -> None:
    from app.llm.tools import workspace as _workspace
    globals().update({
        name: value
        for name, value in vars(_workspace).items()
        if not name.startswith("__") and name not in {
            "handle_write",
            "_extract_python_outline",
            "_build_file_outline",
            "_detect_file_type",
            "_check_file_readable",
            "handle_inspect_file",
            "_track_edit_count",
            "_read_text_safely",
            "handle_read_file",
            "handle_edit_file",
            "handle_multi_edit",
            "handle_insert_in_file",
            "handle_search_in_file",
        }
    })


def _range_is_covered_by_previous_reads(
    start_line: int,
    end_line: int,
    *,
    total_lines: int,
    already_read_full: bool,
    fragments: list,
) -> tuple[bool, str, list[list[int]]]:
    """Return whether a requested read range is duplicate evidence.

    This is a context-budget guard, not a task decision. It only fires when the
    exact requested lines were already exposed by earlier read_file results.
    """
    if start_line < 1 or end_line < start_line:
        return False, "", []
    normalized: list[list[int]] = []
    if already_read_full:
        normalized.append([1, total_lines])
    for item in fragments or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            a = max(1, int(item[0]))
            b = min(total_lines, int(item[1]))
        except (TypeError, ValueError):
            continue
        if b >= a:
            normalized.append([a, b])
    if not normalized:
        return False, "", []
    normalized.sort()
    merged: list[list[int]] = []
    for a, b in normalized:
        if not merged or a > merged[-1][1] + 1:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    for a, b in merged:
        if a <= start_line and end_line <= b:
            reason = "covered_by_prior_full_read" if a == 1 and b >= total_lines else "covered_by_prior_fragment_read"
            return True, reason, merged[-8:]
    return False, "", merged[-8:]


def _helper_env_write_contract_error(path: str) -> dict | None:
    """Keep helper writes inside the declared `_env/` project output contract.

    Helpers may still write scratch files and `_helpers_shared/` coordination
    artifacts. The guard only applies to project copies under `_env/`, where an
    unexpected write can make sibling helpers disagree about ownership.
    """
    classification = classify_path(path)
    norm = classification.normalized
    if classification.zone != PathZone.STAGED_FILE:
        return None
    try:
        from app.core.core_processes import (
            current_helper_expected_outputs,
            current_helper_kind,
            current_helper_write_scopes,
        )
        helper_kind = current_helper_kind()
        declared = tuple(current_helper_expected_outputs() or ())
        write_scopes = tuple(current_helper_write_scopes() or ())
    except Exception:
        return None
    if not helper_kind or (not declared and not write_scopes):
        return None

    def _clean(value: str) -> str:
        return str(value or "").replace("\\", "/").lstrip("./").rstrip("/")

    target = _clean(norm)
    allowed: set[str] = set()
    for output in tuple(write_scopes or ()) + tuple(declared or ()):
        out = _clean(output)
        if not out:
            continue
        allowed.add(out)
        if not out.startswith("_env/"):
            allowed.add(f"_env/{out}")

    for out in allowed:
        if target == out or target.startswith(out + "/"):
            return None
    return {
        "ok": False,
        "error": (
            f"helper kind={helper_kind!r} cannot write project copy {target!r} because it is not in this "
            "helper's declared expected_outputs. The matching recovery facts are an expanded helper contract or "
            "a matching helper assignment before editing it. Files already declared in expected_outputs remain owned; "
            "do not write a same-named scratch file at the sandbox root as a substitute for this project path.\n\n"
            "当前 helper 只能修改自己 expected_outputs 声明的 _env 项目文件；其它项目文件需要匹配的归属事实。"
        ),
        "blocked_reason": "helper_env_write_outside_expected_outputs",
        "blocked_path": target,
        "expected_outputs": sorted(allowed),
        "matching_helper_kind": "code",
        "observed_recovery_tool": "request_resource",
        "observed_recovery_shape": {
            "resource_kind": "code",
            "reason": "need ownership for additional project file",
            "needed_outputs": [target],
        },
        "suggested_tool": "request_resource",
        "suggested_request": (
            "request_resource(kind='code', reason='need ownership for additional project file', "
            "needed_outputs=['" + target + "'])"
        ),
    }


def _existing_env_project_copy_write_fact(ws_dir: str, path: str) -> dict | None:
    """Return facts for whole-file overwrites of existing staged project copies."""
    classification = classify_path(path)
    norm = classification.normalized
    if classification.zone != PathZone.STAGED_FILE:
        return None
    try:
        from app.core.core_processes import current_helper_kind
        if current_helper_kind():
            return None
    except Exception:
        pass
    try:
        target = _safe_resolve(ws_dir, norm)
    except Exception:
        return None
    if not os.path.exists(target):
        return None
    return {
        "staged_project_copy": True,
        "staged_project_path": norm,
        "staged_overwrite_fact": (
            f"Fact: workspace.write replaced the staged project copy {norm!r}, not the real project file. "
            "Use env_diff to inspect the staged change and env_apply_replace to apply it to the project when appropriate. "
            "A renamed_previous backup, when present, is only the previous staged copy."
        ),
        "pending_project_apply_fact": (
            f"The real project file for {norm!r} is unchanged until env_apply_replace applies this staged copy."
        ),
        "suggested_next_tools": ["env_diff", "env_apply_replace"],
    }


def _existing_env_project_copy_edit_fact(ws_dir: str, path: str, operation: str) -> dict | None:
    fact = _existing_env_project_copy_write_fact(ws_dir, path)
    if fact is None:
        return None
    norm = fact.get("staged_project_path") or path
    return {
        **fact,
        "staged_edit_fact": (
            f"Fact: {operation} modified the staged project copy {norm!r}, not the real project file. "
            "Use env_diff to inspect the staged change and env_apply_replace to apply it to the real project when appropriate."
        ),
        "pending_project_apply_fact": (
            f"The real project file for {norm!r} is unchanged until env_apply_replace applies this staged copy."
        ),
    }


def _main_env_new_project_artifact_write_error(path: str, content: str) -> dict | None:
    """Block main workflow from staging substantial new project artifacts."""
    classification = classify_path(path)
    norm = classification.normalized
    if classification.zone != PathZone.STAGED_FILE:
        return None
    try:
        from app.core.core_processes import current_helper_kind
        if current_helper_kind():
            return None
    except Exception:
        pass
    project_rel = norm[len("_env/"):]
    if not project_rel or project_rel.startswith("."):
        return None
    suffix = os.path.splitext(project_rel.lower())[1]
    lower_name = os.path.basename(project_rel).lower()
    is_source = suffix in {
        ".py", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
        ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
        ".kt", ".swift", ".rb", ".php", ".scala", ".lua", ".sql",
    }
    is_project_text = suffix in {
        ".md", ".markdown", ".txt", ".json", ".jsonl", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".csv", ".tsv", ".xml",
    } and (
        any(token in lower_name for token in (
            "contract", "framework", "spec", "outline", "schema", "report",
            "paper", "benchmark", "analysis", "results", "requirements",
            "design", "architecture", "plan",
        ))
        or any(part in project_rel.lower().replace("\\", "/") for part in (
            "/docs/", "/reports/", "/paper/", "/benchmark/", "/_shared/",
        ))
    )
    if not is_source and not is_project_text:
        return None
    if is_source and suffix == ".py" and len(content) < 1200:
        return None
    if is_project_text and len(content) < 1800 and len([line for line in content.splitlines() if line.strip()]) < 45:
        return None
    return {
        "ok": False,
        "error": (
            f"Main workflow cannot stage a substantial new project artifact directly at {norm!r}. "
            "A staged `_env/...` file can later be applied to the real project, so substantial source, reports, "
            "contracts, data, and framework documents need helper ownership and a ready provenance record. "
            "No file was written. The response reports the path, artifact type, expected output, and acceptance "
            "facts so the model can decide whether to delegate, reduce the write to a compact coordination note, "
            "or state that no project artifact is needed.\n\n"
            "事实：主流程未直接写入可应用到项目的新 `_env` 大文件；返回恢复事实，由模型决定后续动作。"
        ),
        "blocked_reason": "main_thread_env_project_artifact_should_delegate",
        "blocked_path": norm,
        "delegate_required": True,
        "recovery_facts": {
            "matching_helper_kind": "code" if is_source else "edit",
            "mode": "easy",
            "framework_fact": "<shared framework, file ownership, inputs, and acceptance checks>",
            "input_files": [],
            "helper_prompt_fact": (
                f"Create this focused project artifact as a helper-owned staged output: {project_rel}. "
                "If this is a shared contract or outline for later helpers, keep it compact and declare whether "
                "it is only an internal handoff or should later be applied as a project file. Keep the helper scope "
                "limited to this artifact and the current framework."
            ),
            "expected_outputs": [norm],
            "acceptance_checks": [
                "outputs_complete is true",
                "the staged file is inspected or locally checked before apply",
                "scope matches only this artifact and the current shared framework",
            ],
        },
    }


_PROJECT_TEXT_ARTIFACT_EXTS = {
    ".md", ".markdown", ".txt", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".xml",
}


def _looks_like_main_project_text_artifact(path: str, content: str) -> bool:
    """Mirror env_apply_create's direct-content delegation shape for hints."""
    if not path or not content:
        return False
    suffix = Path(path).suffix.lower()
    if suffix not in _PROJECT_TEXT_ARTIFACT_EXTS:
        return False
    norm = path.replace("\\", "/").lower()
    basename = Path(norm).name
    if basename in {"notes.txt", "note.txt"} and len(content) <= 600:
        return False
    project_tokens = (
        "api", "architecture", "benchmark", "changelog", "change",
        "contract", "design", "doc", "docs", "framework", "guide",
        "instructions", "manual", "migration", "notes", "outline",
        "overview", "paper", "plan", "readme", "reference", "report",
        "requirements", "results", "schema", "spec", "tutorial", "usage",
    )
    if any(token in basename for token in project_tokens):
        return True
    if any(part in norm for part in ("/docs/", "/reports/", "/paper/", "/benchmark/", "/_shared/")):
        return True
    if len(content) >= 1800:
        return True
    nonempty_lines = [line for line in content.splitlines() if line.strip()]
    return len(nonempty_lines) >= 45


def _preserve_blocked_workspace_project_candidate(ws_dir: str, *, target_rel: str, content: str) -> dict:
    """Save blocked main-authored workspace.write content for helper handoff."""
    if not ws_dir or not content:
        return {}
    try:
        root = Path(ws_dir).resolve()
        safe_rel = target_rel.replace("\\", "/").lstrip("/").replace("..", "__")
        candidate = root / "_env" / ".blocked_creates" / safe_rel
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        rel = str(candidate.relative_to(root)).replace("\\", "/")
        try:
            import hashlib
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        return {
            "candidate_preserved": True,
            "candidate_workspace_path": rel,
            "candidate_sha256": digest,
            "candidate_size": candidate.stat().st_size,
            "candidate_preservation_fact": (
                f"Fact: this blocked workspace.write did not create project file {target_rel!r}; "
                f"the already-supplied candidate content was preserved at `{rel}` for helper review or apply evidence. "
                "Avoid pasting the same long body into another tool call."
            ),
            "summary_zh": "被拦截的写入内容已保存为工作区候选文件；真实项目未改变，可交给 helper 修订或产出。",
        }
    except OSError:
        return {}


def _main_env_existing_project_copy_edit_error(path: str, operation: str) -> dict | None:
    """Keep real-project staged edits helper-owned in environment mode."""
    classification = classify_path(path)
    norm = classification.normalized
    if classification.zone != PathZone.STAGED_FILE:
        return None
    try:
        from app.core.core_processes import current_helper_kind
        from app.core.runtime_mode import is_environment_mode

        if current_helper_kind() or not is_environment_mode():
            return None
    except Exception:
        return None
    project_rel = norm[len("_env/"):]
    if not project_rel:
        return None
    return {
        "ok": False,
        "error": (
            f"Fact: {operation} would modify staged project copy {norm!r} in the main workflow. "
            "The real project is unchanged until env_apply_*. For source/project edits, delegate the staged output "
            "to a helper with expected_outputs and acceptance checks, then inspect/diff/apply in the main thread.\n\n"
            "事实：主流程本次不会直接编辑 _env 项目副本；项目源码/产物修改应由 helper 产出并自验，主流程负责 diff/apply、交付映射和验收记账。"
        ),
        "blocked_reason": "main_thread_env_project_edit_should_delegate",
        "blocked_path": norm,
        "delegate_required": True,
        "recovery_facts": {
            "matching_helper_kind": "code",
            "input_files": [norm],
            "expected_outputs": [norm],
            "acceptance_checks": [
                "staged output contains the intended project edit",
                "main thread inspects env_diff before env_apply_*",
            ],
            "project_truth_fact": "the real project is unchanged until env_apply_* succeeds",
        },
    }


def _main_environment_workspace_write_error(ws_dir: str, path: str, content: str) -> dict | None:
    try:
        from app.core.core_processes import current_helper_kind
        from app.core.runtime_mode import is_environment_mode

        if current_helper_kind() or not is_environment_mode():
            return None
    except Exception:
        return None
    classification = classify_path(path)
    norm = classification.normalized
    if not norm:
        return None
    internal_prefixes = (
        ".temp/",
        ".prev/",
        "_helpers_shared/",
        "_shared/",
        "_downloaded_media/",
    )
    if (
        classification.zone in {PathZone.STAGED_FILE, PathZone.STAGED_ROOT, PathZone.HELPER_SHARED}
        or norm.startswith(internal_prefixes)
    ):
        return None
    try:
        if os.path.isdir(_safe_resolve(ws_dir, norm)):
            return None
    except Exception:
        pass
    if classification.zone not in {PathZone.WORKSPACE, PathZone.DELIVERABLE}:
        return None
    content_text = content if isinstance(content, str) else str(content)
    content_chars = len(content_text)
    content_lines = content_text.count("\n") + (1 if content_text else 0)
    project_text_should_delegate = _looks_like_main_project_text_artifact(norm, content_text)
    candidate = (
        _preserve_blocked_workspace_project_candidate(ws_dir, target_rel=norm, content=content_text)
        if project_text_should_delegate
        else {}
    )
    if project_text_should_delegate:
        input_files = [candidate["candidate_workspace_path"]] if candidate.get("candidate_workspace_path") else []
        result = {
            "ok": False,
            "error": (
                f"workspace.write would create {norm!r} only in the chat workspace, not in the real environment project. "
                "The attempted content looks like a substantial project-facing text artifact. Keep project authoring "
                "helper-owned when durable project state is still required. The response preserves candidate content "
                "when possible and reports the normal helper-owned output shape as facts.\n\n"
                "事实：workspace.write 未创建真实项目文件；候选内容如有保存可作为证据，是否派发或应用由模型决定。"
            ),
            "blocked_reason": "environment_workspace_write_not_project_file",
            "blocked_path": norm,
            "content_chars": content_chars,
            "content_lines": content_lines,
            "content_omitted_from_suggestion": True,
            "chat_workspace_only": True,
            "project_file_created": False,
            "delegate_required": True,
            "next_action_instruction": (
                f"Fact: this workspace.write call did not create project file {norm!r}. "
                "The supplied content was preserved as a candidate file when possible. If it is still the desired "
                "project deliverable, one recoverable shape is a helper with that candidate in input_files and target "
                "`_env/...` in expected_outputs; avoid calling env_apply_create(content=...) with the same long body.\n\n"
                "事实：本次 workspace.write 未创建项目文件；候选文件路径可作为 helper 输入证据，避免重复粘贴正文。"
            ),
            "recovery_facts": {
                "matching_helper_kind": "edit",
                "mode": "easy",
                "framework_fact": "<purpose, source evidence, target path, and acceptance checks>",
                "input_files": input_files,
                "helper_prompt_fact": (
                    f"Create the project-visible text artifact {norm}. "
                    "If a candidate input file is listed, inspect and revise it instead of asking the main process "
                    "to paste the body again."
                ),
                "expected_outputs": [f"_env/{norm}"],
                "acceptance_checks": ["inspect the produced file", "verify requested coverage and format"],
            },
        }
        result.update(candidate)
        return result
    content_for_suggestion = content_text
    omitted_large_content = content_chars > 4000
    if omitted_large_content:
        content_for_suggestion = (
            f"[content omitted after blocked environment workspace.write; "
            f"original_chars={content_chars}, original_lines={content_lines}]"
        )
    return {
        "ok": False,
        "error": (
            f"workspace.write would create {norm!r} in the chat workspace, not in the real environment project. "
            "For a project file or task deliverable in environment mode, create the real project file with env_apply_create "
            "after confirming the target path. Use workspace.write only for internal chat-workspace scratch files.\n\n"
            "workspace.write 写入聊天工作区，不写真实项目；环境项目交付文件请用 env_apply_create。"
        ),
        "blocked_reason": "environment_workspace_write_not_project_file",
        "blocked_path": norm,
        "content_chars": content_chars,
        "content_lines": content_lines,
        "content_omitted_from_suggestion": omitted_large_content,
        "chat_workspace_only": True,
        "project_file_created": False,
        "next_action_instruction": (
            f"Fact: this workspace.write call did not create project file {norm!r}. "
            "If the attempted content is the desired project deliverable, `env_apply_create` with the reported path/content "
            "shape is one equivalent real-project create operation. A separate write-access probe creates a different "
            "file and is not evidence that blocked_path was created.\n\n"
            "事实：本次 workspace.write 未创建项目文件；若该内容就是目标交付物，可用 env_apply_create 按返回的路径/内容形状创建真实项目文件。"
        ),
        "recovery_facts": {
            "matching_tool_shape": "env_apply_create",
            "arguments": {
                "path": norm,
                "content": content_for_suggestion,
            },
        },
    }


def _rewrite_strategy_hint(path: str) -> str:
    classification = classify_path(path)
    if classification.zone == PathZone.STAGED_FILE:
        return (
            f"rewrite the coherent section by continuing on `{path}` with edit_file, multi_edit, or insert_in_file, "
            "then use env_diff and env_apply_replace from the main process. Use staged edit tools for an existing "
            "_env project copy"
        )
    return f"rewrite the relevant file or function from a complete understanding with workspace.write({path!r}, ...)"


def _path_access_error(path: str, *, operation: str) -> dict | None:
    """Reject zone/path-shape mistakes before tool-specific file handling."""
    try:
        classification = classify_path(path)
    except ValueError as exc:
        return {
            "ok": False,
            "error": "invalid_path",
            "path": path,
            "operation": operation,
            "message": f"{exc}\n路径无效。",
        }
    if not classification.normalized:
        return {
            "ok": False,
            "error": "path_required",
            "path": path,
            "operation": operation,
            "message": "A concrete workspace-relative file path is required.\n需要提供明确的工作区文件路径。",
        }
    if classification.zone == PathZone.STAGED_ROOT:
        return {
            "ok": False,
            "error": "path_is_directory_or_missing_staged_root",
            "path": path,
            "path_zone": str(classification.zone),
            "operation": operation,
            "message": (
                "`_env/` is the staged project-root directory, not a file path. Choose a concrete `_env/...` file, "
                "or use environment directory/search tools to locate project files.\n"
                "`_env/` 是项目暂存目录，不是文件；先定位具体文件再读写。"
            ),
            "suggested_tools": ["env_list_tree", "env_search", "env_read", "workspace locate"],
        }
    if classification.is_directory_hint:
        return {
            "ok": False,
            "error": "path_is_directory",
            "path": path,
            "path_zone": str(classification.zone),
            "operation": operation,
            "message": (
                "This operation needs one concrete file path. The supplied path is shaped like a directory; "
                "list or search first, then retry with an exact file.\n"
                "该操作需要具体文件路径；目录路径请先列目录或搜索。"
            ),
            "suggested_tools": ["workspace locate", "search_files", "search_across_files", "env_list_tree", "env_search"],
            "_next_action_instruction": (
                "Identify the exact file in the correct zone before retrying this operation.\n"
                "先定位正确区域中的具体文件，再重试。"
            ),
        }
    return None


def _ready_helper_output_read_fact(
    ws_dir: str,
    path: str,
    *,
    caller_kind: str,
    force: bool,
    unbounded_full_read: bool,
    computed_full_read: bool = False,
) -> dict | None:
    """Return a compact provenance fact for clean helper-owned artifacts.

    This protects the main coordinator context from rereading content that a
    successful helper already owns. It is a provenance boundary, not a task
    classifier: explicit force, fragment reads, and helper callers still pass.
    """
    if force:
        return None
    if str(caller_kind or "").strip().lower() != "main":
        return None
    if not (unbounded_full_read or computed_full_read):
        return None
    norm = str(path or "").replace("\\", "/").strip().lstrip("./")
    if not norm:
        return None
    try:
        registry = FileRegistry.load(
            scope_id=f"workspace:{os.path.abspath(ws_dir)}",
            workspace_root=ws_dir,
        )
        record = registry.find_by_workspace_path(norm)
    except Exception:
        return None
    if record is None or record.kind != FileKind.HELPER_OUTPUT:
        return None
    ready_statuses = {
        FileStatus.READY,
        FileStatus.VERIFIED,
        FileStatus.PROMOTED,
        FileStatus.APPLIED,
        FileStatus.DELIVERED,
    }
    if record.status not in ready_statuses and not record.verified:
        return None
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    outputs_complete = metadata.get("outputs_complete")
    producer_self_verified = metadata.get("producer_self_verified") is True
    if outputs_complete is False or not producer_self_verified:
        return {
            "ok": True,
            "action": "read_file",
            "path": norm,
            "content": "",
            "content_omitted": True,
            "content_omitted_reason": "helper_owned_unverified_artifact",
            "helper_owned_artifact_fact": (
                f"`{norm}` is a helper-owned artifact, but its producer completion facts are not clean "
                "producer-self-verified evidence. The main process should not absorb the artifact body to "
                "repair or judge helper-owned content quality. Treat this as a recovery boundary: resume the "
                "producer helper, use a verify helper, or consume existing helper-run evidence. Use force=true "
                "only for an explicit quote/display request or a narrow main-owned file-management fact that "
                "requires exact local text."
            ),
            "事实": (
                f"`{norm}` 是 helper 产物，但当前没有干净的生产者自验证完成事实；主进程不通过阅读全文来接管质量判断。"
                "应恢复生产 helper、派 verify helper 或使用已有 helper 验证证据；仅显式展示/引用或窄文件管理事实才 force 读取。"
            ),
            "artifact_status": str(record.status),
            "artifact_verified": bool(record.verified),
            "owner_task_id": record.owner_task_id,
            "helper_kind": record.helper_kind,
            "visibility": str(record.visibility),
            "size_bytes": record.size,
            "sha256": record.sha256,
            "metadata": {
                key: metadata.get(key)
                for key in (
                    "source",
                    "terminal_reason",
                    "outputs_complete",
                    "producer_self_verified",
                    "quality_warning_count",
                )
                if key in metadata
            },
            "_next_action_instruction": (
                "Do not read this helper-owned artifact body in the main thread to perform content QA. "
                "Use the helper completion facts as recovery evidence, then resume the producer helper or "
                "delegate a verify helper if the active task still needs content validation. force=true is "
                "reserved for explicit quote/display or narrow main-owned file-management needs.\n\n"
                "主进程不接管 helper 产物正文质检；需要验证时恢复生产 helper 或派 verify helper。"
            ),
        }
    return {
        "ok": True,
        "action": "read_file",
        "path": norm,
        "content": "",
        "content_omitted": True,
        "content_omitted_reason": "helper_owned_verified_artifact",
        "helper_owned_artifact_fact": (
            f"`{norm}` is a ready helper-owned artifact. The main process should trust the successful "
            "helper's content judgment and should not re-read this artifact merely to verify content. "
            "Use helper reports, output maps, and verifier/apply facts as the acceptance evidence. "
            "If the user explicitly asks to display or quote exact text, retry with force=true or read the "
            "smallest relevant fragment. If a separate warning, contradiction, or verifier gap appears, route "
            "that boundary to the producer helper or a verify helper instead of main-thread content QA."
        ),
        "事实": (
            f"`{norm}` 是已就绪的 helper 产物；主进程信任成功 helper 的内容判断，不为复核内容而重读。"
            "如用户明确要求展示/引用原文，再 force=true 或读取最小片段；警告/矛盾交回生产 helper 或 verify helper。"
        ),
        "artifact_status": str(record.status),
        "artifact_verified": bool(record.verified),
        "owner_task_id": record.owner_task_id,
        "helper_kind": record.helper_kind,
        "visibility": str(record.visibility),
        "size_bytes": record.size,
        "sha256": record.sha256,
        "metadata": {
            key: metadata.get(key)
            for key in ("source", "terminal_reason", "outputs_complete", "producer_self_verified")
            if key in metadata
        },
        "_next_action_instruction": (
            "Continue from the helper completion facts. Do not inspect this helper-owned artifact for content "
            "verification. Use force=true only for an explicit display/quote request or a narrow main-owned "
            "file-management/runtime fact that requires exact text. Route warnings, contradictions, verifier "
            "gaps, build/test gaps, or quality concerns to the producer helper or a verify helper. A clean transfer/apply "
            "of helper-owned content is not itself a reason to re-read the artifact.\n\n"
            "继续使用 helper 完成事实；转移/应用 helper 产物本身不要求复读正文，警告/矛盾/外部验收缺口交回生产 helper 或 verify helper。"
        ),
    }


def _main_thread_staged_source_read_fact(
    ws_dir: str,
    path: str,
    *,
    caller_kind: str,
    force: bool,
    unbounded_full_read: bool,
    computed_full_read: bool = False,
) -> dict | None:
    """Compact main-thread full reads of staged project source/test copies.

    Helpers still receive exact text. The coordinator receives path/hash/line
    facts unless it explicitly asks for force or a narrow line range.
    """
    if force:
        return None
    if str(caller_kind or "").strip().lower() != "main":
        return None
    if not (unbounded_full_read or computed_full_read):
        return None
    norm = str(path or "").replace("\\", "/").strip().lstrip("./")
    if not norm.lower().startswith("_env/"):
        return None
    project_rel = norm[5:].lstrip("/")
    if not project_rel:
        return None
    try:
        from app.llm.tools.environment import _env_read_looks_source_or_test
        if not _env_read_looks_source_or_test(project_rel):
            return None
    except Exception:
        low = project_rel.lower()
        if not (
            low.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp"))
            or "/test" in low
            or low.startswith("test")
            or "/tests/" in low
            or low.startswith("tests/")
        ):
            return None
    try:
        target = _safe_resolve(ws_dir, norm)
        if not os.path.isfile(target):
            return None
        with open(target, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    sha256 = hashlib.sha256(raw).hexdigest()
    return {
        "ok": True,
        "action": "read_file",
        "path": norm,
        "project_path": project_rel,
        "content": "",
        "content_compacted": True,
        "content_omitted": True,
        "content_omitted_reason": "main_thread_staged_source_body_compacted",
        "total_lines": len(lines),
        "size_bytes": len(raw),
        "sha256": sha256,
        "staged_source_handoff_fact": (
            f"Fact: `{norm}` is a staged project source/test copy for `{project_rel}`. The main-thread read_file "
            "returned compact path/hash/line facts instead of source body content. Helpers can read the exact staged "
            "copy and own diagnosis, edits, and producer validation. If the main process intentionally needs exact "
            "local text, read only the smallest relevant line range or use force=true for an explicit quote/display need."
        ),
        "事实": (
            f"`{norm}` 是项目源码/测试的暂存副本；主进程默认不吸收正文。"
            "源码诊断、编辑和自验证交给 helper；主进程确需精确文本时读取最小行范围或显式 force。"
        ),
        "_next_action_instruction": (
            "Use these path/hash facts for coordination and delegate source/test body work to a helper with input_files. "
            "Do not full-read staged source/test copies in the main thread merely to verify helper content. "
            "A narrow line-range read remains available for a named main-owned fact.\n\n"
            "主进程使用路径/hash 事实调度；源码/测试正文交给 helper。需要主进程精确事实时只读最小行范围。"
        ),
    }


def _recovery_artifact_read_provenance(path: str) -> dict | None:
    """Return provenance facts for reads of recovery-storage files.

    `_tool_results/...` spill files and `_env/.blocked_creates/...` preserved
    candidates exist for recovery, not as ordinary task material. Reading them
    stays allowed, but the result carries a provenance fact so the caller does
    not treat them as fresh source evidence (handoff 2026-06-10: spill files
    and blocked-create candidates entered the t3-msg-inbox-triage trajectory
    as task material).

    spill 与被拦截候选是恢复性存储；读取时附 provenance 事实，不当成新的源材料证据。
    """
    norm = str(path or "").replace("\\", "/").strip().lstrip("./")
    low = norm.lower()
    if not low:
        return None
    if low.startswith("_tool_results/") or "/_tool_results/" in low:
        return {
            "provenance": "tool_result_spill",
            "provenance_fact": (
                f"`{norm}` is recovery storage for an earlier oversized tool result, not independent "
                "source material. The original tool call already produced this content; use it only to "
                "recover details that were truncated or folded out of context, and cite the original "
                "tool/file as the evidence source.\n"
                "该文件是此前超长工具结果的溢出存档，不是新的源材料；仅用于恢复被截断的细节，证据请引用原始来源。"
            ),
        }
    if low.startswith("_env/.blocked_creates/") or low.startswith(".blocked_creates/") or "/.blocked_creates/" in low:
        return {
            "provenance": "blocked_create_candidate",
            "provenance_fact": (
                f"`{norm}` is a preserved candidate from a blocked direct create, authored earlier in this "
                "workflow. It is not independent source material. If current evidence says it is the desired "
                "final content, pass it by path to an edit helper via input_files or apply it with the compact "
                "create shape; do not re-derive its facts from raw sources unless a contradiction exists.\n"
                "该文件是被拦截创建时保留的候选正文，由本工作流早先生成；可按路径交给 edit helper 或直接套用，无矛盾时不必重新派生其事实。"
            ),
        }
    return None


async def _active_helper_owns_path_fact(path: str) -> str | None:
    """Return a soft fact when a live helper's expected_outputs covers `path`.

    Main-thread co-authoring of a helper-owned deliverable produces duplicate,
    potentially conflicting artifacts (20260610_163156). The write is not
    blocked; the model decides what to do with the fact.
    """
    try:
        from app.core import debug as _debug
        from app.core.core_processes import registry as _proc_registry
        from app.llm.tools.delegate_state import _get_task_contract

        trace_id = _debug.current_trace_id() or ""
        if not trace_id:
            return None
        norm = str(path or "").replace("\\", "/").strip().lstrip("./")
        base = norm.rsplit("/", 1)[-1]
        if not base:
            return None
        reg = _proc_registry()
        async with reg._lock:
            live_task_ids = [
                h.helper_task_id for h in reg._procs.values()
                if getattr(h, "proc_type", "") == "helper"
                and getattr(h, "helper_task_id", "")
                # owner is normally a proc_id mapped to a trace; direct
                # trace-id owners (tests, legacy paths) match by equality.
                and (reg.trace_id_of(h.owner) == trace_id or h.owner == trace_id)
            ]
        for task_id in live_task_ids:
            contract = _get_task_contract(trace_id, task_id)
            if not contract:
                continue
            for declared in contract.get("expected_outputs") or []:
                declared_norm = str(declared).replace("\\", "/").strip().lstrip("./")
                if not declared_norm:
                    continue
                if declared_norm == norm or declared_norm.rsplit("/", 1)[-1] == base:
                    return (
                        f"A still-running helper `{task_id}` declared `{declared}` in its expected_outputs. "
                        "Writing the same deliverable from the main thread creates duplicate authorship and "
                        "potentially conflicting artifacts. If the helper result is wanted, wait/collect it; "
                        "if the helper approach is abandoned, cooperatively kill it before continuing here.\n"
                        "同名交付物已由运行中的 helper 拥有；主线程重复撰写会产生冲突产物，先收结果或先终止 helper。"
                    )
        return None
    except Exception:
        return None


async def handle_write(ws_dir: str, path: str, content: str) -> dict:
    _sync_workspace_globals()

    _access_error = _path_access_error(path, operation="write")
    if _access_error is not None:
        return _access_error
    _contract_error = _helper_env_write_contract_error(path)
    if _contract_error is not None:
        return _contract_error
    _env_overwrite_fact = _existing_env_project_copy_write_fact(ws_dir, path)
    _env_workspace_write_error = _main_environment_workspace_write_error(ws_dir, path, content)
    if _env_workspace_write_error is not None:
        return _env_workspace_write_error
    # ── 2026-05-04 修复:_shared/ 写保护(Razor 教训) ──
    if _is_shared_readonly_path(path):
        return {
            "ok": False,
            "error": _SHARED_READONLY_ERROR_MSG,
            "blocked_path": path,
        }
    # ── 主线程源码写入策略(2026-05-11 重写; 撤销 B5,大幅收紧) ──
    #
    # 实测教训(trace 2026-05-11 12:12 → 13:06, 52 分钟兜圈):
    # 主线程在 13:04 决定"我自己写 python 生成 paper.docx", 然后花了 100 秒
    # streaming 生成 14443 字符的 generate_paper.py, 服务端 reject 后这 100 秒
    # **完全浪费**(token 也算计了)。然后还得重新 delegate 才真正开始干活。
    #
    # 用户判定: 服务端 reject 是 too-late,LLM 已生成完整长 content 才被拦,
    # **这种损失不可逆**。所以策略变更:
    #   - 任何编译型源码 (.c/.cpp/.cc/.h/.hpp/.js/.ts/.go/.rs/.java/.cs/.kt) 一律禁(无大小例外)
    #     撤销之前 B5 的"≤3KB 小 header 放行"豁免 — 实操中没人真写这种小 .h
    #   - .py 上限由 app.core.policies 统一控制。普通 chat 和 environment 都允许
    #     中型验证/转换脚本；实质工程源码仍应走 helper。
    #     任何稍微复杂的脚本立刻被拒,而不是允许 LLM 写到 14000 才发现
    #   - 错误消息更直接,指出"你写这段已经浪费时间了,别再试,delegate"
    # 2026-05-15 Item 11: 扩展名集合 + .py 字符上限统一从 app.core.policies 读取,
    #   防止 prompt 文档和实际执行规则漂移。原硬编码值已经迁过去,默认完全一致。
    from app.core.policies import (
        main_thread_banned_extensions as _banned_exts,
        main_thread_py_max_chars as _py_max,
    )
    _PY_VERIFY_MAX = _py_max()  # 字符;中型 Python 验证/转换脚本上限
    _BANNED_EXTS = _banned_exts()
    _path_low = path.lower()
    _path_norm = path.replace("\\", "/").lstrip("./")
    _is_environment_workspace_copy = False
    try:
        from app.core.runtime_mode import is_environment_mode
        _is_environment_workspace_copy = bool(
            is_environment_mode() and (
                _path_norm == "_env" or _path_norm.startswith("_env/")
            )
        )
    except Exception:
        _is_environment_workspace_copy = False
    _is_compiled_lang_ext = any(_path_low.endswith(ext) for ext in _BANNED_EXTS)
    _is_py = _path_low.endswith(".py")
    # 2026-05-11 P9: edit helper 写代码文件硬拦截
    # 病因(实测 trace 18:46-19:09): paper/pptx helper kind="edit" 越权写
    # matplotlib 重画图(gen_charts.py / gen_chart4.py), 引入算法名错配
    # (B+Tree/RedBlack 不存在于 CSV) → docx/pptx 嵌入的图 BPTree/RBTree 全 0。
    # 系统层警告(granularity_warnings)被 LLM 忽略, 必须硬拦截。
    # edit helper 应该: fetch_to_temp 拿主区现成图 → office.insert_image 嵌入。
    if "_delegate_" in ws_dir:  # helper 上下文
        try:
            from app.core.core_processes import current_helper_kind as _cur_kind
            _kind = _cur_kind()
        except Exception:
            _kind = ""
        if _kind == "read" and _path_norm.startswith("_env/"):
            return {
                "ok": False,
                "error": (
                    f"read helper cannot write internal evidence into staged project files ({path!r}). "
                    "Keep source materials under `_env/...` unchanged and write extraction evidence as `.txt` at the "
                    "sandbox root or under `_helpers_shared/<task_id>/`. Then report the evidence path so the main "
                    "process can synthesize from it without polluting the project tree.\n"
                    "read helper 的证据文件不要写入 _env 项目区；请写到沙箱根或 _helpers_shared。"
                ),
                "blocked_reason": "read_helper_env_evidence_forbidden",
                "blocked_path": path,
                "blocked_kind": _kind,
                "suggested_paths": [
                    "_helpers_shared/<task_id>/evidence.txt",
                    "read_evidence.txt",
                ],
            }
        if _kind == "read" and not _path_low.endswith(".txt"):
            return {
                "ok": False,
                "error": (
                    f"read helper can write only internal `.txt` evidence files, not {path!r}. "
                    "Available evidence-gathering shapes include read, office, OCR, inspect, or request_resource. "
                    "User-facing artifacts, code, charts, or Office documents need a matching owner outside this read helper.\n"
                    "read helper 只写内部 txt 证据；交付物、代码、图表和 Office 文档交给对应流程。"
                ),
                "blocked_reason": "read_helper_non_txt_write_forbidden",
                "blocked_path": path,
                "blocked_kind": _kind,
                "matching_helper_kind": "read",
                "observed_recovery_tool": "request_resource",
                "observed_recovery_shape": {
                    "resource_kind": "read",
                    "allowed_output_shape": "internal .txt evidence",
                },
                "suggested_tool": "request_resource",
            }
        if _kind == "edit" and _is_compiled_lang_ext:
            return {
                "ok": False,
                "error": (
                    f"edit helper cannot write compiled/source implementation files ({path}, kind=edit). "
                    "This task has implementation, compilation, or benchmark ownership facts. A code helper "
                    "resource shape can own the required source/data outputs; this edit helper cannot own them here.\n"
                    "edit helper 负责文档；实现/编译/benchmark 需请求 code helper。"
                ),
                "blocked_reason": "edit_helper_writing_compiled_source",
                "blocked_path": path,
                "blocked_kind": _kind,
                "matching_helper_kind": "code",
                "observed_recovery_tool": "request_resource",
                "observed_recovery_shape": {
                    "resource_kind": "code",
                    "reason": "need implementation/benchmark resource",
                    "needed_outputs": ["<source_or_data_file>"],
                },
                "suggested_helper_kind": "code",
                "suggested_tool": "request_resource",
                "suggested_request": (
                    "request_resource(kind='code', reason='need implementation/benchmark resource', "
                    "needed_outputs=['<source_or_data_file>'])\n"
                    "请求 code helper 接管实现、编译或 benchmark 资源。"
                ),
            }
        if _kind == "edit" and _is_py:
            _lower_content = content.lower()
            _looks_like_docx_script = (
                ("from docx import" in _lower_content or "import docx" in _lower_content)
                and ".docx" in _lower_content
                and any(s in _lower_content for s in (
                    "document(", "paragraphs", "tables", "style.name", "heading",
                    "save(", "doc.save", "python-docx",
                ))
            )
            _looks_like_plotting = any(s in _lower_content for s in (
                "matplotlib", "pyplot", "plt.", "savefig", "seaborn",
            ))
            _looks_like_benchmark = any(s in _lower_content for s in (
                "subprocess", "gcc", "g++", "clang", "timeit",
            ))
            _looks_like_verifier_wrapper = (
                "subprocess" in _lower_content
                and any(s in _lower_content for s in (
                    "verify", "verifier", "check", "test", "pytest", "unittest",
                    "node", "npm", "python",
                ))
                and not any(s in _lower_content for s in (
                    "gcc", "g++", "clang", "timeit", "benchmark", "matplotlib",
                    "pyplot", "savefig", "seaborn",
                ))
            )
            if "benchmark" in _lower_content and not _looks_like_docx_script:
                _looks_like_benchmark = True
            # 2026-05-25: 检测计算/财务/统计脚本(不应由 edit helper 写)
            _looks_like_calculation = any(s in _lower_content for s in (
                "npv", "irr", "np.npv", "np.irr", "numpy_financial",
                "amortization", "depreciation", "cash_flow", "discount",
                "scipy.optimize", "scipy.stats", "statsmodels",
                "线性规划", "optimization", "monte carlo",
            )) or any(s in content for s in (
                "净现值", "内部收益率", "投资回收期", "财务", "折旧",
            ))
            if (
                _looks_like_plotting
                or _looks_like_benchmark
                or _looks_like_calculation
                or (len(content) > 8000 and not _looks_like_docx_script)
                or (len(content) > 20000 and _looks_like_docx_script)
            ):
                # 按观察到的能力缺口标记匹配 helper kind。
                if _looks_like_verifier_wrapper:
                    _suggested_kind = "edit"
                    _suggested_req = (
                        "Run the existing verifier/check commands directly with workspace.run from the helper "
                        "sandbox, preserving their working directory, arguments, stdout/stderr, and comparison "
                        "semantics. Do not write a new wrapper script unless the task explicitly assigns one."
                    )
                elif _looks_like_plotting:
                    _suggested_kind = "draw"
                    _suggested_req = "request_resource(kind='draw', reason='need PNG charts from data', needed_outputs=['<chart>.png'])"
                elif _looks_like_calculation:
                    _suggested_kind = "code"
                    _suggested_req = "request_resource(kind='code', reason='need financial/statistical calculation', needed_outputs=['<result>.csv'])"
                elif _looks_like_docx_script:
                    _suggested_kind = "edit"
                    _suggested_req = (
                        "Use office(action='write' or 'append', path='<target>.docx', blocks=[...]) for the same "
                        "document content. Keep code/draw requests only for computation or chart resources."
                    )
                else:
                    _suggested_kind = "code"
                    _suggested_req = "request_resource(kind='code', reason='need benchmark/compile resource', needed_outputs=['<data>.csv'])"
                if _looks_like_verifier_wrapper:
                    _message = (
                        f"edit helper should not write this verifier/check wrapper script ({path}, kind=edit). "
                        "Existing verifier or check commands are acceptance facts; run them directly with "
                        "workspace.run and report the observed stdout/stderr, working directory, and blocker if "
                        "the exact command cannot run.\n"
                        "edit helper 不应为运行现有检查再写聚合脚本；直接用 workspace.run 执行检查并报告事实。"
                    )
                elif _looks_like_docx_script and not (_looks_like_plotting or _looks_like_benchmark or _looks_like_calculation):
                    _message = (
                        f"edit helper should not write this python-docx script ({path}, kind=edit). The same DOCX "
                        "content can be expressed directly as office blocks with office(action='write'/'append'). "
                        "Use scripts only for small verification probes, and request code/draw resources for computation or charts.\n"
                        "edit helper 生成 DOCX 请直接用 office blocks；计算/绘图资源另行请求。"
                    )
                else:
                    _message = (
                        f"edit helper cannot own this Python script ({path}, kind=edit). The content looks like "
                        "chart generation, benchmark/compile driving, financial/statistical computation, or an "
                        "oversized script. Request a draw helper for chart files or a code helper for computation "
                        "and experiments.\n"
                        "edit helper 只写文档相关小脚本；绘图请求 draw，计算/实验请求 code。"
                    )
                return {
                    "ok": False,
                    "error": _message,
                    "blocked_reason": "edit_helper_writing_out_of_scope_python",
                    "blocked_path": path,
                    "blocked_kind": _kind,
                    "matching_helper_kind": _suggested_kind,
                    "observed_recovery_tool": "workspace.run" if _looks_like_verifier_wrapper else "request_resource",
                    "observed_recovery_shape": {
                        "resource_kind": _suggested_kind,
                        "shape": _suggested_req,
                    },
                    "suggested_helper_kind": _suggested_kind,
                    "suggested_tool": "workspace.run" if _looks_like_verifier_wrapper else "request_resource",
                    "suggested_request": _suggested_req,
                }
        # 2026-05-11 P10: draw helper 限制 — 只允许写画图相关
        # 防止 draw helper 越权做 benchmark / 算法实现等
        if _kind == "draw":
            # draw 允许: .py(画图脚本) + .png/.jpg/.svg(产物)
            # 拒绝: 编译型源码(.c/.cpp/.h 等), 大型 Python(应该都是 < 5KB 画图脚本)
            if _is_compiled_lang_ext:
                return {
                    "ok": False,
                    "error": (
                        f"draw helper cannot write compiled/source implementation files ({path}, kind=draw). "
                        "Algorithm implementation or benchmark work has code-helper ownership facts; draw helper "
                        "ownership is image/chart production from data or visual specifications.\n"
                        "draw helper 负责图像产物；算法和 benchmark 请求 code helper。"
                    ),
                    "blocked_reason": "draw_helper_writing_compiled_source",
                    "blocked_path": path,
                    "matching_helper_kind": "code",
                    "observed_recovery_tool": "request_resource",
                    "observed_recovery_shape": {
                        "resource_kind": "code",
                        "reason": "need algorithm/benchmark resource",
                        "needed_outputs": ["<data>.csv"],
                    },
                    "suggested_helper_kind": "code",
                    "suggested_tool": "request_resource",
                    "suggested_request": (
                        "request_resource(kind='code', reason='need algorithm/benchmark resource', "
                        "needed_outputs=['<data>.csv'])\n"
                        "请求 code helper 接管算法或 benchmark 资源。"
                    ),
                }
            if _is_py and len(content) > 30_000:
                # draw helper 画图脚本不该超过 30KB(~750 行), 超过说明可能在写 benchmark
                return {
                    "ok": False,
                    "error": (
                        f"draw helper Python script is unusually large ({len(content)} chars; cap 30000). "
                        "Benchmark or algorithm work has code-helper ownership facts. Genuine chart work can be "
                        "represented as smaller focused plotting scripts with verifiable image outputs.\n"
                        "draw 脚本过大时拆小；若是算法/benchmark 则请求 code helper。"
                    ),
                    "blocked_reason": "draw_helper_oversized_py",
                    "blocked_path": path,
                    "observed_recovery_options": [
                        {
                            "shape": "split_draw_script",
                            "fact": "If this is genuine chart work, smaller focused plotting scripts with verifiable image outputs fit draw-helper ownership.",
                        },
                        {
                            "shape": "request_code_resource",
                            "resource_kind": "code",
                            "fact": "If the oversized script contains algorithm, benchmark, or data-computation work, code-helper ownership fits that portion.",
                        },
                    ],
                }
    if "_delegate_" not in ws_dir and not _is_environment_workspace_copy:  # 主线程
        if _is_compiled_lang_ext:
            return {
                "ok": False,
                "error": (
                    f"Main workflow cannot write compiled/package source files directly ({path}, {len(content)} chars). "
                    "Keep the coordinator focused on planning, evidence, and verification. Delegate substantive "
                    "implementation to the matching helper kind, then inspect the resulting artifacts before delivery: "
                    "code for implementation/build/benchmark, draw for chart/image generation, edit for Office/document "
                    "assembly, verify for independent checks, and hard mode only as a stronger mode for the same base kind.\n"
                    "主流程保留调度和验收；实质代码写作交给匹配 helper。"
                ),
                "delegate_required": True,
            }
        if _is_py and len(content) > _PY_VERIFY_MAX:
            return {
                "ok": False,
                "error": (
                    f"Main workflow Python script exceeds the coordinator limit ({path}: {len(content)} chars; "
                    f"limit {_PY_VERIFY_MAX}). Use main Python only for compact verification, conversion, extraction, "
                    "or one-off inspection. Delegate substantive Python modules, long generators, or multi-step "
                    "debugging to a code helper with concrete acceptance checks.\n"
                    "主流程只写短验证/转换脚本；实质 Python 工程交给 code helper。"
                ),
                "delegate_required": True,
            }
        # .py ≤ policy 上限 → 落到下面统一的"超大文件" 500KB 检查后写入
    if len(content) > 500_000:
        return {
            "ok": False,
            "error": "workspace_write_content_too_large",
            "error_kind": "workspace_write_content_too_large",
            "blocked_path": path,
            "content_chars": len(content),
            "max_chars": 500_000,
            "fact": (
                "The requested workspace.write content exceeds the hard write limit. No file was written and the "
                "content was not truncated to disk. Use a helper-owned artifact, split the content into smaller "
                "logical files, or write a short section and verify it."
            ),
            "事实": "写入内容超过硬上限；未写入文件，也不会截断落盘。请改用 helper 产物、拆成逻辑小文件或短段写入。",
            "delegate_required": True,
        }
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if os.path.isdir(target):
        dir_path = str(path).replace("\\", "/").rstrip("/") + "/"
        return _path_access_error(dir_path, operation="write") or {
            "ok": False,
            "error": "path_is_directory",
            "path": path,
        }
    os.makedirs(os.path.dirname(target), exist_ok=True)

    # 文件名冲突保护：如果文件已存在，先将旧文件重命名（加时间戳后缀），
    # 避免覆盖旧文件导致 KB 引用失效。
    collision_renamed = None
    if os.path.exists(target):
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_rel = path.replace("\\", "/").lstrip("./")
        renamed = f".write_backups/{safe_rel}.{ts}.bak"
        renamed_target = _safe_resolve(ws_dir, renamed)
        os.makedirs(os.path.dirname(renamed_target), exist_ok=True)
        os.rename(target, renamed_target)
        collision_renamed = renamed
        log.info("collision rename: %s → %s", path, renamed)

    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        _content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    except Exception:
        _content_sha256 = ""
    _all_lines = content.splitlines()
    _line_count = len(_all_lines)
    _head_lines = _all_lines[:8]
    _head_excerpt = "\n".join(_head_lines)
    if len(_head_excerpt) > 800:
        _head_excerpt = _head_excerpt[:800].rstrip() + "...[trim]"
    result = {
        "ok": True,
        "action": "write",
        "path": path,
        "size": len(content),
        "content_chars": len(content),
        "line_count": _line_count,
        "sha256": _content_sha256,
        "write_fact": (
            f"workspace.write created `{path}` with {len(content)} chars and {_line_count} line(s). "
            "Use these write facts plus targeted checks for acceptance; a full reread is only useful for a named "
            "main-owned gap, folded/truncated prior evidence, or an explicit display/quote need."
        ),
        "写入事实": (
            f"workspace.write 已创建 `{path}`，字符数 {len(content)}，行数 {_line_count}；"
            "除非存在明确缺口、折叠丢失或需要独立验收，否则不必仅为确认写入而全文重读。"
        ),
    }
    if _head_excerpt:
        result["head_excerpt"] = _head_excerpt
    if collision_renamed:
        result["renamed_previous"] = collision_renamed
    if _env_overwrite_fact is not None:
        result.update(_env_overwrite_fact)
    # 2026-06-10 Round 8: co-authoring soft fact. In 20260610_163156 the main
    # thread authored triage_report.md while a live helper owned the same
    # deliverable via expected_outputs — duplicate authorship, conflicting
    # artifacts, trajectory noise. The write still succeeds; this is a fact.
    try:
        from app.core.core_processes import current_helper_kind as _chk
        if not _chk():  # main thread only (helpers report their kind)
            _co_fact = await _active_helper_owns_path_fact(path)
            if _co_fact:
                result["helper_ownership_fact"] = _co_fact
    except Exception:
        pass

    # 2026-05-15 P69: write 重写 = "整文件重置" 意图明确, 清零此 path 的 edit_history。
    # 这样 P69 edit thrashing 阻断不会卡住"用户已经按推荐 workspace.write 重写过"的情况。
    try:
        _eh_path = os.path.join(ws_dir, ".edit_history.json")
        if os.path.isfile(_eh_path):
            with open(_eh_path, "r", encoding="utf-8") as f:
                _eh_data = json.load(f) or {}
            if isinstance(_eh_data, dict) and path in _eh_data:
                _eh_data.pop(path, None)
                with open(_eh_path, "w", encoding="utf-8") as f:
                    json.dump(_eh_data, f, ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError):
        pass

    # 2026-05-10 Patch 67:同源文件反复重写检测
    # 病因(trace f973df3770544567):helper woat_impl 130 iter 反复 write 同一
    # mini_test.c 文件,每次 write 触发 collision rename → 30min 内产生 34 个
    # mini_test_<时间戳>.c 历史版本。这是 helper 在打转的明确信号 — P42 的
    # "识别根本性错误"段已教 helper 何时停下来质疑实现,但 helper 没遵守。
    # 修法:在 write 工具返回里加强警告字段,让 helper 模型在下一轮 LLM 调用看到
    # "你已 N 次重写同一文件",触发 P42 自检。
    # 不强制中断 — 只是给信号(跟"系统不替模型决策"哲学一致)。
    try:
        _rewrite_path = os.path.join(ws_dir, ".rewrite_count.json")
        _rewrite_data: dict = {}
        if os.path.isfile(_rewrite_path):
            try:
                with open(_rewrite_path, "r", encoding="utf-8") as f:
                    _rewrite_data = json.load(f) or {}
                if not isinstance(_rewrite_data, dict):
                    _rewrite_data = {}
            except (OSError, json.JSONDecodeError):
                _rewrite_data = {}
        # 仅在发生 collision rename 时计数(说明文件已存在被覆盖)
        # 不带时间戳的"基础名"作为计数键(去掉 _YYYYMMDD_HHMMSS 后缀)
        import re as _re_p67
        _stem, _ext = os.path.splitext(path)
        # 去掉末尾的 _时间戳 后缀(如果有)以聚合同源
        _base_stem = _re_p67.sub(r'_\d{8}_\d{6}$', '', _stem)
        _base_key = f"{_base_stem}{_ext}"
        if collision_renamed:
            _entry = _rewrite_data.get(_base_key) or {"count": 0}
            _entry["count"] = int(_entry.get("count", 0)) + 1
            _entry["last_at"] = time.time()
            _rewrite_data[_base_key] = _entry
            # P74: atomic write
            _atomic_write_json(_rewrite_path, _rewrite_data)
            _count = _entry["count"]
            if _count >= 3:
                result["rewrite_warning"] = (
                    f"`{_base_key}` has been rewritten {_count} times, each time creating an automatic backup. "
                    "Repeated full rewrites with unchanged behavior usually mean the current approach needs verification. "
                    "Pause broad rewrites, create a minimal reproduction or acceptance check, then decide whether the algorithm, data structure, or interface should change. "
                    "If the next step is uncertain, report the exact blocker instead of continuing blind rewrites.\n\n"
                    "同一文件多次重写；先做最小验证并重新评估方案。"
                )
    except Exception:
        pass  # 监控失败不阻塞 write

    # 2026-05-03 优化 #5:write(重写)= 重置该文件的 edit 计数
    # 因为 write 就是模型对"我重写吧"的执行,意图明确;后续若再发生 edit 则
    # 重新累计。如果此前已经触发过重写警告,这次 write 把警告状态清零。
    try:
        _edit_history_path = os.path.join(ws_dir, ".edit_history.json")
        if os.path.isfile(_edit_history_path):
            with open(_edit_history_path, "r", encoding="utf-8") as f:
                _h = json.load(f) or {}
            if isinstance(_h, dict) and path in _h:
                del _h[path]
                with open(_edit_history_path, "w", encoding="utf-8") as f:
                    json.dump(_h, f, ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError):
        pass

    return result


def _extract_python_outline(content: str) -> dict:
    _sync_workspace_globals()

    """从 Python 源码抽 outline。失败 fallback 到正则。"""
    try:
        tree = _ast_mod.parse(content)
        funcs = []
        classes = []
        imports = []
        for node in tree.body:
            if isinstance(node, _ast_mod.FunctionDef):
                args = [a.arg for a in node.args.args]
                funcs.append(f"def {node.name}({', '.join(args)}) [line {node.lineno}]")
            elif isinstance(node, _ast_mod.AsyncFunctionDef):
                args = [a.arg for a in node.args.args]
                funcs.append(f"async def {node.name}({', '.join(args)}) [line {node.lineno}]")
            elif isinstance(node, _ast_mod.ClassDef):
                meths = []
                for child in node.body:
                    if isinstance(child, (_ast_mod.FunctionDef, _ast_mod.AsyncFunctionDef)):
                        meths.append(child.name)
                classes.append(
                    f"class {node.name} [line {node.lineno}] methods: {', '.join(meths[:8])}"
                    + ("..." if len(meths) > 8 else "")
                )
            elif isinstance(node, _ast_mod.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, _ast_mod.ImportFrom):
                mod = node.module or "."
                imports.extend(f"{mod}.{a.name}" for a in node.names)
        return {
            "language": "python",
            "functions": funcs[:50],
            "classes": classes[:30],
            "imports": imports[:30],
        }
    except SyntaxError:
        return _extract_generic_outline(content, language_hint="python")


def _build_file_outline(path: str, content: str) -> dict:
    _sync_workspace_globals()

    """根据文件扩展名选择 outline 提取器,返回结构化摘要 dict。"""
    p = path.lower()
    if p.endswith(".py"):
        return _extract_python_outline(content)
    if p.endswith((".c", ".cpp", ".cc", ".h", ".hpp")):
        return _extract_c_outline(content)
    # 其他语言用通用 fallback
    lang = "generic"
    if p.endswith((".js", ".ts", ".jsx", ".tsx")):
        lang = "javascript"
    elif p.endswith((".go",)):
        lang = "go"
    elif p.endswith((".rs",)):
        lang = "rust"
    elif p.endswith((".java",)):
        lang = "java"
    return _extract_generic_outline(content, language_hint=lang)


def _detect_file_type(path: str) -> tuple[str, str, str | None, str | None]:
    _sync_workspace_globals()

    """返回 (category, friendly_name, extract_code_template, save_back_code_template)。
    未知扩展名返回 unknown 类别。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _FILE_TYPE_TABLE:
        return _FILE_TYPE_TABLE[ext]
    return ("unknown", f"unknown extension ({ext or 'no ext'})", None, None)


def _check_file_readable(target: str, *, size_cap: int | None = None) -> tuple[int, dict | None]:
    _sync_workspace_globals()

    if size_cap is None:
        size_cap = _FILE_SIZE_CAP

    """通用文件可读性检查:存在 / 不过大 / 非二进制。
    返回 (size, error_dict_or_None)。
    """
    if os.path.isdir(target):
        return 0, {
            "ok": False,
            "error": "path_is_directory",
            "path": os.path.basename(target) or target,
            "message": (
                "This tool reads one file. The supplied path is a directory; "
                "choose a concrete file, or use a directory-level search/listing tool.\n\n"
                "该工具一次读取一个文件；目录需先列出或搜索后选择具体文件。"
            ),
            "suggested_tools": ["search_across_files", "search_files", "read_file"],
            "_next_action_instruction": (
                "Use search_across_files for content search across a directory, "
                "search_files/listing to choose files, then read/search a concrete file path.\n\n"
                "先用目录级搜索或列表选出具体文件，再读取或检索。"
            ),
        }

    try:
        size = os.path.getsize(target)
    except OSError as e:
        return 0, {"ok": False, "error": f"stat failed: {e}"}

    if size > size_cap:
        return size, {
            "ok": False,
            "error": f"file too large ({size:,} bytes > {size_cap:,} cap). " + _GUIDE_TOO_LARGE.format(path="<path>"),
        }

    category, friendly, _, _ = _detect_file_type(os.path.basename(target))

    # 二进制检测
    try:
        with open(target, "rb") as f:
            head = f.read(_BINARY_DETECT_BYTES)
    except OSError as e:
        return size, {"ok": False, "error": f"read failed: {e}"}

    if b"\x00" in head:
        structured = _structured_read_file_rejection(os.path.basename(target))
        if structured is not None:
            structured.setdefault("file_type", friendly)
            return size, structured
        # 未识别的扩展名,也返回结构化引导,避免落回旧式纯字符串错误
        return size, {
            "ok": False,
            "error": "binary_or_structured_file_not_readable_as_text",
            "path": os.path.basename(target),
            "file_category": category,
            "file_type": friendly,
            "message": (
                "This file contains binary content and cannot be read as plain text. Inspect the file type first, "
                "then use the matching extraction, OCR, Office, media, or conversion workflow.\n"
                "二进制文件先 inspect_file，再用对应格式工具。"
            ),
            "suggested_tools": ["inspect_file"],
        }

    return size, None


async def handle_inspect_file(ws_dir: str, path: str) -> dict:
    _sync_workspace_globals()

    """检查文件类型,返回完整的处理建议。

    用于 LLM 拿到陌生文件时的预检——告诉它:
    - 这是什么文件(类型 + 友好名)
    - 能不能直接 read_file
    - 如果不能,用什么 Python 库 + 完整的转文本/转回代码模板

    设计:此工具不读文件内容,只看扩展名 + 大小 + 是否二进制。
    成本极低,鼓励 LLM 在 read_file 之前主动调用。
    """
    _access_error = _path_access_error(path, operation="read")
    if _access_error is not None:
        return _access_error
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(target):
        return _file_not_found_response(ws_dir, path, action_hint="edit_file")
    if os.path.isdir(target):
        _size, _dir_err = _check_file_readable(target)
        if _dir_err is not None:
            return _dir_err
    if os.path.isdir(target):
        _size, _dir_err = _check_file_readable(target)
        if _dir_err is not None:
            return _dir_err
    if not os.path.isfile(target):
        return {"ok": False, "error": f"not a regular file: {path}"}

    try:
        size = os.path.getsize(target)
    except OSError as e:
        return {"ok": False, "error": f"stat failed: {e}"}

    category, friendly, extract_code, save_back_code = _detect_file_type(path)

    # 也做二进制检测,即使扩展名是 .txt 也可能是二进制
    is_binary = False
    try:
        with open(target, "rb") as f:
            head = f.read(_BINARY_DETECT_BYTES)
        is_binary = b"\x00" in head
    except OSError:
        pass

    # 决定能否直接 read_file
    can_read_directly = (category == "text") or (
        category == "text-structured" and not is_binary
    )

    result = {
        "ok": True,
        "action": "inspect_file",
        "path": path,
        "size_bytes": size,
        "file_category": category,
        "file_type": friendly,
        "is_binary": is_binary,
        "can_read_directly": can_read_directly,
    }

    # 给 actionable 的处理建议
    if can_read_directly:
        result["recommended_workflow"] = (
            f"Direct text file. Use read_file(path='{path}') to read with line numbers, "
            f"then edit_file / insert_in_file to modify.\n\n"
            "文本文件可直接按行读取，再执行编辑。"
        )
    elif category in ("document", "spreadsheet", "image", "archive", "text-structured"):
        if extract_code:
            workflow = [
                f"Step 1 — extract to text:",
                f"  workspace write convert.py:",
                f"  {extract_code.replace('PATH', path)}",
                f"  workspace run 'python convert.py'",
                f"",
                f"Step 2 — process the extracted text with read_file/edit_file/...",
                f"",
            ]
            if save_back_code:
                workflow.extend([
                    f"Step 3 — save back to original format:",
                    f"  workspace write save.py:",
                    f"  {save_back_code.replace('PATH', path)}",
                    f"  workspace run 'python save.py'",
                ])
            else:
                workflow.append(
                    f"Note: this format is hard to write back. "
                    f"Consider outputting to a different format (e.g. PDF → docx)."
                )
            result["recommended_workflow"] = "\n".join(workflow)
        else:
            result["recommended_workflow"] = (
                f"{friendly} — see file_category for hints. "
                f"Generally needs a Python library to extract text first.\n\n"
                "结构化文件通常先用对应库抽取文本。"
            )
    elif category == "media":
        result["recommended_workflow"] = (
            f"{friendly} — LLM cannot process media bytes directly. "
            f"For audio: try openai-whisper for transcription. "
            f"For video: extract frames or audio with cv2/ffmpeg.\n\n"
            "媒体文件需先转录或抽帧/抽音频。"
        )
    elif category == "binary":
        result["recommended_workflow"] = (
            f"{friendly} — binary executable/library. "
            f"Direct processing not recommended. If user needs analysis, "
            f"use specialized tools (pefile/lief).\n\n"
            "二进制可执行或库文件需用专用分析工具。"
        )
    else:  # unknown
        # 二进制但扩展名未知 → 提示尝试通用方法
        if is_binary:
            result["recommended_workflow"] = (
                "Unknown binary format. Try `file PATH` to identify it, "
                "or hexdump first 100 bytes to inspect signature.\n\n"
                "未知二进制先识别格式或查看文件头。"
            )
        else:
            result["recommended_workflow"] = (
                "Unknown extension but appears to be text. "
                "Try read_file(path) — it should work.\n\n"
                "未知扩展但像文本，可尝试 read_file。"
            )

    return result


def _track_edit_count(ws_dir: str, path: str, op: str) -> tuple[int, str | None]:
    _sync_workspace_globals()

    """记录对 path 的 edit 操作,返回 (累计次数, 警告文本或 None)。

    op: "edit_file" / "multi_edit" / "insert_in_file" — 都算 edit 操作。
    持久化到 ws_dir/.edit_history.json,格式: {path: {"count": N, "ops": ["edit_file", ...]}}

    达到 _EDIT_REWRITE_THRESHOLD(=3)次时返回警告文本,推动模型走 workspace.write
    重写而非继续修补(铁律 #2)。

    2026-05-03 优化 #5:之前铁律 #2 只说 edit_file ≥3 次,但 multi_edit 内部
    可能批量 5 个改动也算"一次 edit_file",计数失准。这里统一所有 edit 类工具。

    2026-06-05 软化: 之前 ≥10 次有 "🚫HARD_BLOCK:" 前缀让上层硬拒,用户 06-05
    要求改为软提示 — 仅返回 advisory warning 字符串, 不影响 ok=True/False。
    """
    history_path = os.path.join(ws_dir, ".edit_history.json")
    history: dict = {}
    try:
        if os.path.isfile(history_path):
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        history = {}

    entry = history.get(path)
    if not isinstance(entry, dict):
        entry = {"count": 0, "ops": []}
    entry["count"] = int(entry.get("count", 0)) + 1
    ops = list(entry.get("ops", []))
    ops.append(op)
    entry["ops"] = ops[-10:]  # 只留最近 10 个,防止无限增长
    history[path] = entry

    try:
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

    count = entry["count"]
    # 2026-05-15 P69: 硬阻断阈值 (≥10 次) — 返回特殊前缀让上层硬拒
    if count >= _EDIT_HARD_BLOCK_THRESHOLD:
        ops_str = " → ".join(entry["ops"][-5:])
        warning = (
            f"Hard edit-thrashing signal: {path} has received {count} edit operations "
            f"(recent operations: {ops_str}), which is beyond a normal patch loop. Switch strategy: "
            f"{_rewrite_strategy_hint(path)}, or report "
            f"the verified blocker and next useful route so the main process can reroute. Another small edit is unlikely to converge.\n"
            "同文件反复修补过多时应重写关键部分或报告阻塞；已有 _env 副本仍在原文件上编辑，不用 workspace.write 覆盖。"
        )
        return count, warning
    if count >= _EDIT_REWRITE_THRESHOLD:
        ops_str = " → ".join(entry["ops"][-5:])
        warning = (
            f"Repeated edit signal: {path} has received {count} edit operations (recent operations: {ops_str}). "
            f"If the same area has not converged after several edits, read the surrounding context and consider "
            f"{_rewrite_strategy_hint(path)} instead of continuing local patches.\n"
            "同一区域多次修补未收敛时，先读上下文并考虑整体重写；已有 _env 副本仍在原文件上编辑，不用 workspace.write 覆盖。"
        )
        return count, warning
    return count, None


def _read_text_safely(target: str) -> tuple[str | None, dict | None]:
    _sync_workspace_globals()

    """读文件文本内容,返回 (content, error_dict)。
    err 不是 None 时表示读取失败,content 为 None。
    自动:size 检查 + 二进制检测 + UTF-8 解码 + 行尾规范化。
    """
    size, err = _check_file_readable(target)
    if err is not None:
        return None, err

    # UTF-8 解码,errors=replace 容忍少量损坏字节
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return None, {"ok": False, "error": f"read failed: {e}"}

    # 行尾规范化:统一 \r\n / \r → \n,让 str_replace 不被行尾差异坑
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return content, None


async def handle_read_file(
    ws_dir: str, path: str,
    start_line: int = 1, end_line: int = -1, max_chars: int | None = None,
    *,
    force: bool = False,
    caller_kind: str = "main",
) -> dict:
    _sync_workspace_globals()

    if max_chars is None:
        max_chars = _READ_MAX_CHARS_DEFAULT

    """局部读文件,带行号前缀。

    start_line: 1-indexed,首行
    end_line: 1-indexed,末行(含)。-1 表示读到文件末尾
    max_chars: 输出总字符上限,超过自动截断

    2026-05-03 加(优化 #2):**全读后片段重读检测**。如果该 helper/主线程在
    本会话已经把整个文件全读过(无 view_range,且没被 max_chars 截断),那么
    后续小片段重读会带 `_already_read_full=true` 标记 + 提醒,告诉模型"全文
    其实还在你之前的上下文里,翻上去看就行"。这是实测 trace 09ba132f 中
    bwt_fix helper 的反模式:全读 823 行后,又 5 次小片段重读同一文件,
    浪费 4-5 个 iter / ~1 分钟。
    """
    if not path:
        return {"ok": False, "error": "path is required"}
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        result = {"ok": False, "error": str(e)}
        # 2026-06-10 Round 7: in environment mode the model sometimes converts
        # project-relative paths into `..\..\..\inbox\x.txt` traversal chains
        # (20260610_165331: 11 such failed read_file calls in one run). The
        # blocked path's tail is usually a valid project path — say so.
        if "traversal" in str(e):
            from app.core.runtime_mode import is_environment_mode
            _tail = str(path).replace("\\", "/").strip("/")
            while _tail.startswith("../"):
                _tail = _tail[3:]
            if _tail and not _tail.startswith("..") and is_environment_mode():
                result["suggested_tool"] = "env_read"
                result["suggested_args"] = {"path": _tail}
                result["fact"] = (
                    f"Parent-directory traversal is blocked in workspace reads. The target looks like project file "
                    f"`{_tail}`; in environment mode read it with env_read(path={_tail!r}) using the project-relative "
                    "path, or read the staged copy `_env/" + _tail + "` if it was fetched.\n"
                    "工作区读取禁止越界；项目文件用 env_read 的项目相对路径读取。"
                )
        return result
    if not os.path.exists(target):
        # 2026-05-12 P47: 主进程在 .temp/ 工作但 helper 产物在永久根 (.temp 父目录)
        # 病因(实测 22:44 trace): workspace 双层架构 — main=<arch>/<group>/, temp=.temp/.
        # helper push 到 main, 主线程在 .temp 读不到。Critical 架构 bug 修复:
        # ws_dir 找不到时, 自动 fallback 到永久根 (ws_dir 的父目录, 即 main_workspace)。
        # 这覆盖 P42 之前的极强 fuzzy 兜底, 因为 P42 fuzzy 也只搜 ws_dir 内。
        _parent_ws = _derive_permanent_root(ws_dir)
        if _parent_ws and os.path.isdir(_parent_ws):
            try:
                _main_target = _safe_resolve(_parent_ws, path)
                if os.path.isfile(_main_target):
                    # 找到了! 直接读永久根的文件 + warning
                    debug.log(
                        "p47.main_fallback",
                        f"P47: read_file `{path}` 在 .temp 不存在, fallback 到永久根命中"
                    )
                    _main_result = await handle_read_file(
                        _parent_ws, path,
                        start_line=start_line, end_line=end_line,
                        max_chars=max_chars, force=force, caller_kind=caller_kind,
                    )
                    if _main_result.get("ok"):
                        _main_result["_p47_main_fallback"] = True
                        _main_result["_warning"] = (
                            f"The file was found in the persistent main workspace, not in this session temp workspace. "
                            f"Persistent-relative path: `{path}`. Treat it as an existing artifact from prior helper work.\n\n"
                            "文件位于永久主工作区；按已有产物处理。"
                        )
                        return _main_result
            except (ValueError, OSError):
                pass
        # 2026-05-12 P42: 极强 fuzzy 匹配时, 直接重定向
        # 病因(实测 21:05 trace): 主线程 read_file 用 `_helpers_shared/helper_X/Y`
        # 错路径 31 次, 系统已给 hint 但主线程没听. 修法: score>=95 自动重定向.
        _fnf = _file_not_found_response(ws_dir, path)
        _redirect = _fnf.get("_auto_redirect_path")
        if _redirect and _redirect != path:
            try:
                _redirect_target = _safe_resolve(ws_dir, _redirect)
                if os.path.isfile(_redirect_target):
                    _redirect_result = await handle_read_file(
                        ws_dir, _redirect,
                        start_line=start_line, end_line=end_line,
                        max_chars=max_chars, force=force, caller_kind=caller_kind,
                    )
                    if _redirect_result.get("ok"):
                        _redirect_result["_p42_redirected_from"] = path
                        _redirect_result["_warning"] = (
                            f"The requested path `{path}` did not exist, and the tool redirected to `{_redirect}`. "
                            f"Use `{_redirect}` directly in later calls to avoid repeated path recovery.\n\n"
                            "路径已自动更正；后续直接使用真实路径。"
                        )
                        return _redirect_result
            except (ValueError, OSError):
                pass
        # P47 永久根再加 fuzzy 兜底 (覆盖 P42 只搜 .temp 的盲区)
        if _parent_ws and os.path.isdir(_parent_ws):
            _suggestions_main = _suggest_similar_files(_parent_ws, path)
            if _suggestions_main and _suggestions_main[0]["score"] >= 95:
                _top = _suggestions_main[0]
                try:
                    _main_target = _safe_resolve(_parent_ws, _top["path"])
                    if os.path.isfile(_main_target):
                        debug.log(
                            "p47.main_fuzzy",
                            f"P47: read_file `{path}` 永久根 fuzzy 命中 → `{_top['path']}` (score={_top['score']})"
                        )
                        _main_result = await handle_read_file(
                            _parent_ws, _top["path"],
                            start_line=start_line, end_line=end_line,
                            max_chars=max_chars, force=force, caller_kind=caller_kind,
                        )
                        if _main_result.get("ok"):
                            _main_result["_p47_main_fallback"] = True
                            _main_result["_p47_redirected_from"] = path
                            _main_result["_warning"] = (
                                f"The file was recovered from the persistent main workspace by fuzzy match: "
                                f"`{path}` -> `{_top['path']}` (score={_top['score']}). "
                                "Use the recovered persistent path directly in later calls.\n\n"
                                "文件已在永久主工作区模糊命中；后续直接使用命中路径。"
                            )
                            return _main_result
                except (ValueError, OSError):
                    pass
        return _fnf
    if os.path.isdir(target):
        _size, _dir_err = _check_file_readable(target)
        if _dir_err is not None:
            return _dir_err
    if not os.path.isfile(target):
        return {"ok": False, "error": f"not a regular file: {path}"}

    structured_rejection = _structured_read_file_rejection(path)
    if structured_rejection and not force:
        # 2026-05-15 P113: 二进制 read_file 反复尝试时加强警告
        # 病因(实测压缩 trace): comp_pptx 对 4 个不同 PNG 反复 read_file:
        #   xxx_ratio.png → reject → xxx_by_type.png → reject → xxx_time.png → reject → ...
        # helper 不学, 每次试不同 PNG, 因为错误信息看起来"针对该文件"。
        # 修法: 维护 per-workspace binary refuse 计数, 第 2 次起返回**极强警告** +
        # 直接附 inspect_file 替代命令 (helper 复制粘贴即可)。
        _bin_key = (os.path.realpath(ws_dir), "binary_refuse_count")
        _bin_count = _read_tracker.get(_bin_key, {}).get("count", 0) + 1
        _read_tracker[_bin_key] = {"count": _bin_count, "last_path": path}
        structured_rejection["_binary_refuse_count"] = _bin_count
        if _bin_count >= 2:
            _orig_msg = structured_rejection.get("message", "")
            structured_rejection["message"] = (
                f"read_file has been attempted on binary or structured files {_bin_count} times. "
                "This is a file-type issue, not a single-file issue. "
                f"Use `inspect_file(path='{path}')` to inspect structure, or use the matching Office, image, OCR, or media tool. "
                f"Original message: {_orig_msg}\n\n"
                "二进制或结构化文件不能直接 read_file；先 inspect_file 或用专用工具。"
            )
            # 加错误转码字段, 让 stuck detector 视为同种错误累计
            structured_rejection["repeat_error"] = "binary_read_repeat_use_inspect_file"
        return structured_rejection

    helper_output_fact = _ready_helper_output_read_fact(
        ws_dir,
        path,
        caller_kind=caller_kind,
        force=force,
        unbounded_full_read=(start_line <= 1 and end_line < 0),
    )
    if helper_output_fact is not None:
        return helper_output_fact

    content, err = _read_text_safely(target)
    if err is not None:
        # 替换错误信息里的 path 占位符为实际路径
        if isinstance(err.get("error"), str):
            err["error"] = err["error"].replace("<path>", path)
        return err

    lines = content.split("\n")
    total_lines = len(lines)

    # 规范化范围
    if start_line < 1:
        start_line = 1
    if end_line == -1 or end_line > total_lines:
        end_line = total_lines
    if start_line > total_lines:
        return {
            "ok": True, "action": "read_file", "path": path,
            "total_lines": total_lines,
            "shown_range": [start_line, start_line - 1],
            "content": "",
            "truncated": False,
            "note": f"start_line {start_line} exceeds total_lines {total_lines}",
        }

    # max_chars 硬上限
    max_chars = min(max(max_chars, 100), _READ_MAX_CHARS_HARD_CAP)

    # ── 全读检测(2026-05-03 加,trace b78b242533a24a46 增强)──
    # 双层追踪:
    #   1. 进程内 _read_tracker(主):key=(ws_dir, path),计数 + 全读标记 + 片段历史
    #      → 内存可靠,即使磁盘异常也准确
    #   2. 磁盘 ws_dir/.read_history.json(辅,跨进程持久化):helper restart 也能恢复
    # 判定"全读":start_line=1 且 end_line>=total_lines 且 not truncated。
    is_request_full = (start_line == 1 and (end_line == -1 or end_line >= total_lines))
    is_request_fragment = not is_request_full

    helper_output_fact = _ready_helper_output_read_fact(
        ws_dir,
        path,
        caller_kind=caller_kind,
        force=force,
        unbounded_full_read=False,
        computed_full_read=is_request_full,
    )
    if helper_output_fact is not None:
        helper_output_fact["total_lines"] = total_lines
        helper_output_fact["shown_range"] = [1, 0]
        return helper_output_fact

    # ─ 进程内 tracker 读 ─
    tracker_key = (os.path.realpath(ws_dir), path)
    tracker_entry = _read_tracker.get(tracker_key) or {
        "full_read": False,
        "read_count": 0,
        "fragment_read_count": 0,
        "first_full_iter_hint": None,
    }
    already_read_full = bool(tracker_entry.get("full_read", False))
    repeat_count = int(tracker_entry.get("read_count", 0))
    fragment_read_count = int(tracker_entry.get("fragment_read_count", 0))

    # ─ 磁盘 history 读(辅,跨进程恢复)─
    prev_full_meta: dict | None = None
    history_path = os.path.join(ws_dir, ".read_history.json")
    history: dict = {}
    try:
        if os.path.isfile(history_path):
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f) or {}
        if isinstance(history.get(path), dict):
            prev_full_meta = history[path]
            # 磁盘和内存不一致时,**取或** — 任何一边说全读过就算全读过(更保守)
            if prev_full_meta.get("full_read"):
                already_read_full = True
    except (OSError, json.JSONDecodeError):
        history = {}
        prev_full_meta = None

    # 选定行 + 加行号前缀
    selected = lines[start_line - 1:end_line]
    # 行号宽度:按 end_line 决定对齐
    width = max(3, len(str(end_line)))
    out_lines: list[str] = []
    chars_used = 0
    truncated = False
    actual_end = start_line - 1
    for i, line in enumerate(selected):
        line_no = start_line + i
        formatted = f"{line_no:>{width}}: {line}"
        if chars_used + len(formatted) + 1 > max_chars:
            truncated = True
            break
        out_lines.append(formatted)
        chars_used += len(formatted) + 1
        actual_end = line_no

    prior_fragments = list(tracker_entry.get("fragments", []) or [])
    if isinstance((prev_full_meta or {}).get("fragments"), list):
        prior_fragments.extend((prev_full_meta or {}).get("fragments") or [])
    covered_duplicate = False
    duplicate_coverage_reason = ""
    duplicate_coverage_ranges: list[list[int]] = []
    if is_request_fragment and not force and not truncated:
        covered_duplicate, duplicate_coverage_reason, duplicate_coverage_ranges = _range_is_covered_by_previous_reads(
            start_line,
            actual_end,
            total_lines=total_lines,
            already_read_full=already_read_full,
            fragments=prior_fragments,
        )

    # ── 重读"软退化":返回结构化 outline 而非 ERROR(2026-05-11 A2 重写)──
    # 原版返回 ERROR + "翻上去看就行"。问题:
    #   1. 长 session 早期 read 的内容已经被工具结果挤出窗口或 fold,"翻上去"是假的
    #   2. helper 拿到 ERROR 后凭印象 edit_file → old_str not found → 死循环
    # 新版:仍然不返回 full content(避免上下文重复膨胀),但**返回 outline**:
    #   - total_lines, size
    #   - functions / classes / imports / defines (按语言抽取,确定性零 LLM)
    #   - last_seen_in_iter: 提示上次读的相对时间
    # helper 拿到 outline 仍能"知道文件结构,定位需要改的函数",可调:
    #   read_file(path, start_line=N, end_line=M)  → 仍允许片段读
    #   force=true                                  → 重新全读
    # ── 2026-06-02: 全文重复与片段分页分离 ──
    # 长清单、OCR 文本、Word/PDF 抽取结果需要按 start_line/end_line 分页读取。
    # 片段读取不应消耗全文重读阈值；否则 helper 会在读清单第 4 个片段时被 outline
    # 降级，造成“大文件读不全”。重复全文读取仍然按阈值降级。
    if is_request_full and repeat_count >= _READ_REPEAT_BLOCK_THRESHOLD - 1 and not force:
        debug.log(
            f"workspace.read_file.outline_fallback",
            f"P109: refused {path} (read_count={repeat_count + 1} ≥ {_READ_REPEAT_BLOCK_THRESHOLD}, "
            f"full reads), returning outline",
        )
        try:
            outline = _build_file_outline(path, content)
        except Exception as e:
            outline = {"error": f"outline extraction failed: {type(e).__name__}: {e}"}
        # 仍要更新 tracker
        _read_tracker[tracker_key] = {
            "full_read": already_read_full,
            "read_count": repeat_count + 1,
            "fragment_read_count": fragment_read_count,
            "total_lines": total_lines,
            "first_full_iter_hint": tracker_entry.get("first_full_iter_hint"),
            "fragments": tracker_entry.get("fragments", []),
        }
        return {
            "ok": True,
            "action": "read_file",
            "path": path,
            "total_lines": total_lines,
            "size_bytes": len(content.encode("utf-8", errors="replace")),
            "outline": outline,
            "_outline_mode": True,
            "_read_count": repeat_count + 1,
            "note": (
                f"This file has been read {repeat_count + 1} times (>= {_READ_REPEAT_BLOCK_THRESHOLD}), "
                f"so an outline is returned instead of the full text. Reuse content already in context when possible; "
                f"if folding truly removed needed details, use force=true carefully.\n"
                f"重复读取时返回 outline，必要时才谨慎强读。"
            ),
        }

    # (P109 早期 return outline 已覆盖所有重读 case; 旧 already_read_full 分支不可达)

    result = {
        "ok": True,
        "action": "read_file",
        "path": path,
        "total_lines": total_lines,
        "shown_range": [start_line, actual_end],
        "content": "" if covered_duplicate else "\n".join(out_lines),
        "truncated": truncated,
    }
    _recovery_provenance = _recovery_artifact_read_provenance(path)
    if _recovery_provenance is not None:
        result.update(_recovery_provenance)
    if covered_duplicate:
        result.update({
            "content_omitted": True,
            "content_omitted_reason": "duplicate_read_range_already_returned",
            "coverage_reason": duplicate_coverage_reason,
            "covered_by_ranges": duplicate_coverage_ranges,
            "_fragment_read_count": fragment_read_count + 1,
            "note": (
                f"Requested lines {start_line}-{actual_end} of `{path}` were already returned by earlier read_file "
                "results, so this duplicate content is omitted to preserve model context. This is not evidence that "
                "the file is absent or unchanged; it only states prior coverage. Use force=true only if the earlier "
                "tool result was folded/truncated and these exact lines are still required.\n\n"
                "该行段此前已返回，本次省略重复正文以节省上下文；如确因折叠丢失，可 force=true 恢复读取。"
            ),
            "_next_action_instruction": (
                "Use the previously returned lines as evidence, or request force=true only when the prior result is no "
                "longer recoverable in context and the exact text is necessary.\n\n"
                "优先复用已返回行段；只有上下文确实丢失且必须要原文时才 force=true。"
            ),
        })
    if truncated:
        saved_path = write_tool_output_spill(
            root_dir=ws_dir,
            tool_name="read_file",
            label="content",
            text=content,
        )
        result["content_full_saved_path"] = saved_path
        result["content_original_chars"] = len(content)
        result["content_truncated"] = True
        result["output_truncated"] = True
        result["tool_result_truncated"] = True
        result["visible_excerpt_policy"] = (
            f"Full normalized file content was saved at `{saved_path}` (`content_full_saved_path`); "
            "only the head excerpt is returned in this tool result.\n"
            "完整规范化文件内容已保存；当前工具结果只返回头部摘录。"
        )
        result["next_start_line"] = actual_end + 1
        result["note"] = (
            f"output truncated at {max_chars} chars ({result['shown_range'][0]}-{actual_end} of {total_lines} lines). "
            f"The hard single-call cap is {_READ_MAX_CHARS_HARD_CAP} chars. Continuing with next_start_line would page "
            f"the same large file into the main context; when the missing detail is local, search_in_file can locate specific content "
            f"(e.g., search_in_file(path, 'keyword') for problem numbers, terms, or patterns), "
            f"then read_file with narrow start_line/end_line can target only the relevant lines. "
            f"Full normalized file content was saved at `{saved_path}` for recovery or delegated paging if broad coverage is still needed.\n"
            f"已截断并保存完整规范化正文；可搜索定位、窄范围读取，或交给 helper 分段覆盖。"
        )

    # ── 重读警告 ──
    # 全文读取按 read_count 计数；片段分页只记录片段历史，不触发全文降级。
    will_be_count = repeat_count + (1 if is_request_full else 0)
    will_be_fragment_count = fragment_read_count + (1 if is_request_fragment else 0)
    if is_request_full and will_be_count >= _READ_REPEAT_WARN_THRESHOLD:
        result["_already_read_full"] = already_read_full
        result["_read_count"] = will_be_count
        # 2026-05-08 Fix(Bug 7): 旧实现把 unix 时间戳格式化为 `iter_at_1778215836`
        # 当作"上次读取时点"塞给 LLM,实际上完全无意义(既不是 iter 号也不是
        # 人类可读时间)。改为相对时间短语;同时大幅缩短警告文本(原 ~280 字符
        # × 4 文件批读 = 1KB+ 噪音,helper 实测会反复触发)。
        prev_ts_raw = tracker_entry.get("first_full_iter_hint") or (
            (prev_full_meta or {}).get("iter_hint")
        )
        prev_phrase = ""
        if isinstance(prev_ts_raw, str) and prev_ts_raw.startswith("iter_at_"):
            try:
                _delta = time.time() - float(prev_ts_raw[len("iter_at_"):])
                if _delta < 60:
                    prev_phrase = "刚刚"
                elif _delta < 600:
                    prev_phrase = f"{int(_delta / 60)} 分钟前"
                else:
                    prev_phrase = "之前"
            except (ValueError, TypeError):
                prev_phrase = "之前"
        elif prev_ts_raw:
            prev_phrase = str(prev_ts_raw)[:20]
        prev_total = (
            tracker_entry.get('total_lines')
            or (prev_full_meta or {}).get('total_lines', '?')
        )
        warn_phrase = f"full-file reread #{will_be_count}"
        result["_redundant_read_warning"] = (
            f"{warn_phrase}: `{path}` was already read in full ({prev_total} lines, previous read: {prev_phrase}). "
            "Reuse the content already in context. If only a local detail is missing, read the smallest start_line/end_line fragment. "
            f"From read #{_READ_REPEAT_BLOCK_THRESHOLD}, repeated full reads return an outline unless force=true is justified by folded or emergency-truncated context.\n\n"
            "全文已读过；复用已有上下文，必要时只读局部片段。"
        )
        result["_next_action_instruction"] = (
            "Fact: the full file body was already returned earlier in this run. If a local detail is missing, a small start_line/end_line fragment is available; force=true is for cases where the prior result was folded or emergency-truncated.\n\n"
            "事实：全文已在本轮返回过；只缺局部时可读最小片段，force=true 用于先前结果被折叠或紧急截断。"
        )
        result["_available_next_actions"] = ["reuse_existing_context", "read_minimal_fragment", "force_only_if_context_folded"]
    elif is_request_fragment and will_be_fragment_count >= _READ_REPEAT_WARN_THRESHOLD:
        result["_fragment_read_count"] = will_be_fragment_count
        result["_next_action_instruction"] = (
            "Continuing with start_line/end_line paging is acceptable, but each read should be the smallest fragment needed for the current task. "
            "If enough evidence has already been gathered, synthesize the result before reading more.\n\n"
            "分页读取应保持最小片段；证据足够时先整理结果。"
        )

    # ── 更新 tracker(进程内,可靠)──
    full_now = is_request_full and not truncated
    new_full = full_now or already_read_full
    fragments = list(tracker_entry.get("fragments", []))
    if is_request_fragment:
        fragments.append([start_line, actual_end])
        fragments = fragments[-20:]  # keep enough recent ranges to detect duplicate fragments
    _read_tracker[tracker_key] = {
        "full_read": new_full,
        "read_count": will_be_count,
        "fragment_read_count": will_be_fragment_count,
        "total_lines": total_lines,
        "first_full_iter_hint": (
            tracker_entry.get("first_full_iter_hint")
            or (f"iter_at_{int(time.time())}" if full_now else None)
        ),
        "fragments": fragments,
    }

    # ── 更新磁盘 history(辅,容错)──
    try:
        if path not in history or full_now:
            history[path] = {
                "full_read": new_full,
                "total_lines": total_lines,
                "last_read_range": [start_line, actual_end],
                "last_was_full": full_now,
                "read_count": will_be_count,
                "fragment_read_count": will_be_fragment_count,
                "fragments": fragments,
            }
        else:
            history[path]["last_read_range"] = [start_line, actual_end]
            history[path]["last_was_full"] = False
            history[path]["read_count"] = will_be_count
            history[path]["fragment_read_count"] = will_be_fragment_count
            history[path]["fragments"] = fragments
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError as e:
        # 写不进去虽然不致命(进程内 tracker 兜底),但要记下来便于排查
        debug.log(
            "workspace.read_file.history_write_failed",
            f"failed to write {history_path}: {e!r}",
        )

    return result


async def handle_append(ws_dir: str, path: str, content: str) -> dict:
    _sync_workspace_globals()

    _access_error = _path_access_error(path, operation="write")
    if _access_error is not None:
        return _access_error
    _contract_error = _helper_env_write_contract_error(path)
    if _contract_error is not None:
        return _contract_error
    _env_workspace_write_error = _main_environment_workspace_write_error(ws_dir, path, content)
    if _env_workspace_write_error is not None:
        return _env_workspace_write_error
    if _is_shared_readonly_path(path):
        return {
            "ok": False,
            "error": _SHARED_READONLY_ERROR_MSG,
            "blocked_path": path,
        }
    if len(content) > 500_000:
        return {
            "ok": False,
            "error": "workspace_append_content_too_large",
            "error_kind": "workspace_append_content_too_large",
            "blocked_path": path,
            "content_chars": len(content),
            "max_chars": 500_000,
            "fact": (
                "The requested workspace.append content exceeds the hard write limit. No file was changed and the "
                "content was not truncated to disk. Use a helper-owned artifact or append a smaller logical section."
            ),
            "事实": "追加内容超过硬上限；未修改文件，也不会截断落盘。请改用 helper 产物或追加更小逻辑段。",
            "delegate_required": True,
        }
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if os.path.isdir(target):
        dir_path = str(path).replace("\\", "/").rstrip("/") + "/"
        return _path_access_error(dir_path, operation="write") or {
            "ok": False,
            "error": "path_is_directory",
            "path": path,
        }
    os.makedirs(os.path.dirname(target), exist_ok=True)
    existed = os.path.exists(target)
    with open(target, "a", encoding="utf-8") as f:
        f.write(content)
    return {
        "ok": True,
        "action": "append",
        "path": path,
        "size_appended": len(content),
        "created": not existed,
    }


async def handle_edit_file(
    ws_dir: str, path: str, old_str: str, new_str: str,
    expected_count: int = 1,
) -> dict:
    _sync_workspace_globals()

    """str_replace 模式精准修改。

    严格规则:
    - old_str 必须在文件中正好出现 expected_count 次(默认 1)
    - 不匹配 → 报错并提示如何修
    - old_str 长度 < 5 字符 → 拒绝(防止误改 ; } 等)
    """
    _access_error = _path_access_error(path, operation="edit")
    if _access_error is not None:
        return _access_error
    _contract_error = _helper_env_write_contract_error(path)
    if _contract_error is not None:
        return _contract_error
    _env_edit_fact = _existing_env_project_copy_edit_fact(ws_dir, path, "edit_file")
    if old_str == new_str:
        return {
            "ok": False,
            "error": (
                "old_str equals new_str, so the edit would make no change. Provide the current fragment as old_str "
                "and the intended replacement as new_str.\n"
                "old_str 与 new_str 相同；请提供真实替换内容。"
            ),
        }
    if len(old_str) < _EDIT_OLD_STR_MIN_LEN:
        return {
            "ok": False,
            "error": f"old_str too short (len={len(old_str)} < {_EDIT_OLD_STR_MIN_LEN}); "
                     f"include more context to make it unambiguous.\n"
                     f"old_str 太短；加入更多上下文使其唯一。",
        }
    # ── 2026-05-04 修复:_shared/ 写保护(Razor 教训) ──
    if _is_shared_readonly_path(path):
        return {
            "ok": False,
            "error": _SHARED_READONLY_ERROR_MSG,
            "blocked_path": path,
        }
    # ── 2026-05-15 P69 → 2026-06-05 软化: edit thrashing 仅作软提示, 不再硬拒 ──
    # 用户要求: 不限制最大 edit 次数,除非导致上下文超限。让 LLM 自行判断是否换策略。
    # _track_edit_count (调用在实际 edit 路径里) 仍累计并产出 soft warning。
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(target):
        return _file_not_found_response(ws_dir, path, action_hint="edit_file")

    content, err = _read_text_safely(target)
    if err is not None:
        if isinstance(err.get("error"), str):
            err["error"] = err["error"].replace("<path>", path)
        return err

    # 规范化 old_str 行尾(_read_text_safely 已规范化 content)
    old_str_norm = old_str.replace("\r\n", "\n").replace("\r", "\n")
    new_str_norm = new_str.replace("\r\n", "\n").replace("\r", "\n")

    # ── 2026-05-02 part13:配对校验(trace 74b1295b iter 111 教训)──
    # rdh_v2 把 `{ size_t expected = ...; ... }` 块解构,old_str 里包含开头 `{`
    # 但截断在 `}` 之前,new_str 里 `{` 和 `}` 都没有 → 文件中孤儿 `}` 没人删,
    # 函数提前闭合,line 657 报 `expected identifier or '(' before 'if'`。
    # 模型陷入 read+edit 死循环修这个由 edit 自己制造的语法错。
    #
    # 校验逻辑:edit 不应该让 `{` `}` 净配对发生位移。
    #   net(old_str) := count('{') - count('}')
    #   net(new_str) := 同
    #   如果 net(old) != net(new) → patch 会让文件总体配对失衡 → 拒绝
    # 这只校验 patch 局部的"差量",不是全局,所以不会把模型有意图重构(连删整段
    # block 等)误判 — 只要 old_str 和 new_str 的差量一致,无论怎么改都允许。
    # 同样校验 `(` `)` 和奇偶引号 `"` (字符串字面量).
    def _net_brace(s: str, open_c: str, close_c: str) -> int:
        # 简单计数。不考虑字符串/注释里的字符是为了零误报代价 —
        # 真要严格,需要 lexer。但实测 99% 的 edit 是结构性的,这种简单计数足够。
        return s.count(open_c) - s.count(close_c)

    pairs_to_check = [("{", "}"), ("(", ")"), ("[", "]")]
    imbalance = []
    for o, c in pairs_to_check:
        diff = _net_brace(old_str_norm, o, c) - _net_brace(new_str_norm, o, c)
        if diff != 0:
            sign = "more" if diff < 0 else "fewer"
            imbalance.append(f"`{o}{c}` net diff: new_str has {abs(diff)} {sign} unmatched")
    # 引号校验:总数差不影响配对,但奇偶变化会导致字符串字面量被切开 →
    # 检查 (count(") 是否同奇偶)
    if old_str_norm.count('"') % 2 != new_str_norm.count('"') % 2:
        imbalance.append('`"` count parity changed — string literal may be broken')

    if imbalance:
        return {
            "ok": False,
            "error": (
                f"edit rejected: bracket/quote pairing would shift in file. "
                f"This typically means old_str is truncated mid-block (cut between `{{` "
                f"and `}}`) and new_str doesn't compensate, or vice versa.\n"
                f"Detected imbalance(s): {'; '.join(imbalance)}.\n"
                f"Fix: extend old_str to cover whole matched pair, OR include the same "
                f"closing/opening token in new_str. Re-read the file around the edit "
                f"site to see what closing token follows.\n\n"
                "编辑会破坏括号或引号配对；扩大匹配范围或补齐对应开闭符号后再改。"
            ),
            "imbalance": imbalance,
        }

    actual_count = content.count(old_str_norm)
    if actual_count == 0:
        # 给一个有用的提示:看看是不是缩进/空格的问题
        hint = ""
        old_stripped = old_str_norm.strip()
        if old_stripped and old_stripped in content:
            hint = " (note: stripped version of old_str found — check leading/trailing whitespace)"
        return {
            "ok": False,
            "error": (
                f"old_str not found in {path}{hint}; call read_file(start_line=..., end_line=...) to verify the exact local fragment "
                "instead of rereading the whole file.\n"
                "old_str 未匹配；只读取局部片段并复制当前文本后重试。"
            ),
            "next_action_instruction": (
                "Fact: the supplied old_str is not present in the current file text. A local fragment read can "
                "provide the exact current text near the intended edit site; a whole-file reread is usually only "
                "useful when the local edit site is genuinely unknown.\n"
                "事实：old_str 未出现在当前文件中；局部片段可提供精确文本，只有定位未知时才通常需要全文重读。"
            ),
            "recovery_facts": {
                "old_str_present": False,
                "available_recovery_shapes": [
                    "read a relevant local fragment",
                    "expand old_str with exact surrounding context",
                    "retry edit_file after confirming the current text",
                ],
            },
        }
    if actual_count != expected_count:
        # 2026-06-10 Round 8: include match line numbers so the model can
        # decide expand-context vs replace-all without re-reading the file
        # (20260610_165331: a count-mismatch retry cost one extra turn).
        _match_lines: list[int] = []
        _pos = content.find(old_str_norm)
        while _pos >= 0 and len(_match_lines) < 20:
            _match_lines.append(content[:_pos].count("\n") + 1)
            _pos = content.find(old_str_norm, _pos + 1)
        return {
            "ok": False,
            "error": f"old_str appears {actual_count} times but expected_count={expected_count}; "
                     f"either expand old_str with more context to make it unique, "
                     f"or set expected_count={actual_count} to replace all.\n"
                     f"old_str 匹配次数不符；扩大上下文或显式设置 expected_count。",
            "actual_count": actual_count,
            "match_line_numbers": _match_lines,
            "fact": (
                f"Matches start at lines {_match_lines}. If all occurrences should change, retry with "
                f"expected_count={actual_count}; if only some, expand old_str with surrounding text from the "
                "unwanted lines' context to exclude them."
            ),
        }

    new_content = content.replace(old_str_norm, new_str_norm)

    # 大小校验
    if len(new_content) > _FILE_SIZE_CAP:
        return {
            "ok": False,
            "error": (
                f"resulting file would exceed size cap ({len(new_content)} > {_FILE_SIZE_CAP}). "
                "Split the edit, reduce generated content, or write section-sized artifacts.\n"
                "结果文件超过大小上限；请拆分编辑或分段写入。"
            ),
        }

    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return {"ok": False, "error": f"write failed: {e}"}

    # 报告改动行号 + 改后 ±2 行 context(2026-05-03 优化 #4:edit-time evidence)
    pos = content.find(old_str_norm)
    line_no = content[:pos].count("\n") + 1 if pos >= 0 else None
    after_diff_text: str | None = None
    if pos >= 0:
        # 在 new_content 里找 new_str_norm 的位置(可能与 pos 不同 —— old/new 长度不一)
        new_pos = new_content.find(new_str_norm) if new_str_norm else pos
        if new_pos >= 0:
            new_end = new_pos + len(new_str_norm)
            ctx_start = new_pos
            back = 0
            while ctx_start > 0 and back < 2:
                ctx_start -= 1
                if new_content[ctx_start] == "\n":
                    back += 1
            if new_content[ctx_start] == "\n":
                ctx_start += 1
            ctx_end = new_end
            fwd = 0
            while ctx_end < len(new_content) and fwd < 2:
                if new_content[ctx_end] == "\n":
                    fwd += 1
                    if fwd >= 2:
                        break
                ctx_end += 1
            after_diff_text = new_content[ctx_start:ctx_end]
            if len(after_diff_text) > 400:
                after_diff_text = after_diff_text[:400] + "...[trim]"

    result = {
        "ok": True,
        "action": "edit_file",
        "path": path,
        "replacements": expected_count,
        "first_change_at_line": line_no,
        "new_size": len(new_content),
    }
    if after_diff_text is not None:
        result["after_diff"] = after_diff_text
        result["_next_step_hint"] = (
            "Edit applied. Next, run the narrowest compile/test/verification command with bash/workspace.run. "
            "The changed fragment is already shown in after_diff, so avoid rereading/searching only to confirm the edit.\n"
            "改动已应用；下一步运行最小验证。"
        )

    # 2026-05-03 优化 #5:同文件 edit 计数 + ≥3 次重写提示
    _edit_count, _edit_warning = _track_edit_count(ws_dir, path, "edit_file")
    if _edit_warning:
        result["_edit_count"] = _edit_count
        result["_rewrite_suggestion"] = _edit_warning

    # ── 2026-05-02 part13:edit 后回传"文件顶部 30 行"+ include 缺失警告 ──
    # 教训(trace 74b1295b iter 44):rdh_v2 在 huff_encode 函数体内加 fprintf,
    # 但忘 #include <stdio.h>,因为 edit 只看到 old_str/new_str 范围,看不到顶部
    # includes。下次 compile 才发现 implicit declaration,得回去 read line 1 再 edit。
    # 修复:edit 成功后,把文件前 30 行(几乎肯定覆盖 includes)回传给模型,
    # 模型在下一轮看到 result 时,顺便看到顶部状态,新增依赖时能自查。
    # 仅对源代码文件做(.c/.cpp/.cc/.h/.hpp/.py/.js/.ts/.go/.rs);其他类型不附,避免噪声。
    _src_exts = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh",
                 ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java")
    if path.lower().endswith(_src_exts):
        head_lines = new_content.split("\n", 31)[:30]
        head_text = "\n".join(head_lines)
        if len(head_text) <= 1500:  # 头部 30 行通常 < 1KB,极端情况上限保护
            result["file_head_30"] = head_text

        # ── include / import 缺失主动检测 ──
        # 检查 new_str 里有没有引入新 stdlib 调用,且文件顶部缺对应头文件。
        # 命中即在 result 里给个 hint(不阻断,模型自己决定是否补 include)。
        if path.lower().endswith((".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh")):
            _C_STDLIB_HEADERS = {
                "stdio.h": ("printf", "scanf", "fprintf", "sprintf", "fopen",
                            "fclose", "fread", "fwrite", "fputs", "fgets",
                            "perror", "stderr", "stdout", "stdin", "FILE",
                            "puts", "putc", "getc", "ftell", "fseek"),
                "stdlib.h": ("malloc", "free", "calloc", "realloc", "exit",
                             "atoi", "atof", "atol", "qsort", "bsearch",
                             "abort", "abs", "rand", "srand", "EXIT_SUCCESS",
                             "EXIT_FAILURE"),
                "string.h": ("strlen", "strcpy", "strncpy", "strcmp", "strncmp",
                             "strcat", "strstr", "strchr", "strrchr",
                             "memcpy", "memmove", "memset", "memcmp"),
                "math.h": ("sqrt", "pow", "log", "log2", "log10", "exp",
                           "sin", "cos", "tan", "ceil", "floor", "fabs"),
                "assert.h": ("assert",),
                "ctype.h": ("isdigit", "isalpha", "isspace", "isalnum",
                            "tolower", "toupper"),
                "inttypes.h": ("PRIu64", "PRId64", "PRIx64", "PRIu32",
                               "PRId32", "SCNu64", "SCNd64"),
                "stdint.h": ("uint8_t", "uint16_t", "uint32_t", "uint64_t",
                             "int8_t", "int16_t", "int32_t", "int64_t",
                             "size_t", "uintptr_t"),
                "time.h": ("time", "clock", "difftime", "strftime",
                           "localtime", "gmtime"),
            }
            missing = []
            head_lower = head_text if 'head_text' in dir() else new_content[:1500]
            new_lower = new_str_norm
            for header, syms in _C_STDLIB_HEADERS.items():
                # new_str 里是否引入了这个 header 对应的 symbol
                if not any(re.search(rf"\b{re.escape(s)}\b", new_lower) for s in syms):
                    continue
                # 文件顶部是否已经 include 了?
                if re.search(rf'#\s*include\s*[<"]{re.escape(header)}[>"]', head_lower):
                    continue
                # 进一步:看整文件是否 include(顶部 30 行没有不代表全文没,有些项目 include 在中部)
                if re.search(rf'#\s*include\s*[<"]{re.escape(header)}[>"]', new_content):
                    continue
                # 找出具体哪个 symbol 触发,作为提示
                triggered = next(
                    (s for s in syms if re.search(rf"\b{re.escape(s)}\b", new_lower)),
                    None
                )
                if triggered:
                    missing.append(f"`{triggered}` needs `#include <{header}>`")

            if missing:
                result["include_check"] = (
                    f"new_str introduced symbols whose required header is not yet included in {path}: "
                    + "; ".join(missing[:5]) +
                    ". Add the header or compile immediately to confirm whether a declaration warning appears.\n"
                    "新增符号可能缺头文件；补 include 或立即编译验证。"
                )

    # ── 2026-05-02 part16:.helper_notes.md 自动记账 ──
    # 教训:1M context 下,helper 反复 read 同一区域是因为它"忘了"做过什么。
    # 类比我自己:做长任务时会写 /tmp/events.txt 沉淀状态,后续基于这个聚合产物推理。
    # 这里给 helper 做一个轻量的工作记忆:每次 edit_file 自动记一行到 .helper_notes.md。
    # helper 反复看同一函数想不起改过什么时,read .helper_notes.md 就能拿到完整修改历史。
    #
    # 仅 helper 工作区(_delegate_)启用 — 主线程不需要(它本身有 trace log)。
    if "_delegate_" in ws_dir:
        try:
            notes_path = os.path.join(ws_dir, ".helper_notes.md")
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%H:%M:%S")
            # 摘要式记账:edit @ path:line, "old前 30" → "new前 30"
            old_excerpt = old_str_norm.replace("\n", "↵")[:40]
            new_excerpt = new_str_norm.replace("\n", "↵")[:40]
            note_line = (
                f"- [{ts}] edit_file {path}:L{line_no} "
                f"`{old_excerpt}...` → `{new_excerpt}...`\n"
            )
            # append (创建若不存在)
            with open(notes_path, "a", encoding="utf-8") as f:
                if os.path.getsize(notes_path) == 0:
                    f.write(
                        "# Helper 工作笔记(自动记账)\n"
                        "\n"
                        "此文件由工具自动追加每次 edit_file 的摘要,helper 反复审视代码时\n"
                        "可以 read_file('.helper_notes.md') 看自己做过什么,**避免重复 read 源代码\n"
                        "或重复改同一处**。也可以手动 edit_file 这个文件添加自己的发现/假设。\n"
                        "\n"
                        "## 自动记账(每次 edit_file 后追加)\n\n"
                    )
                f.write(note_line)
            result["note_logged"] = True
        except OSError:
            pass  # 写笔记失败不影响 edit 主流程

    if _env_edit_fact is not None:
        result.update(_env_edit_fact)

    return result


async def handle_multi_edit(
    ws_dir: str, path: str, edits: list,
) -> dict:
    _sync_workspace_globals()

    """同文件多处原子编辑(Claude Code 风格)。

    edits: [{"old_str": str, "new_str": str, "expected_count": int=1}, ...]
    按顺序应用每个 edit。任一失败 → 全部回滚。
    每个 edit 应用后,后续 edit 在新内容上匹配(支持链式改动)。
    """
    _access_error = _path_access_error(path, operation="multi_edit")
    if _access_error is not None:
        return _access_error
    _contract_error = _helper_env_write_contract_error(path)
    if _contract_error is not None:
        return _contract_error
    _env_edit_fact = _existing_env_project_copy_edit_fact(ws_dir, path, "multi_edit")
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "edits must be a non-empty list"}
    if len(edits) > 50:
        return {"ok": False, "error": f"too many edits ({len(edits)} > 50); split into multiple multi_edit calls"}

    # ── 2026-05-04 修复:_shared/ 写保护(Razor 教训) ──
    if _is_shared_readonly_path(path):
        return {
            "ok": False,
            "error": _SHARED_READONLY_ERROR_MSG,
            "blocked_path": path,
        }

    # ── 2026-05-15 P69 → 2026-06-05 软化: multi_edit thrashing 不再硬拒 ──

    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(target):
        return _file_not_found_response(ws_dir, path, action_hint="multi_edit")

    content, err = _read_text_safely(target)
    if err is not None:
        if isinstance(err.get("error"), str):
            err["error"] = err["error"].replace("<path>", path)
        return err

    # 链式应用所有 edit
    work = content
    applied = []
    failures = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            failures.append(f"edit[{i}]: not a dict")
            continue
        old_str = edit.get("old_str", "")
        new_str = edit.get("new_str", "")
        expected = int(edit.get("expected_count", 1) or 1)
        if not isinstance(old_str, str) or not isinstance(new_str, str):
            failures.append(f"edit[{i}]: old_str/new_str must be strings")
            continue
        if old_str == new_str:
            failures.append(f"edit[{i}]: old_str equals new_str (nothing to change)")
            continue
        if len(old_str) < _EDIT_OLD_STR_MIN_LEN:
            failures.append(
                f"edit[{i}]: old_str too short (len={len(old_str)} < {_EDIT_OLD_STR_MIN_LEN}); "
                f"include more context to make it unambiguous"
            )
            continue
        # 规范化
        old_norm = old_str.replace("\r\n", "\n").replace("\r", "\n")
        new_norm = new_str.replace("\r\n", "\n").replace("\r", "\n")

        # 配对校验(同 edit_file)
        def _net_brace(s: str, oc: str, cc: str) -> int:
            return s.count(oc) - s.count(cc)
        imbalance = []
        for o, c in (("{", "}"), ("(", ")"), ("[", "]")):
            d = _net_brace(old_norm, o, c) - _net_brace(new_norm, o, c)
            if d != 0:
                imbalance.append(f"`{o}{c}`")
        if old_norm.count('"') % 2 != new_norm.count('"') % 2:
            imbalance.append('`"` parity')
        if imbalance:
            failures.append(
                f"edit[{i}]: bracket/quote imbalance detected ({', '.join(imbalance)}); "
                f"extend old_str to cover whole pair"
            )
            continue

        # 计数
        actual = work.count(old_norm)
        if actual == 0:
            hint = ""
            if old_norm.strip() and old_norm.strip() in work:
                hint = " (note: stripped version found — check whitespace)"
            failures.append(
                f"edit[{i}]: old_str not found in current content{hint}; "
                "read only the local fragment and expand old_str context before retrying"
            )
            continue
        if actual != expected:
            failures.append(
                f"edit[{i}]: old_str matches {actual} times (expected {expected}); "
                f"expand old_str context to make it unique, or explicitly set expected_count={actual}"
            )
            continue

        # 应用替换 — 同时记录改后位置 + ±2 行 context
        # 2026-05-03 优化 #4:multi_edit return 自带 after_diff 证据,
        # 模型能直接从 result 看出"改对了",不需要再 search_in_file 验证。
        # 实测 trace 09ba132f bwt_fix 反模式:multi_edit → search → multi_edit
        # 浪费 1-2 个 iter。
        new_pos = work.find(old_norm)  # 改前位置(work 还没替换)
        work = work.replace(old_norm, new_norm, expected)
        # 改后,从 new_pos 开始 new_norm 是新内容
        # 计算改后 ±2 行 context(在 work 里)
        before_text = work[:new_pos]
        line_no = before_text.count("\n") + 1
        # 取改后位置开始 + new_norm 长度,前后扩 2 行
        new_end = new_pos + len(new_norm)
        # 找前 2 行起点
        ctx_start = new_pos
        nl_count_back = 0
        while ctx_start > 0 and nl_count_back < 2:
            ctx_start -= 1
            if work[ctx_start] == "\n":
                nl_count_back += 1
        if work[ctx_start] == "\n":
            ctx_start += 1
        # 找后 2 行终点
        ctx_end = new_end
        nl_count_fwd = 0
        while ctx_end < len(work) and nl_count_fwd < 2:
            if work[ctx_end] == "\n":
                nl_count_fwd += 1
                if nl_count_fwd >= 2:
                    break
            ctx_end += 1
        after_diff = work[ctx_start:ctx_end]
        # 限长(同一 multi_edit 多次 edit 总和别超 800 字符)
        if len(after_diff) > 250:
            after_diff = after_diff[:250] + "...[trim]"
        applied.append({
            "index": i,
            "replacements": expected,
            "first_change_at_line": line_no,
            "after_diff": after_diff,
        })

    # 任一失败 → 全部回滚(不写文件)
    if failures:
        return {
            "ok": False,
            "error": "multi_edit aborted: " + len(failures).__str__() + " edit(s) failed (no changes written)",
            "failures": failures,
            "applied_before_failure": applied,
            "hint": (
                "multi_edit is atomic: any failed edit rolls back the whole batch. Inspect failures, fix the exact "
                "fragments, then retry; split into separate edit_file calls only when debugging individual matches.\n"
                "multi_edit 原子回滚；先修正失败片段后再重试。"
            ),
        }

    # size cap 校验
    if len(work) > _FILE_SIZE_CAP:
        return {
            "ok": False,
            "error": f"resulting file would exceed size cap ({len(work)} > {_FILE_SIZE_CAP})",
        }

    # 提交
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(work)
    except OSError as e:
        return {"ok": False, "error": f"write failed: {e}"}

    result = {
        "ok": True,
        "action": "multi_edit",
        "path": path,
        "edits_applied": len(applied),
        "applied": applied,  # 2026-05-03 优化 #4:含 first_change_at_line + after_diff
        "new_size": len(work),
        "_next_step_hint": (
            "All edits were applied. Next, run the narrowest compile/test/verification command with bash/workspace.run. "
            "Each changed fragment is already shown in applied[i].after_diff, so avoid search-only confirmation.\n"
            "所有改动已应用；下一步运行最小验证。"
        ),
    }

    # 2026-05-03 优化 #5:同文件 edit 计数 + ≥3 次重写提示
    _edit_count, _edit_warning = _track_edit_count(ws_dir, path, "multi_edit")
    if _edit_warning:
        result["_edit_count"] = _edit_count
        result["_rewrite_suggestion"] = _edit_warning

    # 同 edit_file:回传文件顶部 30 行 + include 检测
    _src_exts = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh",
                 ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java")
    if path.lower().endswith(_src_exts):
        head_lines = work.split("\n", 31)[:30]
        head_text = "\n".join(head_lines)
        if len(head_text) <= 1500:
            result["file_head_30"] = head_text

    # .helper_notes.md 自动记账(同 edit_file)
    notes_path = os.path.join(ws_dir, ".helper_notes.md")
    is_helper_workspace = "_delegate_" in ws_dir
    if is_helper_workspace and not path.endswith(".helper_notes.md"):
        try:
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            note_line = f"- [{ts}] multi_edit {path}: {len(applied)} edits applied\n"
            with open(notes_path, "a", encoding="utf-8") as f:
                f.write(note_line)
            result["note_logged"] = True
        except OSError:
            pass

    if _env_edit_fact is not None:
        result.update(_env_edit_fact)

    return result


async def handle_insert_in_file(
    ws_dir: str, path: str, after_line: int, content_to_insert: str,
) -> dict:
    _sync_workspace_globals()

    """在指定行后插入内容。

    after_line:
      0    → 插在文件开头(第 1 行之前)
      -1   → 插在文件末尾
      N>0  → 插在第 N 行之后(第 N+1 行之前)
    """
    _access_error = _path_access_error(path, operation="insert")
    if _access_error is not None:
        return _access_error
    _contract_error = _helper_env_write_contract_error(path)
    if _contract_error is not None:
        return _contract_error
    _env_edit_fact = _existing_env_project_copy_edit_fact(ws_dir, path, "insert_in_file")
    # ── 2026-05-04 修复:_shared/ 写保护(Razor 教训) ──
    if _is_shared_readonly_path(path):
        return {
            "ok": False,
            "error": _SHARED_READONLY_ERROR_MSG,
            "blocked_path": path,
        }
    # ── 2026-05-15 P69 → 2026-06-05 软化: insert_in_file thrashing 不再硬拒 ──
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(target):
        return _file_not_found_response(ws_dir, path, action_hint="insert_in_file")

    content, err = _read_text_safely(target)
    if err is not None:
        if isinstance(err.get("error"), str):
            err["error"] = err["error"].replace("<path>", path)
        return err

    lines = content.split("\n")
    total_lines = len(lines)

    if after_line == -1:
        after_line = total_lines
    if after_line < 0 or after_line > total_lines:
        return {
            "ok": False,
            "error": f"after_line={after_line} out of range [0, {total_lines}]; "
                     f"use 0 for file start, -1 for file end, or 1..{total_lines} for after that line.\n"
                     f"after_line 超出范围；使用 0、-1 或有效行号。",
        }

    # 规范化插入内容的行尾;末尾若没换行符,内容自然附在新行
    insert_norm = content_to_insert.replace("\r\n", "\n").replace("\r", "\n")
    insert_lines = insert_norm.split("\n")

    new_lines = lines[:after_line] + insert_lines + lines[after_line:]
    new_content = "\n".join(new_lines)

    if len(new_content) > _FILE_SIZE_CAP:
        return {
            "ok": False,
            "error": (
                f"resulting file would exceed size cap ({len(new_content)} > {_FILE_SIZE_CAP}). "
                "Insert a smaller section or split the artifact.\n"
                "结果文件超过大小上限；请缩小插入内容或拆分产物。"
            ),
        }

    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return {"ok": False, "error": f"write failed: {e}"}

    result = {
        "ok": True,
        "action": "insert_in_file",
        "path": path,
        "inserted_after_line": after_line,
        "lines_inserted": len(insert_lines),
        "new_total_lines": len(new_lines),
    }

    # 2026-05-03 优化 #5:同文件 edit 计数 + ≥3 次重写提示
    _edit_count, _edit_warning = _track_edit_count(ws_dir, path, "insert_in_file")
    if _edit_warning:
        result["_edit_count"] = _edit_count
        result["_rewrite_suggestion"] = _edit_warning

    if _env_edit_fact is not None:
        result.update(_env_edit_fact)

    return result


async def handle_search_in_file(
    ws_dir: str, path: str, pattern: str,
    is_regex: bool = False, max_results: int | None = None,
) -> dict:
    _sync_workspace_globals()

    if max_results is None:
        max_results = _SEARCH_MAX_RESULTS_DEFAULT

    """文件内搜索,返回匹配行号 + 预览。
    流式扫描:大文件(最多 50MB)也能跑,不会爆内存。

    pattern: 文本(默认)或正则
    is_regex=True: pattern 视为 Python re 正则
    max_results: 找到 N 个就停
    """
    _access_error = _path_access_error(path, operation="search")
    if _access_error is not None:
        return _access_error
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    try:
        target = _safe_resolve(ws_dir, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(target):
        return _file_not_found_response(ws_dir, path)

    # search 用更宽松的 size cap(50MB)+ 二进制检测,不全文加载
    size, err = _check_file_readable(target, size_cap=_SEARCH_FILE_SIZE_CAP)
    if err is not None:
        # 替换错误信息里的 path 占位符
        if isinstance(err.get("error"), str):
            err["error"] = err["error"].replace("<path>", path)
        return err

    max_results = min(max(max_results, 1), 200)

    matcher = None
    if is_regex:
        try:
            matcher = re.compile(pattern)
        except re.error as e:
            return {"ok": False, "error": f"invalid regex: {e}"}

    matches: list[dict] = []
    truncated = False
    lines_scanned = 0

    # 流式扫描 + 简单超时(每 5000 行检查一次时间)
    import time as _t
    start_t = _t.monotonic()
    try:
        for line_no, line in _iter_text_lines(target):
            lines_scanned = line_no
            if line_no % 5000 == 0 and (_t.monotonic() - start_t) > _SEARCH_TIMEOUT_S:
                return {
                    "ok": False,
                    "error": (
                        f"search timed out after {_SEARCH_TIMEOUT_S}s "
                        f"(scanned {lines_scanned:,} lines); "
                        f"refine pattern, or use workspace run with a Python script "
                        f"that does targeted reading instead of full scan"
                    ),
                }
            hit = matcher.search(line) if matcher else (pattern in line)
            if hit:
                preview = line.strip()[:120]
                matches.append({"line": line_no, "preview": preview})
                if len(matches) >= max_results:
                    truncated = True
                    break
    except OSError as e:
        return {"ok": False, "error": f"read failed: {e}"}

    result = {
        "ok": True,
        "action": "search_in_file",
        "path": path,
        "pattern": pattern,
        "is_regex": is_regex,
        "total_found": len(matches),
        "matches": matches,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = f"results capped at {max_results}; refine pattern for fewer hits"
    return result
