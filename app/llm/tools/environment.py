"""Environment-mode project directory tools."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.core import debug
from app.core.environment_events import emit_environment_event
from app.core.environment_monitor import monitor as env_monitor
from app.core.filesystem import FileRegistry, stage_project_file
from app.core.filesystem.models import FileKind, FileStatus, Visibility
from app.core.filesystem.transfers import intake_workspace_file
from app.core.runtime_mode import current_environment
from app.llm.tools import workspace as ws_tool
from app.llm.tools.command_risk import analyze_command
from app.llm.tools import environment_resources as _env_resources
from app.llm.tools.memory_guard import (
    WorkspaceMemoryGuard,
    memory_limit_error,
    preflight_memory_check,
    workspace_memory_limits,
)
from app.llm.tools.process_utils import _kill_process_tree
from app.llm.tools.workspace import _translate_windows_command


TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".css", ".scss", ".html", ".xml", ".csv", ".sql",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".go", ".rs", ".cs", ".php",
    ".rb", ".sh", ".ps1", ".bat", ".cmd", ".dockerfile", ".gitignore",
}
MAX_READ_BYTES = 512 * 1024
ENV_READ_DEFAULT_MAX_CHARS = 20000
ENV_READ_ABSOLUTE_MAX_CHARS = 30000
MAX_FETCH_BYTES = 5 * 1024 * 1024
MAX_LIST_ITEMS = 500

SOURCE_PROJECT_EXTS = {
    ".py", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
    ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
    ".kt", ".swift", ".rb", ".php", ".scala", ".lua", ".sql",
}
PROJECT_TEXT_ARTIFACT_EXTS = {
    ".md", ".markdown", ".txt", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".xml",
}
_ENV_PROVENANCE_FILE = ".provenance.json"


def _load_env_provenance(workspace_dir: str) -> dict:
    if not workspace_dir:
        return {"files": {}}
    path = Path(workspace_dir).resolve() / "_env" / _ENV_PROVENANCE_FILE
    if not path.exists():
        return {"files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    if not isinstance(data, dict):
        return {"files": {}}
    files = data.get("files")
    if not isinstance(files, dict):
        data["files"] = {}
    return data


def _save_env_provenance(workspace_dir: str, data: dict) -> None:
    if not workspace_dir:
        return
    path = Path(workspace_dir).resolve() / "_env" / _ENV_PROVENANCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _environment_file_registry(workspace_dir: str) -> FileRegistry | None:
    if not workspace_dir:
        return None
    env = current_environment()
    if env is None:
        return None
    project_root = Path(env.root_dir).resolve()
    workspace_root = Path(workspace_dir).resolve()
    return FileRegistry.load(
        scope_id=f"env:{project_root}",
        workspace_root=workspace_root,
        project_root=project_root,
    )


def _record_registry_apply(
    workspace_dir: str,
    *,
    project_path: str,
    workspace_path: str = "",
    replacing: bool,
    sha256: str,
) -> None:
    registry = _environment_file_registry(workspace_dir)
    if registry is None:
        return
    record = registry.find_by_project_path(project_path)
    if record is None:
        try:
            target = registry.resolver.safe_project_path(project_path, must_exist=True)
            record = registry.upsert_project_file(
                target,
                kind=FileKind.PROJECT_SOURCE,
                status=FileStatus.APPLIED,
                visibility=Visibility.PROJECT,
                origin="env_apply_replace" if replacing else "env_apply_create",
                staged=bool(workspace_path),
            )
        except Exception:
            return
    record.status = FileStatus.APPLIED
    record.apply_state = "replaced" if replacing else "created"
    record.sha256 = sha256
    if workspace_path:
        record.workspace_path = workspace_path.replace("\\", "/").strip().lstrip("./")
    record.metadata["applied_at"] = time.time()
    record.metadata["apply_type"] = "replace" if replacing else "create"
    registry.add_or_update(record)
    registry.save()


def record_env_helper_outputs(
    workspace_dir: str,
    *,
    task_id: str,
    files: list[str],
    ok: bool,
    terminal_reason: str = "",
    outputs_complete: bool | None = None,
    kind: str = "",
    mode: str = "",
) -> None:
    """Record helper provenance for staged environment files.

    Only clean helper outputs may be applied to the real project directly.

    记录 `_env` 暂存文件来源；只有干净完成的 helper 产物可直接应用到真实项目。
    """
    if not workspace_dir or not files:
        return
    data = _load_env_provenance(workspace_dir)
    entries = data.setdefault("files", {})
    status = "ready" if ok and outputs_complete is not False else "failed"
    now = time.time()
    for raw in files:
        norm = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if not norm:
            continue
        if norm.startswith("_env/"):
            env_rel = norm[len("_env/"):]
        else:
            continue
        if not env_rel or env_rel == _ENV_PROVENANCE_FILE or env_rel.startswith("."):
            continue
        entries[env_rel] = {
            "status": status,
            "task_id": task_id,
            "ok": bool(ok),
            "outputs_complete": outputs_complete,
            "terminal_reason": terminal_reason,
            "kind": kind,
            "mode": mode,
            "updated_at": now,
        }
    _save_env_provenance(workspace_dir, data)


def _env_create_source_delegation_hint(path: str, content_len: int) -> dict:
    return {
        "ok": False,
        "error": "main_thread_source_create_should_delegate",
        "error_kind": "main_thread_source_create_should_delegate",
        "path": path,
        "content_chars": content_len,
        "hint": (
            "This is a substantial new project source/script file being authored directly by the main process. "
            "Keep the target absent, then continue by spawning or resuming a focused kind='code' helper with a "
            "helper request envelope: task_id, kind, mode, framework, input_files, prompt, expected_outputs, "
            "and acceptance_checks. For multi-file work, establish or reference the shared framework first, "
            "delegate coherent slices, inspect outputs, apply the resulting project files, and verify.\n\n"
            "主进程不直接写入大段新源码；保持目标未创建，使用完整 helper envelope 派发 code helper，完成后验收并应用。"
        ),
        "suggested_next_action": {
            "tool": "delegate",
            "action": "spawn",
            "task_template": {
                "kind": "code",
                "mode": "easy",
                "framework": "<shared interface/schema/outline and ownership contract>",
                "input_files": [],
                "prompt": f"Create or update the focused project slice that includes {path}.",
                "expected_outputs": [f"_env/{path}"],
                "acceptance_checks": ["read/inspect the produced file", "run the relevant local check"],
            },
        },
    }


def _env_create_project_text_delegation_hint(path: str, content_len: int) -> dict:
    return {
        "ok": False,
        "error": "main_thread_project_artifact_create_should_delegate",
        "error_kind": "main_thread_project_artifact_create_should_delegate",
        "path": path,
        "content_chars": content_len,
        "hint": (
            "This is a substantial new project framework, contract, report, data, or documentation artifact being "
            "authored directly by the main process. The main process keeps project ownership lightweight: "
            "write only compact private coordination notes directly, then delegate project-facing authored files through a "
            "helper request envelope with framework, inputs, expected_outputs, and acceptance_checks. Apply the "
            "helper output only after it reports clean completion and the file has been inspected or validated.\n\n"
            "主进程只直接写内部协调说明；项目侧框架、报告、数据或文档由 helper 产出并验收。"
        ),
        "suggested_next_action": {
            "tool": "delegate",
            "action": "spawn",
            "task_template": {
                "kind": "edit",
                "mode": "easy",
                "framework": "<purpose, structure, source evidence, ownership, and acceptance checks>",
                "input_files": [],
                "prompt": f"Create the focused project artifact {path} from the provided framework and evidence.",
                "expected_outputs": [f"_env/{path}"],
                "acceptance_checks": ["inspect the produced file", "verify requested sections and source coverage"],
            },
        },
    }


_SOURCE_MATERIAL_EXT_RE = re.compile(
    r"\.(?:docx|doc|pdf|pptx|ppt|xlsx|xls|png|jpe?g|webp|bmp|gif|tiff?)\b",
    re.IGNORECASE,
)
_BULK_SOURCE_TRAVERSAL_RE = re.compile(
    r"\b(?:os\.walk|rglob|glob\.glob|Path\([^)]*\)\.glob|Path\([^)]*\)\.rglob|for\s+\w+\s+in\s+.*(?:files|paths|documents|docs))\b",
    re.IGNORECASE | re.DOTALL,
)
_SOURCE_BODY_EXTRACTION_RE = re.compile(
    r"(?:"
    r"document\.xml|word/document\.xml|presentation\.xml|xl/sharedStrings\.xml|"
    r"Document\s*\(|docx\.Document|paragraphs|tables|extract_text|PdfReader|pdfplumber|fitz\.open|"
    r"openpyxl|load_workbook|pytesseract|image_to_string|ocr|zipfile|BeautifulSoup|itertext|"
    r"read_text\s*\(|decode\s*\(\s*['\"]utf-8"
    r")",
    re.IGNORECASE,
)
_SOURCE_OUTPUT_ACCUMULATION_RE = re.compile(
    r"(?:texts|contents|all_text|full_text|combined|summary|evidence|report)\s*(?:=|\.append|\.extend)",
    re.IGNORECASE,
)


def _env_run_source_material_guard(command: str, python_code: str) -> dict | None:
    """Return a recoverable guard response for main-thread bulk source extraction."""
    text = f"{command}\n{python_code or ''}"
    if not text.strip():
        return None
    ext_hits = _SOURCE_MATERIAL_EXT_RE.findall(text)
    if not ext_hits:
        return None
    traversal = bool(_BULK_SOURCE_TRAVERSAL_RE.search(text))
    extraction = bool(_SOURCE_BODY_EXTRACTION_RE.search(text))
    accumulation = bool(_SOURCE_OUTPUT_ACCUMULATION_RE.search(text))
    broad_reference = len(ext_hits) >= 3 or re.search(
        r"\b(?:all|every|many|batch|bulk|folder|directory|recursive|materials|documents|files)\b",
        text,
        re.IGNORECASE,
    )
    if not (extraction and (traversal or accumulation or broad_reference)):
        return None
    return {
        "ok": False,
        "error": "main_thread_bulk_source_material_read_should_delegate",
        "error_kind": "main_thread_bulk_source_material_read_should_delegate",
        "hint": (
            "This env_run call looks like broad source-material body extraction from Office/PDF/image files. "
            "Keep env_run for orientation, statistics, validation, and targeted spot checks. For content coverage, "
            "fetch the concrete project files when needed, spawn focused kind='read' helpers split by natural source "
            "group, file batch, page range, or image range, then collect their evidence files before synthesis. "
            "Use a code helper later only for computation over the extracted evidence or for implementation work.\n\n"
            "env_run 不承担批量材料正文抽取；先按文件组/页段/图片段派发 read helper，收集证据后再汇总或交给 code/edit。"
        ),
        "suggested_next_action": {
            "tool": "delegate",
            "action": "spawn",
            "task_template": {
                "kind": "read",
                "mode": "easy",
                "framework": "<source groups, coverage contract, evidence format, and acceptance checks>",
                "input_files": ["<fetched _env/... files or project-relative file list>"],
                "prompt": "Extract the assigned source-material content into compact, cited evidence. Preserve coverage counts, missing/unread items, and evidence paths.",
                "expected_outputs": ["<task_id>_evidence.txt"],
                "acceptance_checks": ["all assigned source files are accounted for", "unread or failed files are named with reasons"],
            },
        },
    }


def _should_delegate_main_env_create(path: str, content: str) -> bool:
    if not path or not content:
        return False
    try:
        from app.core.core_processes import current_helper_proc_id
        if current_helper_proc_id() is not None:
            return False
    except Exception:
        pass
    suffix = Path(path).suffix.lower()
    if suffix not in SOURCE_PROJECT_EXTS:
        return False
    stripped = content.strip()
    basename = Path(path).name.lower()
    # The main process may create only marker-like source files. Any authored
    # implementation, test, UI script, native source, or project entry point
    # belongs to a helper, even when each file is individually short.
    #
    # 主线程只允许极小源码标记文件；短实现文件也必须由 helper 产出。
    marker_names = {"__init__.py", ".gitignore"}
    marker_like = basename in marker_names and len(content) <= 300
    if marker_like and not re.search(
        r"(?m)^\s*(def|class|async\s+def|function|const|let|var|#include|public\s+class|fn|impl|package)\b",
        content,
    ):
        return False
    if len(content) >= 300:
        return True
    nonempty_lines = [line for line in content.splitlines() if line.strip()]
    if len(nonempty_lines) >= 8:
        return True
    marker_pattern = re.compile(
        r"(?m)^\s*(def|class|async\s+def|function|const|let|var|import|from|"
        r"#include|public\s+class|fn|impl|package)\b"
    )
    marker_hits = len(marker_pattern.findall(content))
    if marker_hits:
        return True
    return bool(stripped and suffix in {".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".cs"})


def _should_delegate_main_project_text_create(path: str, content: str) -> bool:
    if not path or not content:
        return False
    try:
        from app.core.core_processes import current_helper_proc_id
        if current_helper_proc_id() is not None:
            return False
    except Exception:
        pass
    suffix = Path(path).suffix.lower()
    if suffix not in PROJECT_TEXT_ARTIFACT_EXTS:
        return False
    norm = path.replace("\\", "/").lower()
    basename = Path(norm).name
    projectish_name = any(
        token in basename
        for token in (
            "contract", "framework", "spec", "outline", "schema",
            "report", "paper", "benchmark", "analysis", "results",
            "requirements", "design", "architecture", "plan",
        )
    ) or any(part in norm for part in ("/docs/", "/reports/", "/paper/", "/benchmark/", "/_shared/"))
    if not projectish_name:
        return False
    # Project-facing coordination artifacts shape later helper work. Keep them
    # helper-owned even when the initial file is short, so the main process
    # stays in orchestration/acceptance and does not accumulate many tiny
    # authored project files.
    #
    # 项目侧契约、框架、报告和基准数据即使较短，也应由 helper 产出后再验收应用。
    direct_allowed_names = {"notes.txt", "note.txt"}
    if basename in direct_allowed_names and len(content) <= 600:
        return False
    if any(
        token in basename
        for token in (
            "contract", "framework", "spec", "outline", "schema",
            "report", "paper", "benchmark", "analysis", "results",
            "requirements", "design", "architecture", "plan",
        )
    ):
        return True
    if any(part in norm for part in ("/docs/", "/reports/", "/paper/", "/benchmark/", "/_shared/")):
        return True
    if len(content) >= 1800:
        return True
    nonempty_lines = [line for line in content.splitlines() if line.strip()]
    return len(nonempty_lines) >= 45


def _env_apply_provenance_guard(
    workspace_dir: str,
    *,
    workspace_path: str,
    target_path: str,
    replacing: bool = False,
    require_ready: bool = False,
) -> dict | None:
    norm = str(workspace_path or "").replace("\\", "/").strip().lstrip("./")
    if not norm.startswith("_env/"):
        return None
    env_rel = norm[len("_env/"):]
    registry = _environment_file_registry(workspace_dir)
    record = registry.find_by_workspace_path(norm) if registry is not None else None
    if record is not None:
        if record.kind in {FileKind.STAGED_INPUT, FileKind.PROJECT_SOURCE} and record.status == FileStatus.STAGED:
            record = None
    if record is not None:
        ready_statuses = {
            FileStatus.READY,
            FileStatus.VERIFIED,
            FileStatus.PROMOTED,
            FileStatus.APPLIED,
            FileStatus.DELIVERED,
        }
        if record.verified or record.status in ready_statuses:
            return None
        return {
            "ok": False,
            "error": "staged_environment_file_not_ready",
            "error_kind": "staged_environment_file_not_ready",
            "path": target_path,
            "workspace_path": norm,
            "source_task_id": record.owner_task_id,
            "source_status": str(record.status),
            "source_terminal_reason": str(record.metadata.get("terminal_reason") or ""),
            "hint": (
                "The staged `_env/...` file is registered, but it is not marked ready or verified. Treat the "
                "registry state as authoritative. Continue or repair the same work until the file is ready, then "
                "apply it to the project.\n\n"
                "该暂存文件已登记但尚未就绪；继续同一任务并完成验证后再应用。"
            ),
            "suggested_next_action": {
                "tool": "delegate",
                "action": "spawn",
                "task_template": {
                    "task_id": record.owner_task_id or "<same_task_id>",
                    "resume": True,
                    "kind": record.helper_kind or "code",
                    "mode": "hard",
                    "framework": "<current framework plus registry status and file evidence>",
                    "input_files": [norm],
                    "prompt": (
                        "Continue from the registered staged file, inspect the current content, repair any "
                        "incomplete work, and finish with verified expected outputs."
                    ),
                    "expected_outputs": [norm],
                    "acceptance_checks": ["the staged file is ready or verified", "content has been inspected or tested"],
                },
            },
            "apply_type": "replace" if replacing else "create",
        }
    data = _load_env_provenance(workspace_dir)
    entry = (data.get("files") or {}).get(env_rel)
    if not isinstance(entry, dict):
        if require_ready:
            return {
                "ok": False,
                "error": "staged_environment_file_without_ready_provenance",
                "error_kind": "staged_environment_file_without_ready_provenance",
                "path": target_path,
                "workspace_path": norm,
                "source_status": "unknown",
                "hint": (
                    "This new project file is being applied from an `_env/...` staged copy without a clean helper "
                    "provenance record. Treat unknown staged files as unverified until project truth is checked. For compact files, use "
                    "env_apply_create with direct content. For substantial source, reports, contracts, data, or other "
                    "project artifacts, spawn a focused helper with declared expected_outputs, inspect the ready staged "
                    "file, then apply it.\n\n"
                    "未知来源的 `_env` 暂存新文件不能直接应用；小文件可直接创建，大文件需 helper 完成并验收。"
                ),
                "suggested_next_action": {
                    "tool": "delegate",
                    "action": "spawn",
                    "task_template": {
                        "kind": "code",
                        "mode": "easy",
                        "framework": "<shared framework, file ownership, interfaces, and acceptance checks>",
                        "input_files": [],
                        "prompt": f"Create the focused project file {target_path} and verify it before completion.",
                        "expected_outputs": [norm],
                        "acceptance_checks": ["outputs_complete is true", "inspect or test the staged file before apply"],
                    },
                },
                "apply_type": "replace" if replacing else "create",
            }
        return None
    status = str(entry.get("status") or "").lower()
    if status == "ready":
        return None
    task_id = str(entry.get("task_id") or "")
    terminal_reason = str(entry.get("terminal_reason") or "")
    return {
        "ok": False,
        "error": "staged_environment_file_not_ready",
        "error_kind": "staged_environment_file_not_ready",
        "path": target_path,
        "workspace_path": norm,
        "source_task_id": task_id,
        "source_status": status or "unknown",
        "source_terminal_reason": terminal_reason,
        "hint": (
            "This staged `_env/...` file came from a helper result that is not cleanly accepted. Apply it only after "
            "a clean acceptance step. Collect or read the helper result, inspect the failure evidence, then resume the "
            "same task_id or spawn a corrected same-kind helper. After a clean helper completion or an explicit "
            "verification pass produces a ready staged file, apply that file.\n\n"
            "该暂存文件来自未干净完成的 helper；先续作、修复或验证，再写入真实项目。"
        ),
        "suggested_next_action": {
            "tool": "delegate",
            "action": "spawn",
            "task_template": {
                "task_id": task_id or "<same_task_id>",
                "resume": True,
                "kind": entry.get("kind") or "code",
                "mode": "hard" if entry.get("mode") != "hard" else entry.get("mode") or "hard",
                "framework": "<previous framework plus the concrete failure evidence>",
                "input_files": [norm],
                "prompt": "Continue from the preserved staged file, fix the incomplete result, and verify before reporting completion.",
                "expected_outputs": [norm],
                "acceptance_checks": ["outputs_complete is true", "the staged file is inspected or tested before apply"],
            },
        },
        "apply_type": "replace" if replacing else "create",
    }


def _looks_like_directory_create_target(path: str) -> bool:
    clean = str(path or "").replace("\\", "/").strip().rstrip("/")
    if not clean:
        return False
    name = Path(clean).name
    if name in {"Makefile", "Dockerfile", "LICENSE", "README", "NOTICE"}:
        return False
    if "." in name:
        return False
    return True


def _env_run_fix_hint(
    command: str,
    stdout: str,
    stderr: str,
    *,
    python_code_used: bool = False,
    source_text: str = "",
) -> str:
    text = (stderr or "") + "\n" + (stdout or "")
    lowered = text.lower()
    attempted_source = command + "\n" + (source_text or "")
    mentions_env_staging = bool(re.search(r"(^|[\s\"'`=:/\\])_env[\\/]", attempted_source))
    if mentions_env_staging and any(
        marker in lowered
        for marker in (
            "no such file",
            "cannot find",
            "can't open file",
            "does not exist",
            "path not found",
            "not found",
            "找不到",
            "不存在",
        )
    ):
        return (
            "env_run executes with cwd set to the real project directory; chat-workspace staged `_env/...` copies "
            "belong to workspace tools. Use project-relative paths inside env_run commands and cwd values. Use read_file, "
            "edit_file, multi_edit, or helper handoff for fetched `_env/...` copies; for existing project edits use "
            "env_fetch -> edit the staged copy -> env_diff -> env_apply_replace. Rerun the corrected check before "
            "making claims."
            "\n\n"
            "env_run 只在真实项目目录中执行；`_env/...` 是聊天工作区暂存副本。命令里用项目相对路径，暂存副本用工作区读写工具处理。"
        )
    if any(marker in lowered for marker in ("is not recognized", "the term", "不是内部或外部命令")):
        return (
            "The command used shell syntax or utilities that are unavailable in the active platform. Rewrite the check "
            "with env_run python_code for portable inspection, or use a platform-native command with explicit output. "
            "Rerun the corrected command and rely only on successful output."
            "\n\n"
            "命令语法或工具不适合当前平台时，改用 env_run 的 python_code 或当前平台原生命令，并以成功输出为准。"
        )
    if not any(error_name in text for error_name in ("SyntaxError:", "IndentationError:", "TabError:")):
        return ""
    if "normalized_from" in text:
        return ""
    if "ast.parse" in text:
        return (
            "Python SyntaxError while parsing source text. If the script is inspecting project code, parse the complete "
            "file once and then filter AST nodes by line range, or use textual line scanning for partial ranges. Prefer "
            "complete Python files for ast.parse because arbitrary slices may start or end inside an open "
            "string, call, class, function, or parenthesized expression. Rerun the corrected inspection and rely only "
            "on the successful output."
            "\n\n"
            "源码分析脚本不要对任意截取片段做 ast.parse；应解析完整文件再按行号筛选，或改用文本扫描。"
        )
    if python_code_used:
        return (
            "Python SyntaxError inside env_run python_code. The command transport is already safe; fix the Python script "
            "itself before drawing conclusions. Check the reported line, indentation, quotes, and compound statements, "
            "then rerun the corrected script and rely only on successful output."
            "\n\n"
            "python_code 内部语法错误时，先修脚本并重跑；不能用失败输出下结论。"
        )
    hint = (
        "Python SyntaxError. Treat the error text as evidence and fix the probe before drawing conclusions. "
        "Use env_run python_code for nontrivial Python inspection, especially statements that need indentation "
        "such as try/except, for/while, with, def/class, if/else, or multiline assertions. "
        "A normalized python -c command solves shell quoting but still runs normal Python syntax; compound "
        "statements belong in multiline python_code or a script file. "
        "Rerun the corrected check and rely only on the successful output."
        "\n\n"
        "Python 语法错误时先修正探针；复杂检查用 env_run 的 python_code，多行验证成功后再下结论。"
    )
    return hint


def _env_run_usage_hint(error: str) -> str:
    if error == "both_command_and_python_code":
        return (
            "env_run accepts exactly one execution body. Use command for shell commands such as pytest, build, grep, "
            "or CLI smoke tests. Use python_code by itself for Python inspections, statistics, file traversal, or "
            "multiline probes; the tool will run it from a temporary script with cwd set to the project directory. "
            "Resubmit the same check with only the suitable field, then rely on the successful output."
            "\n\n"
            "env_run 一次只接收一种执行内容；shell 命令用 command，Python 检查脚本单独用 python_code。"
        )
    if error == "missing_execution_body":
        return (
            "env_run needs either a command or python_code. Choose command for project commands, or python_code for "
            "inspection scripts that should not become project files. Run the corrected check before making claims."
            "\n\n"
            "env_run 需要 command 或 python_code；先补齐并成功执行检查后再下结论。"
        )
    return ""


ENV_LIST_TREE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_list_tree",
        "description": (
            "List files and directories inside the current project directory. Read-only. "
            "This is for browsing and locating paths. Complete ranking/statistics require truncated=false. "
            "If truncated=true, use env_run for exact statistics or continue listing narrower subdirectories before answering largest/smallest/count/ranking questions."
            "\n\n用于浏览和定位路径；截断结果不能作为完整统计。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative directory. Defaults to the project root.\n项目相对目录。"},
                "max_depth": {"type": "integer", "description": "Recursive depth. Defaults to 2.\n递归深度。", "default": 2},
                "limit": {"type": "integer", "description": "Maximum number of returned items. Defaults to 200.\n返回条目上限。", "default": 200},
            },
        },
    },
}


ENV_INVENTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_inventory",
        "description": (
            "Return a compact exact inventory of project files and write `_env/project_inventory.md` plus "
            "`_env/.resource_manifest.json` for helper handoff. Use it before broad all-files, source-material, "
            "or unfamiliar-project work so the main process can delegate from exact project_path/staged_path values "
            "instead of guessing paths. It is for path truth, categories, suffix counts, and staging status; delegate "
            "content extraction to read/code/edit helpers.\n\n"
            "返回精确项目资源清单和 helper 交接清单；用于路径真值、分类、后缀统计和暂存状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional category filter such as text, code, office_pdf, image, audio_video, archive, or other.\n可选资源类别过滤。",
                },
                "suffixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional suffix filter such as .docx, .pdf, .png, or txt.\n可选文件后缀过滤。",
                },
                "limit": {"type": "integer", "description": "Maximum listed resource rows. Defaults to 160.\n清单行数上限。", "default": 160},
                "include_unstaged": {"type": "boolean", "description": "Whether to include files not yet staged into `_env`. Defaults to true.\n是否包含尚未暂存的文件。", "default": True},
                "stage": {
                    "type": "boolean",
                    "description": "Whether to stage the listed files into `_env` in this call. Defaults to false; use with filters and a safe stage_limit.\n是否本次将筛选文件暂存到 _env。",
                    "default": False,
                },
                "stage_limit": {"type": "integer", "description": "Maximum files to stage when stage=true. Defaults to 40.\n单次暂存文件数量上限。", "default": 40},
                "max_stage_bytes": {
                    "type": "integer",
                    "description": "Maximum single-file size staged by this inventory call. Defaults to 5242880 bytes.\n单个暂存文件大小上限。",
                    "default": 5242880,
                },
            },
        },
    },
}


def _list_tree_truncated_note(limit: int) -> str:
    return (
        f"Result stopped after {limit} items and is a partial project listing. "
        "Use full traversal or env_inventory for largest/smallest/count/ranking/statistics questions. "
        "For exact file counts, sizes, line counts, or top-N rankings, run an explicit env_run script that walks the project tree, "
        "or call env_list_tree again on narrower subdirectories until truncated=false."
        "\n\n目录树被截断时只能作定位；统计排行需完整遍历或 env_inventory。"
    )


ENV_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_read",
        "description": "Read a text file or line range from the project directory. Read-only. Large files return paged content; use start_line/end_line or helpers for full coverage.\n读取项目文本文件或行范围；大文件需分页或交给 helper 覆盖。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path.\n项目相对文件路径。"},
                "start_line": {"type": "integer", "description": "Starting line. Defaults to 1.\n起始行号。", "default": 1},
                "end_line": {"type": "integer", "description": "Ending line. Defaults to the truncation limit.\n结束行号。", "default": -1},
                "max_chars": {"type": "integer", "description": "Maximum returned characters. Defaults to 20000; use helpers for broad reading.\n返回字符上限。", "default": 20000},
            },
            "required": ["path"],
        },
    },
}


ENV_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_search",
        "description": "Search project text files by plain text or regex. Read-only. Use it to locate code or configuration.\n在项目文本文件中搜索，用于定位代码或配置。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text or regex pattern.\n搜索文本或正则。"},
                "path": {"type": "string", "description": "Project-relative file or directory. Defaults to root.\n项目相对文件或目录。"},
                "regex": {"type": "boolean", "description": "Whether query is a regex.\n是否按正则搜索。", "default": False},
                "limit": {"type": "integer", "description": "Maximum matched lines. Defaults to 50.\n匹配行数上限。", "default": 50},
            },
            "required": ["query"],
        },
    },
}


ENV_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_fetch",
        "description": (
            "Copy a project file into the chat workspace under _env/ for editing and record the source sha256. "
            "Always call this before modifying an existing project file. The returned `_env/...` path is a staged "
            "workspace copy for read_file/edit_file/multi_edit/insert_in_file or helper handoff; env_run uses real "
            "project-relative paths instead."
            "\n\n把真实项目文件取到 _env 暂存副本，后续编辑和 helper 交接使用该副本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path.\n项目相对文件路径。"},
            },
            "required": ["path"],
        },
    },
}


ENV_DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_diff",
            "description": (
                "Compare an existing project source file with an edited `_env/...` workspace copy. Call this before "
                "env_apply_replace. Use it after editing the staged copy, not after writing directly to the project."
                "\n\n对比真实项目文件和 _env 暂存副本；用于 apply 前确认差异。"
            ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative source file path.\n项目相对源文件路径。"},
                "workspace_path": {"type": "string", "description": "Optional workspace copy path, usually _env/<path>.\n可选暂存副本路径。"},
                "max_chars": {"type": "integer", "description": "Maximum diff characters. Defaults to 30000.\n差异输出字符上限。", "default": 30000},
            },
            "required": ["path"],
        },
    },
}


ENV_APPLY_REPLACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_apply_replace",
            "description": (
                "Replace an existing project file with an edited workspace copy. "
                "Requires the exact expected_hash value returned by the latest env_fetch for this path; "
                "use the real hash value instead of placeholders such as unknown. Refuses overwrite if the project source changed. This is the "
                "apply step for existing project files after env_fetch, staged editing, and env_diff."
                "\n\n用已编辑暂存副本替换真实项目文件；需精确 env_fetch 哈希并在 diff 后执行。"
            ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path.\n项目相对文件路径。"},
                "workspace_path": {"type": "string", "description": "Optional workspace copy path, usually _env/<path>.\n可选暂存副本路径。"},
                "expected_hash": {
                    "type": "string",
                    "description": "Exact sha256 returned by env_fetch for this path; use the real hash value.\n从 env_fetch 复制的精确 sha256。",
                },
            },
            "required": ["path", "expected_hash"],
        },
    },
}


ENV_APPLY_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_apply_create",
            "description": (
                "Create a new project file from a workspace file or direct text content. Use only after confirming the target path does not already exist. "
                "Direct content is for tiny private markers or notes. "
                "Project-facing contracts, frameworks, outlines, manifests, stubs, source, tests, scripts, benchmark code, reports, documents, and broad generated sections should be authored by the appropriate helper and then applied from workspace_path or verified project files. "
                "Create new project files through env_apply_create or accepted helper outputs rather than leaving workspace.write `_env/...` paths staged. "
                "Refuses to overwrite an existing project file; for existing files use env_fetch, edit, env_diff, then env_apply_replace."
                "\n\n创建新的真实项目文件；面向项目的实质内容应由 helper 产出后再应用。"
            ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "New project-relative target file path.\n新的项目相对目标文件路径。"},
                "workspace_path": {"type": "string", "description": "Workspace source file path. Optional when content is provided.\n工作区源文件路径。"},
                "content": {"type": "string", "description": "Direct text for tiny private markers or notes only; project-facing artifacts should come from helper output via workspace_path.\n仅用于很小的内部标记或备注。"},
            },
            "required": ["path"],
        },
    },
}


ENV_RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_run",
        "description": (
            "Run a command inside the current project directory or a project subdirectory for tests, builds, or checks. "
            "Prefer this over workspace/bash for project validation so pytest/build cwd stays isolated. "
            "env_run uses the real project tree; inside command and cwd, use project-relative paths from the real project root. "
            "Use read_file/edit_file/multi_edit or helpers for `_env/...` copies, then env_diff/env_apply_replace to update real project files. "
            "For Python inspection scripts, pass python_code instead of writing a workspace file and copying it into the project; env_run will execute it from a system temporary file with cwd set to the project path and then delete it. "
            "For nontrivial Python snippets, use a normalized python -c command or a temporary script outside the project tree; "
            "the tool will also normalize multiline python -c snippets when possible. "
            "For directory statistics, line counts, rankings, loops, nested quotes, or f-strings, prefer a temporary .py script "
            "outside the project tree or a normalized python -c command with explicit print output; after a SyntaxError, change the quoting or script shape before retrying. "
            "For statistics, keep metric names and units exact. Characters, bytes, file size, line count, and file count are different metrics; "
            "compute and label the requested metric, or print both chars and bytes when the request is ambiguous. Exclude transient inspection scripts, caches, and generated probe files unless explicitly requested. "
            "Use env_run for broad source-material statistics, inventories, and targeted spot checks, not for bulk body extraction from Office/PDF/image files. "
            "When content coverage from many source materials matters, fetch concrete files if needed, split by source group/file batch/page/image range, delegate kind='read' helpers first, then collect evidence files before synthesis. "
            "Inspection scripts are temporary files: run them through env_run or clean them if a command creates one in the project. "
            "Use this for commands and inspection rather than direct source-code writing.\n\n"
            "env_run 用于真实项目目录内验证、统计和定点抽查；批量 Office/PDF/图片正文抽取先分派 read helpers；临时检查脚本不属于项目文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run.\n要执行的命令。"},
                "python_code": {
                    "type": "string",
                    "description": (
                        "Optional Python script body for inspection/statistics. When set, env_run writes it to a system temporary file, "
                        "runs it with cwd set to the project directory, and deletes it automatically. Keep inspection scripts temporary instead of applying them as project files.\n"
                        "可选临时 Python 检查脚本正文。"
                    ),
                },
                "cwd": {"type": "string", "description": "Project-relative working directory. Defaults to root.\n项目相对工作目录。"},
                "timeout_sec": {"type": "integer", "description": "Timeout in seconds. Defaults to 30, max 1800.\n命令超时秒数。", "default": 30},
            },
        },
    },
}


ENVIRONMENT_TOOL_SCHEMAS = [
    ENV_INVENTORY_SCHEMA,
    ENV_LIST_TREE_SCHEMA,
    ENV_READ_SCHEMA,
    ENV_SEARCH_SCHEMA,
    ENV_FETCH_SCHEMA,
    ENV_DIFF_SCHEMA,
    ENV_APPLY_REPLACE_SCHEMA,
    ENV_APPLY_CREATE_SCHEMA,
    ENV_RUN_SCHEMA,
]


def environment_tool_names() -> set[str]:
    return {schema["function"]["name"] for schema in ENVIRONMENT_TOOL_SCHEMAS}


def _env_required() -> tuple[Path, object]:
    env = current_environment()
    if env is None:
        raise RuntimeError("environment tools are only available in environment mode")
    return Path(env.root_dir).resolve(), env


def _resolve_env_path(rel_path: str, *, must_exist: bool | None = None) -> Path:
    root, _ = _env_required()
    raw = (rel_path or ".").strip().strip('"')
    if not raw or raw in {"/", "\\"}:
        raw = "."
    p = Path(raw)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (root / p).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes environment root: {rel_path!r}")
    if must_exist is True and not resolved.exists():
        raise FileNotFoundError(f"path not found: {rel_path}")
    if must_exist is False and resolved.exists():
        raise FileExistsError(f"path already exists: {rel_path}")
    return resolved


def _rel_to_root(path: Path) -> str:
    root, _ = _env_required()
    return path.resolve().relative_to(root).as_posix()


def _resolve_read_path(rel_path: str, workspace_dir: str = "") -> tuple[Path, str, str]:
    """Resolve env_read inputs across project and staged workspace paths."""
    raw = (rel_path or ".").strip().strip('"')
    norm = raw.replace("\\", "/").lstrip("./")
    if workspace_dir and (norm == "_env" or norm.startswith("_env/")):
        workspace_root = Path(workspace_dir).resolve()
        staged = (workspace_root / norm).resolve()
        try:
            staged.relative_to(workspace_root)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace root: {rel_path!r}") from exc
        if not staged.exists():
            raise FileNotFoundError(f"staged workspace path not found: {rel_path}")
        return staged, norm, "workspace_staged"
    project_path = _resolve_env_path(rel_path, must_exist=True)
    return project_path, _rel_to_root(project_path), "project"


def _path_candidate_score(query: str, candidate: str) -> tuple[int, int]:
    q = query.replace("\\", "/").strip().lower()
    c = candidate.replace("\\", "/").strip().lower()
    q_name = Path(q).name
    c_name = Path(c).name
    if q == c:
        return (0, len(c))
    if q_name and q_name == c_name:
        return (1, len(c))
    if q and c.endswith(q):
        return (2, len(c))
    if q_name and q_name in c_name:
        return (3, len(c))
    q_tokens = [token for token in re.split(r"[/\s._()（）-]+", q) if token]
    matched = sum(1 for token in q_tokens if token in c)
    if matched:
        return (10 - matched, len(c))
    return (99, len(c))


def _env_path_candidates(query: str, *, limit: int = 8) -> list[dict[str, object]]:
    root, _ = _env_required()
    candidates: list[tuple[tuple[int, int], Path]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = _rel_to_root(path)
        score = _path_candidate_score(query, rel)
        if score[0] < 99:
            candidates.append((score, path))
    result = []
    for _, path in sorted(candidates, key=lambda item: item[0])[:limit]:
        try:
            result.append({"path": _rel_to_root(path), "size": path.stat().st_size})
        except OSError:
            result.append({"path": _rel_to_root(path)})
    return result


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_text(path: Path) -> bool:
    if path.name.lower() in {"dockerfile", "makefile"}:
        return True
    return path.suffix.lower() in TEXT_EXTS


def _resource_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in SOURCE_PROJECT_EXTS:
        return "code"
    if suffix in TEXT_EXTS:
        return "text"
    if suffix in {".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm"}:
        return "office_pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        return "image"
    if suffix in {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".mkv"}:
        return "audio_video"
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}:
        return "archive"
    return "other"


def _read_text(path: Path, max_bytes: int = MAX_READ_BYTES) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace")


def _workspace_env_path(workspace_dir: str, rel_path: str) -> Path:
    normalized = Path(rel_path.replace("\\", "/"))
    parts = [part for part in normalized.parts if part not in ("", ".", "..")]
    target = Path(workspace_dir).resolve() / "_env" / Path(*parts)
    target.resolve().relative_to(Path(workspace_dir).resolve())
    return target


def _manifest_path(workspace_dir: str) -> Path:
    return Path(workspace_dir).resolve() / "_env" / ".manifest.json"


def _load_manifest(workspace_dir: str) -> dict:
    path = _manifest_path(workspace_dir)
    if not path.exists():
        return {"files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}
    if not isinstance(data, dict):
        return {"files": {}}
    data.setdefault("files", {})
    return data


def _save_manifest(workspace_dir: str, data: dict) -> None:
    path = _manifest_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def handle_environment_tool(name: str, workspace_dir: str, args: dict) -> str:
    args = args or {}
    env_ctx = current_environment()
    event_base = {
        "trace_id": debug.current_trace_id() or "",
        "archive_id": env_ctx.archive_id if env_ctx else "",
        "group_id": env_ctx.group_id if env_ctx else "",
        "user_id": env_ctx.user_id if env_ctx else "",
    }
    emit_environment_event(
        "workflow",
        {"kind": "tool_start", "tool": name, "args": args, **event_base},
    )
    await env_monitor.publish(
        "workflow",
        {"kind": "tool_start", "tool": name, "args": args, **event_base},
    )
    try:
        if name == "env_list_tree":
            result = _handle_list_tree(args)
        elif name == "env_inventory":
            result = _handle_inventory(workspace_dir, args)
        elif name == "env_read":
            result = _handle_read(args, workspace_dir)
        elif name == "env_search":
            result = _handle_search(args)
        elif name == "env_fetch":
            result = _handle_fetch(workspace_dir, args)
        elif name == "env_diff":
            result = _handle_diff(workspace_dir, args)
        elif name == "env_apply_replace":
            result = _handle_apply_replace(workspace_dir, args)
        elif name == "env_apply_create":
            result = _handle_apply_create(workspace_dir, args)
        elif name == "env_run":
            result = await _handle_run(args)
        else:
            result = {"ok": False, "error": f"unknown environment tool: {name}"}
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    emit_environment_event(
        "workflow",
        {"kind": "tool_done", "tool": name, "ok": bool(result.get("ok", False)), **event_base},
    )
    await env_monitor.publish(
        "workflow",
        {"kind": "tool_done", "tool": name, "ok": bool(result.get("ok", False)), **event_base},
    )
    debug.log(f"environment.{name}", str(result)[:500], result)
    return json.dumps(result, ensure_ascii=False)


def _handle_list_tree(args: dict) -> dict:
    base = _resolve_env_path(str(args.get("path") or "."), must_exist=True)
    if not base.is_dir():
        return {"ok": False, "error": "path is not a directory", "path": _rel_to_root(base)}
    max_depth = max(0, min(int(args.get("max_depth", 2) or 2), 8))
    limit = max(1, min(int(args.get("limit", 200) or 200), MAX_LIST_ITEMS))
    rows = []
    root_depth = len(base.parts)
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
    for current, dirs, files in os.walk(base):
        cur = Path(current)
        depth = len(cur.parts) - root_depth
        dirs[:] = [d for d in sorted(dirs) if d not in skip_dirs]
        if depth >= max_depth:
            dirs[:] = []
        for d in dirs:
            p = cur / d
            rows.append({"path": _rel_to_root(p), "type": "dir"})
            if len(rows) >= limit:
                return {
                    "ok": True,
                    "root": _rel_to_root(base),
                    "items": rows,
                    "truncated": True,
                    "incomplete": True,
                    "next_action_instruction": _list_tree_truncated_note(limit),
                }
        for f in sorted(files):
            p = cur / f
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            rows.append({"path": _rel_to_root(p), "type": "file", "size": size})
            if len(rows) >= limit:
                return {
                    "ok": True,
                    "root": _rel_to_root(base),
                    "items": rows,
                    "truncated": True,
                    "incomplete": True,
                    "next_action_instruction": _list_tree_truncated_note(limit),
                }
    result = {"ok": True, "root": _rel_to_root(base), "items": rows, "truncated": False}
    hint = _list_tree_workflow_hint(rows)
    if hint:
        result["next_action_instruction"] = hint
    return result


def _handle_inventory(workspace_dir: str, args: dict) -> dict:
    if not workspace_dir:
        return {"ok": False, "error": "workspace is required for env_inventory"}
    root, _ = _env_required()
    workspace_root = Path(workspace_dir).resolve()
    try:
        manifest = _env_resources.write_resource_manifest_files(root, workspace_root, [{
            "kind": "inventory",
            "task_id": "env_inventory",
            "prompt": "Build an exact project inventory for helper handoff.",
        }])
    except Exception as exc:
        return {"ok": False, "error": f"inventory_failed:{type(exc).__name__}: {exc}"}

    requested_categories = {
        str(item or "").strip().lower()
        for item in (args.get("categories") or [])
        if str(item or "").strip()
    }
    requested_suffixes = {
        ("." + str(item or "").strip().lower().lstrip("."))
        for item in (args.get("suffixes") or [])
        if str(item or "").strip()
    }
    limit = max(1, min(int(args.get("limit", 160) or 160), 1000))
    include_unstaged = bool(args.get("include_unstaged", True))
    stage = bool(args.get("stage", False))
    stage_limit = max(0, min(int(args.get("stage_limit", 40) or 40), 200))
    max_stage_bytes = max(1, min(int(args.get("max_stage_bytes", MAX_FETCH_BYTES) or MAX_FETCH_BYTES), MAX_FETCH_BYTES))
    registry = _environment_file_registry(workspace_dir) if stage else None
    staged_now: list[str] = []
    stage_skipped: list[dict] = []
    resources = manifest.get("resources") if isinstance(manifest, dict) else []
    summary = manifest.get("summary") if isinstance(manifest, dict) else {}
    rows: list[dict] = []
    for item in resources if isinstance(resources, list) else []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "")
        suffix = str(item.get("suffix") or "").lower()
        if requested_categories and category.lower() not in requested_categories:
            continue
        if requested_suffixes and suffix not in requested_suffixes:
            continue
        if not include_unstaged and not item.get("staged"):
            continue
        project_path = str(item.get("project_path") or "")
        staged_path = str(item.get("staged_path") or "")
        staged = bool(item.get("staged"))
        if stage and project_path and not staged:
            size = int(item.get("size") or 0)
            if registry is None:
                stage_skipped.append({"project_path": project_path, "reason": "registry_unavailable"})
            elif len(staged_now) >= stage_limit:
                stage_skipped.append({"project_path": project_path, "reason": "stage_limit"})
            elif size > max_stage_bytes:
                stage_skipped.append({"project_path": project_path, "reason": "file_too_large", "size": size})
            else:
                try:
                    record = stage_project_file(registry, project_path)
                    staged_path = record.workspace_path
                    item["staged_path"] = staged_path
                    item["staged"] = True
                    staged = True
                    staged_now.append(project_path)
                except Exception as exc:
                    stage_skipped.append({"project_path": project_path, "reason": f"stage_failed:{type(exc).__name__}"})
        rows.append({
            "project_path": project_path,
            "staged_path": staged_path,
            "category": category,
            "suffix": suffix,
            "size": item.get("size"),
            "staged": staged,
            "key_candidate": bool(item.get("key_candidate", False)),
        })
        if len(rows) >= limit:
            break
    if staged_now:
        try:
            manifest = _env_resources.write_resource_manifest_files(root, workspace_root, [{
                "kind": "inventory",
                "task_id": "env_inventory",
                "prompt": "Refresh manifest after inventory staging.",
            }])
            if isinstance(manifest, dict):
                summary = manifest.get("summary") or summary
        except Exception:
            pass
    return {
        "ok": True,
        "summary": summary,
        "filters": {
            "categories": sorted(requested_categories),
            "suffixes": sorted(requested_suffixes),
            "include_unstaged": include_unstaged,
            "limit": limit,
            "stage": stage,
            "stage_limit": stage_limit,
            "max_stage_bytes": max_stage_bytes,
        },
        "resources": rows,
        "staged_now": staged_now,
        "stage_skipped": stage_skipped[:80],
        "truncated": len(rows) >= limit,
        "manifest_paths": {
            "inventory": "_env/project_inventory.md",
            "resource_manifest": "_env/.resource_manifest.json",
        },
        "next_action_instruction": (
            "The main process may read the generated manifest paths directly with env_read. Use project_path values "
            "with env_fetch when project files must be staged, or call env_inventory with stage=true and narrow filters "
            "to stage a safe batch in one call. Use staged_path values that already exist for helper prompts. Split "
            "broad content extraction by category, directory, file batch, page range, or image range and delegate read "
            "helpers before synthesis.\n"
            "主进程可直接读取生成的清单；获取项目文件用 project_path，交给 helper 用已存在的 staged_path；大范围正文抽取先分批给 read helper。"
        ),
    }


def _list_tree_workflow_hint(rows: list[dict]) -> str:
    files = [r for r in rows if r.get("type") == "file"]
    dirs = [r for r in rows if r.get("type") == "dir"]
    if len(files) < 8 and len(rows) < 20:
        return ""
    file_paths = [str(r.get("path") or "").replace("\\", "/") for r in files]
    dir_paths = [str(r.get("path") or "").replace("\\", "/") for r in dirs]
    suffixes = {Path(path).suffix.lower() for path in file_paths}
    source_material_suffixes = {
        ".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".doc", ".pdf",
        ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".png", ".jpg", ".jpeg",
        ".webp", ".bmp", ".gif", ".mp3", ".wav", ".m4a", ".mp4", ".zip",
    }
    code_suffixes = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go",
        ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".rb", ".swift",
        ".kt", ".kts", ".scala", ".sh", ".bat", ".ps1", ".sql", ".r", ".lua",
    }
    config_names = {
        "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
        "pyproject.toml", "requirements.txt", "poetry.lock", "uv.lock", "setup.py",
        "cargo.toml", "cargo.lock", "go.mod", "go.sum", "cmakelists.txt",
        "makefile", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "tsconfig.json", "vite.config.ts", "webpack.config.js", "rollup.config.js",
        "pytest.ini", "tox.ini", "ruff.toml", ".eslintrc", ".prettierrc",
    }
    doc_names = {"readme.md", "readme.txt", "readme", "changelog.md", "license"}
    source_material_count = sum(
        1 for path in file_paths
        if Path(path).suffix.lower() in source_material_suffixes
    )
    code_count = sum(1 for path in file_paths if Path(path).suffix.lower() in code_suffixes)
    config_count = sum(1 for path in file_paths if Path(path).name.lower() in config_names)
    doc_count = sum(1 for path in file_paths if Path(path).name.lower() in doc_names)
    test_count = sum(
        1 for path in file_paths
        if "/test" in f"/{path.lower()}" or Path(path).name.lower().startswith("test_")
        or ".test." in Path(path).name.lower() or ".spec." in Path(path).name.lower()
    )
    nested_dir_count = sum(1 for path in dir_paths if "/" in path)
    many_files = len(files) >= 25 or len(rows) >= 40
    mixed_materials = source_material_count >= 5 or len(suffixes & source_material_suffixes) >= 4
    complex_project = (
        (code_count >= 5 and (config_count > 0 or test_count > 0 or doc_count > 0))
        or (code_count >= 8 and len(dirs) >= 3)
        or (config_count >= 2 and (code_count >= 2 or test_count > 0))
        or (nested_dir_count >= 4 and code_count >= 4)
    )
    broad_directory = len(dirs) >= 10 and len(files) >= 12
    if not (many_files or mixed_materials or complex_project or broad_directory):
        return ""
    return (
        "This directory listing is large, mixed, or structurally complex. "
        "Treat `kind='inventory'` as an advanced directory tree/project inventory and first-pass inventory before bulk reading in the main thread: "
        "file categories, directory roles, likely entry points, README/docs, config/build/test hints, source-material coverage, "
        "unread binary/Office/PDF/image/audio categories, exact lightweight statistics, and recommended next read targets. "
        "Then use targeted env_read/env_fetch and read/edit/code helpers from that inventory.\n\n"
        "目录较大、材料较多或工程结构复杂；先用 inventory helper 做高级目录树/工程索引，再按摘要定向读取和分派。"
    )


def _handle_read(args: dict, workspace_dir: str = "") -> dict:
    requested_path = str(args.get("path") or "")
    path, display_path, source_zone = _resolve_read_path(requested_path, workspace_dir)
    if not path.is_file():
        return {"ok": False, "error": "path is not a file", "path": display_path, "source_zone": source_zone}
    if not _looks_text(path):
        return {
            "ok": False,
            "error": "file does not look like text; use env_fetch for binary copy",
            "path": display_path,
            "source_zone": source_zone,
        }
    max_chars = max(1000, min(int(args.get("max_chars", ENV_READ_DEFAULT_MAX_CHARS) or ENV_READ_DEFAULT_MAX_CHARS), ENV_READ_ABSOLUTE_MAX_CHARS))
    start_line = max(1, int(args.get("start_line", 1) or 1))
    end_line = int(args.get("end_line", -1) or -1)
    text = _read_text(path)
    lines = text.splitlines()
    start_idx = start_line - 1
    end_idx = len(lines) if end_line < start_line else min(end_line, len(lines))
    selected_lines = lines[start_idx:end_idx]
    out_lines: list[str] = []
    chars_used = 0
    actual_end = start_line - 1
    truncated = False
    for offset, line in enumerate(selected_lines):
        line_no = start_line + offset
        chunk = line
        if chars_used + len(chunk) + 1 > max_chars:
            truncated = True
            break
        out_lines.append(chunk)
        chars_used += len(chunk) + 1
        actual_end = line_no
    if actual_end < end_idx:
        truncated = True
    result = {
        "ok": True,
        "path": display_path,
        "source_zone": source_zone,
        "sha256": _sha256(path),
        "start_line": start_line,
        "end_line": actual_end,
        "total_lines": len(lines),
        "content": "\n".join(out_lines),
        "truncated": truncated,
    }
    if truncated:
        result["next_start_line"] = actual_end + 1
        result["note"] = (
            f"Content was paged at {max_chars} characters. Continue with start_line={actual_end + 1}, "
            "or delegate broad reading to a read helper and keep the main process focused on coordination.\n"
            f"内容已分页；继续读取请用 start_line={actual_end + 1}，大范围阅读优先交给 read helper。"
        )
    return result


def _handle_search(args: dict) -> dict:
    import re
    query = str(args.get("query") or "")
    if not query:
        return {"ok": False, "error": "query is required"}
    base = _resolve_env_path(str(args.get("path") or "."), must_exist=True)
    if base.is_file():
        files = [base]
    else:
        files = [p for p in base.rglob("*") if p.is_file() and _looks_text(p)]
    limit = max(1, min(int(args.get("limit", 50) or 50), 200))
    use_regex = bool(args.get("regex", False))
    pattern = re.compile(query) if use_regex else None
    matches = []
    for path in files:
        try:
            text = _read_text(path)
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), 1):
            hit = bool(pattern.search(line)) if pattern else query.lower() in line.lower()
            if not hit:
                continue
            matches.append({"path": _rel_to_root(path), "line": idx, "text": line[:500]})
            if len(matches) >= limit:
                return {"ok": True, "matches": matches, "truncated": True}
    return {"ok": True, "matches": matches, "truncated": False}


def _handle_fetch(workspace_dir: str, args: dict) -> dict:
    if not workspace_dir:
        return {"ok": False, "error": "workspace is required for env_fetch"}
    requested_path = str(args.get("path") or "")
    src = _resolve_env_path(requested_path, must_exist=None)
    if not src.exists():
        candidates = _env_path_candidates(requested_path)
        return {
            "ok": False,
            "error": (
                "Path not found. Use one exact project-relative path from candidates or inspect the manifest again.\n\n"
                "路径不存在；从候选或清单中选择精确项目相对路径。"
            ),
            "path": requested_path,
            "candidates": candidates,
            "next_action": (
                "retry env_fetch with one exact candidate path, or read _env/.resource_manifest.json for "
                "source-of-truth paths.\n\n"
                "用精确候选路径重试，或读取资源清单确认真实路径。"
            ),
        }
    if not src.is_file():
        return {"ok": False, "error": "env_fetch only supports files", "path": _rel_to_root(src)}
    size = src.stat().st_size
    if size > MAX_FETCH_BYTES:
        suffix = src.suffix.lower()
        category = _resource_category(src)
        result = {
            "ok": False,
            "error": f"file too large to fetch ({size} bytes)",
            "path": _rel_to_root(src),
            "size": size,
            "max_bytes": MAX_FETCH_BYTES,
            "category": category,
            "next_action": (
                "Use env_run for metadata or targeted extraction, or delegate a read helper with the exact project_path from env_inventory; the helper can use OCR/Office extraction tools when relevant. "
                "If only a small excerpt is needed, run a bounded project-side extraction command and save a "
                "small evidence file before reading it.\n\n"
                "大文件先用 env_run 做元数据或小片段抽取，完整内容交给 read helper 分批处理；需要时使用 OCR/Office 证据工具。"
            ),
            "suggested_helper_kind": "read",
            "suggested_project_path": _rel_to_root(src),
        }
        if suffix == ".pdf":
            result["suggested_actions"] = [
                "Use env_run to inspect PDF page count/metadata without copying the whole file.",
                "Delegate a read helper with this exact project_path for selected pages or summarized extraction, using OCR when needed.",
                "For full OCR, ask the helper to batch pages and write chunked evidence summaries instead of moving the source PDF into _env.",
            ]
        elif suffix in {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}:
            result["suggested_actions"] = [
                "Use env_run or an Office/read helper to extract bounded metadata or selected body text in chunks.",
                "Delegate full content extraction to a read helper and ask for coverage summaries plus evidence files.",
            ]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
            result["suggested_actions"] = [
                "Use env_run for image dimensions/metadata.",
                "Delegate OCR/vision reading with the exact project_path and request chunked evidence.",
            ]
        return result
    rel = _rel_to_root(src)
    registry = _environment_file_registry(workspace_dir)
    if registry is not None:
        record = stage_project_file(registry, rel)
        workspace_path = record.workspace_path
        digest = record.sha256
        size = int(record.size or size)
    else:
        dst = _workspace_env_path(workspace_dir, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        digest = _sha256(src)
        workspace_path = str(dst.relative_to(Path(workspace_dir).resolve())).replace("\\", "/")
    manifest = _load_manifest(workspace_dir)
    manifest["files"][rel] = {
        "source_path": rel,
        "workspace_path": workspace_path,
        "sha256": digest,
        "size": size,
        "fetched_at": time.time(),
        "source": "file_registry" if registry is not None else "legacy_fetch",
    }
    _save_manifest(workspace_dir, manifest)
    return {"ok": True, "path": rel, "workspace_path": manifest["files"][rel]["workspace_path"], "sha256": digest, "size": size}


def _workspace_copy_path(workspace_dir: str, rel_path: str, workspace_path: str = "") -> Path:
    if workspace_path:
        from app.llm.tools.workspace_paths import _safe_resolve
        return Path(_safe_resolve(workspace_dir, workspace_path))
    return _workspace_env_path(workspace_dir, rel_path)


def _handle_diff(workspace_dir: str, args: dict) -> dict:
    if not workspace_dir:
        return {"ok": False, "error": "workspace is required for env_diff"}
    src = _resolve_env_path(str(args.get("path") or ""), must_exist=True)
    if not src.is_file():
        return {"ok": False, "error": "source is not a file", "path": _rel_to_root(src)}
    rel = _rel_to_root(src)
    dst = _workspace_copy_path(workspace_dir, rel, str(args.get("workspace_path") or ""))
    if not dst.exists() or not dst.is_file():
        return {"ok": False, "error": "workspace copy not found", "workspace_path": str(dst)}
    if not _looks_text(src) or not _looks_text(dst):
        return {
            "ok": True,
            "path": rel,
            "binary": True,
            "source_sha256": _sha256(src),
            "workspace_sha256": _sha256(dst),
            "changed": _sha256(src) != _sha256(dst),
        }
    old = _read_text(src).splitlines()
    new = _read_text(dst).splitlines()
    diff = "\n".join(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
    max_chars = max(1000, min(int(args.get("max_chars", 30000) or 30000), 100000))
    return {
        "ok": True,
        "path": rel,
        "changed": old != new,
        "source_sha256": _sha256(src),
        "workspace_sha256": _sha256(dst),
        "diff": diff[:max_chars],
        "truncated": len(diff) > max_chars,
    }


def _handle_apply_replace(workspace_dir: str, args: dict) -> dict:
    if not workspace_dir:
        return {"ok": False, "error": "workspace is required for env_apply_replace"}
    src = _resolve_env_path(str(args.get("path") or ""), must_exist=True)
    if not src.is_file():
        return {"ok": False, "error": "target is not a file", "path": _rel_to_root(src)}
    rel = _rel_to_root(src)
    expected = str(args.get("expected_hash") or "").strip().lower()
    current_hash = _sha256(src).lower()
    if not expected:
        return {
            "ok": False,
            "error": "expected_hash is required.\n\n需要提供 env_fetch 返回的 expected_hash。",
        }
    if expected in {"unknown", "todo", "placeholder", "sha256"}:
        return {
            "ok": False,
            "error": (
                "expected_hash must be copied exactly from env_fetch; call env_fetch for this path and reuse its sha256.\n\n"
                "expected_hash 必须精确使用 env_fetch 返回的 sha256。"
            ),
            "path": rel,
        }
    manifest = _load_manifest(workspace_dir)
    manifest_entry = (manifest.get("files") or {}).get(rel)
    if not isinstance(manifest_entry, dict) or str(manifest_entry.get("sha256", "")).lower() != expected:
        return {
            "ok": False,
            "error": (
                "env_apply_replace requires a matching env_fetch manifest entry; call env_fetch and copy its sha256 exactly.\n\n"
                "应用替换前必须先 env_fetch，并使用匹配清单中的 sha256。"
            ),
            "path": rel,
        }
    if expected != current_hash:
        return {
            "ok": False,
            "error": (
                "The source file changed since env_fetch. Re-read the current source, merge the staged changes, "
                "then apply with the new hash.\n\n"
                "源文件在 fetch 后变化；先重读并合并，再用新 hash 应用。"
            ),
            "expected_hash": expected,
            "current_hash": current_hash,
        }
    workspace_path_arg = str(args.get("workspace_path") or "")
    provenance_guard = _env_apply_provenance_guard(
        workspace_dir,
        workspace_path=workspace_path_arg or f"_env/{rel}",
        target_path=rel,
        replacing=True,
    )
    if provenance_guard is not None:
        return provenance_guard
    dst = _workspace_copy_path(workspace_dir, rel, workspace_path_arg)
    if not dst.exists() or not dst.is_file():
        return {"ok": False, "error": "workspace copy not found", "workspace_path": str(dst)}
    backup_dir = Path(workspace_dir).resolve() / "_env" / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (rel.replace("/", "__").replace("\\", "__") + f".{int(time.time())}.bak")
    shutil.copy2(src, backup)
    shutil.copy2(dst, src)
    new_hash = _sha256(src)
    manifest["files"][rel] = {
        **manifest_entry,
        "sha256": new_hash,
        "applied_at": time.time(),
    }
    _save_manifest(workspace_dir, manifest)
    _record_registry_apply(
        workspace_dir,
        project_path=rel,
        workspace_path=workspace_path_arg or f"_env/{rel}",
        replacing=True,
        sha256=new_hash,
    )
    return {
        "ok": True,
        "path": rel,
        "new_sha256": new_hash,
        "backup_workspace_path": str(backup.relative_to(Path(workspace_dir).resolve())).replace("\\", "/"),
        "backup_project_path": None,
    }


def _handle_apply_create(workspace_dir: str, args: dict) -> dict:
    if not workspace_dir:
        return {"ok": False, "error": "workspace is required for env_apply_create"}
    raw_path = str(args.get("path") or "")
    if _looks_like_directory_create_target(raw_path):
        return {
            "ok": False,
            "error": "env_apply_create_requires_file_target",
            "error_kind": "env_apply_create_requires_file_target",
            "path": raw_path,
            "hint": (
                "env_apply_create creates files, not empty project directories. Choose the first concrete project file "
                "that should exist under this directory and create it; parent directories are created automatically. "
                "Use env_run only when an empty directory itself is the requested artifact.\n\n"
                "env_apply_create 用于创建文件；请选择该目录下的具体文件，父目录会自动创建。"
            ),
        }
    target = _resolve_env_path(raw_path, must_exist=False)
    if target.exists():
        return {"ok": False, "error": "target already exists", "path": _rel_to_root(target)}
    workspace_path = str(args.get("workspace_path") or "")
    target.parent.mkdir(parents=True, exist_ok=True)
    if workspace_path:
        provenance_guard = _env_apply_provenance_guard(
            workspace_dir,
            workspace_path=workspace_path,
            target_path=_rel_to_root(target),
            replacing=False,
            require_ready=True,
        )
        if provenance_guard is not None:
            return provenance_guard
        source = _workspace_copy_path(workspace_dir, target.name, workspace_path)
        if not source.exists() or not source.is_file():
            return {"ok": False, "error": "workspace source not found", "workspace_path": workspace_path}
        shutil.copy2(source, target)
        source_kind = "workspace_path"
    else:
        if "content" not in args:
            return {"ok": False, "error": "workspace_path or content is required"}
        content = str(args.get("content") or "")
        rel_path = _rel_to_root(target)
        if _should_delegate_main_env_create(rel_path, content):
            return _env_create_source_delegation_hint(rel_path, len(content))
        if _should_delegate_main_project_text_create(rel_path, content):
            return _env_create_project_text_delegation_hint(rel_path, len(content))
        target.write_text(content, encoding="utf-8")
        source_kind = "content"
    rel_created = _rel_to_root(target)
    created_hash = _sha256(target)
    _record_registry_apply(
        workspace_dir,
        project_path=rel_created,
        workspace_path=workspace_path,
        replacing=False,
        sha256=created_hash,
    )
    return {"ok": True, "path": rel_created, "sha256": created_hash, "source": source_kind}


def _augment_pytest_command(command: str, cwd: Path) -> str:
    needle = "python -m pytest"
    lower = command.lower()
    idx = lower.find(needle)
    if idx < 0:
        return command
    pytest_flags: list[str] = []
    if "--rootdir" not in lower:
        pytest_flags.append("--rootdir=.")
    if " -c " not in lower and " --config-file" not in lower:
        project_config = next(
            (name for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
             if (cwd / name).is_file()),
            None,
        )
        if project_config:
            pytest_flags.append(f"-c {shlex.quote(project_config)}")
        else:
            empty_config = cwd / ".env_pytest_empty.ini"
            if not empty_config.exists():
                empty_config.write_text("[pytest]\n", encoding="utf-8")
            config_path = str(empty_config)
            if sys.platform == "win32":
                config_path = '"' + config_path.replace('"', '\\"') + '"'
            else:
                config_path = shlex.quote(config_path)
            pytest_flags.append(f"-c {config_path}")
    if not pytest_flags:
        return command
    insert_at = idx + len(needle)
    return command[:insert_at] + " " + " ".join(pytest_flags) + command[insert_at:]


class _EnvCommandNormalization(tuple):
    """Tuple-compatible env_run normalization result.

    The public helper historically unpacked as three values. env_run itself
    also needs cleanup paths for generated Windows probe scripts, so expose that
    as an attribute without changing the visible unpacking contract.

    env_run 内部可读取 cleanup_paths；外部旧调用仍按三元组解包。
    """

    cleanup_paths: list[Path]

    def __new__(
        cls,
        command: str,
        script_name: str,
        script_path: Path | None,
        cleanup_paths: list[Path] | None = None,
    ):
        obj = super().__new__(cls, (command, script_name, script_path))
        obj.cleanup_paths = list(cleanup_paths or [])
        return obj


def _normalize_env_command(command: str, cwd: Path) -> _EnvCommandNormalization:
    """Reuse workspace command normalization without polluting the project tree."""
    cleanup_paths: list[Path] = []
    if sys.platform != "win32":
        return _EnvCommandNormalization(command, "", None, cleanup_paths)
    # Keep complex shell commands in shell form. The workspace translator is
    # useful for plain `python -c "..."`, but a command such as
    # `python -c "..." 2>&1 | findstr ...` must keep its pipe/redirection suffix.
    # env_run already offers `python_code` for portable probes.
    if re.search(r"\bpython(?:\.exe)?\s+(?:-[A-Za-z0-9]+\s+)*-c\s+", command, re.IGNORECASE) and any(
        op in command for op in ("|", ">", "<", "&&", "||")
    ):
        return _EnvCommandNormalization(command, "", None, cleanup_paths)
    translated = _translate_windows_command(command, str(cwd))
    if translated == command:
        return _EnvCommandNormalization(command, "", None, cleanup_paths)
    script_name = ""
    script_path: Path | None = None
    try:
        for part in shlex.split(translated, posix=False):
            cleaned = part.strip('"')
            if Path(cleaned).name.startswith("_py_cmd_") and cleaned.endswith(".py"):
                script_name = Path(cleaned).name
                candidate = cwd / script_name
                if candidate.exists():
                    cleanup_paths.append(candidate)
                break
    except ValueError:
        pass
    # The Windows python -c translator may materialize short probe scripts in
    # the project cwd before env_run moves them to a system temp file. Track all
    # such generated scripts so interrupted or malformed commands cannot pollute
    # the environment project and skew later file inventories.
    try:
        for candidate in cwd.glob("_py_cmd_*.py"):
            if candidate not in cleanup_paths:
                cleanup_paths.append(candidate)
    except OSError:
        pass
    if script_name:
        project_script = cwd / script_name
        if project_script.exists():
            original_code = project_script.read_text(encoding="utf-8", errors="replace")
            tmp = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=".py",
                prefix="env_run_cmd_",
                delete=False,
            )
            try:
                tmp.write(
                    "import sys as _env_run_sys\n"
                    "_env_run_sys.path[0] = ''\n"
                    "del _env_run_sys\n"
                )
                tmp.write(original_code)
            finally:
                tmp.close()
            script_path = Path(tmp.name)
            try:
                project_script.unlink(missing_ok=True)
            except OSError:
                pass
            quoted_tmp = str(script_path).replace('"', r'\"')
            translated = translated.replace(script_name, f'"{quoted_tmp}"', 1)
            script_name = str(script_path)
    return _EnvCommandNormalization(translated, script_name, script_path, cleanup_paths)


def _python_tool_site_paths(project_root: Path) -> list[str]:
    """Return package paths needed by the tool runner without exposing app imports."""
    candidates: list[Path] = []
    try:
        candidates.extend(Path(p) for p in site.getsitepackages())
    except Exception:
        pass
    candidates.extend(Path(p) for p in sys.path if p and "site-packages" in p.lower())
    candidates.append(project_root / ".venv" / "Lib" / "site-packages")

    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen or not Path(resolved).is_dir():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _append_tool_pythonpath_to_command(command: str, tool_pythonpath: str) -> str:
    """Preserve explicit project PYTHONPATH while keeping tool packages importable."""
    if not tool_pythonpath:
        return command
    if sys.platform == "win32":
        pat = re.compile(
            r'(?P<prefix>\bset\s+)(?P<quote>"?)(?P<name>PYTHONPATH)=(?P<value>[^"&|<>]*?)(?P=quote)\s*&&',
            re.IGNORECASE,
        )

        def repl(match: re.Match) -> str:
            value = match.group("value").strip()
            if tool_pythonpath in value:
                return match.group(0)
            merged = value + (";" if value else "") + tool_pythonpath
            return f'set "PYTHONPATH={merged}" &&'

        return pat.sub(repl, command, count=1)

    pat = re.compile(r'(?P<name>PYTHONPATH)=(?P<value>[^\s&|<>]+)')

    def repl(match: re.Match) -> str:
        value = match.group("value")
        if tool_pythonpath in value:
            return match.group(0)
        return f"PYTHONPATH={value}:{tool_pythonpath}"

    return pat.sub(repl, command, count=1)


async def _cancel_and_drain_task(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except BaseException:
        pass


def _close_subprocess_transport(proc: asyncio.subprocess.Process) -> None:
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:
        pass


async def _handle_run(args: dict) -> dict:
    env_ctx = current_environment()
    if env_ctx is None:
        return {"ok": False, "error": "environment context is required"}
    command = str(args.get("command") or "").strip()
    python_code = str(args.get("python_code") or "")
    if command and python_code.strip():
        return {
            "ok": False,
            "error": "Provide either command or python_code, not both.\n\ncommand 与 python_code 二选一。",
            "FIX_HINT": _env_run_usage_hint("both_command_and_python_code"),
        }
    if not command and not python_code.strip():
        return {
            "ok": False,
            "error": "command or python_code is required.\n\n必须提供 command 或 python_code。",
            "FIX_HINT": _env_run_usage_hint("missing_execution_body"),
        }
    delegated_source_guard = _env_run_source_material_guard(command, python_code)
    if delegated_source_guard is not None:
        return delegated_source_guard
    cwd = _resolve_env_path(str(args.get("cwd") or "."), must_exist=True)
    if not cwd.is_dir():
        return {"ok": False, "error": "cwd is not a directory", "cwd": _rel_to_root(cwd)}
    timeout_sec = max(1, min(int(args.get("timeout_sec", 30) or 30), 1800))
    temp_script_path: Path | None = None
    if python_code.strip():
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".py",
            prefix="env_run_",
            delete=False,
        )
        try:
            tmp.write(python_code)
            if not python_code.endswith("\n"):
                tmp.write("\n")
        finally:
            tmp.close()
        temp_script_path = Path(tmp.name)
        command = f'"{sys.executable}" -X utf8 "{temp_script_path}"'
    decision = analyze_command(command, str(cwd), is_main_thread=True)
    if not decision.allowed:
        if temp_script_path:
            temp_script_path.unlink(missing_ok=True)
        return {"ok": False, "error": decision.reason, "category": decision.category}
    env = os.environ.copy()
    # Isolate project commands from the backend process import path. Project-specific
    # imports should come from cwd or an explicit PYTHONPATH in the command.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["NVIDIA_VISIBLE_DEVICES"] = "none"
    project_root = Path(__file__).resolve().parents[3]
    toolchain_bins = [
        Path(sys.executable).resolve().parent,
        project_root / "mingw64" / "bin",
    ]
    existing_path = env.get("PATH", "")
    extra_path = [
        str(path)
        for path in toolchain_bins
        if path.exists() and str(path) not in existing_path
    ]
    if extra_path:
        env["PATH"] = os.pathsep.join(extra_path + [existing_path])
    tool_pythonpath = os.pathsep.join(_python_tool_site_paths(project_root))
    if tool_pythonpath:
        env["PYTHONPATH"] = tool_pythonpath
    if "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in env:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    command = _augment_pytest_command(command, cwd)
    command = _append_tool_pythonpath_to_command(command, tool_pythonpath)
    normalized_from = ""
    normalized_temp_script: Path | None = None
    _normalized_command = _normalize_env_command(command, cwd)
    command, normalized_from, normalized_temp_script = _normalized_command
    normalized_cleanup_paths = _normalized_command.cleanup_paths
    _memory_preflight = preflight_memory_check(command)
    if _memory_preflight is not None:
        if temp_script_path:
            try:
                temp_script_path.unlink(missing_ok=True)
            except OSError:
                pass
        if normalized_temp_script:
            try:
                normalized_temp_script.unlink(missing_ok=True)
            except OSError:
                pass
        for probe_script in normalized_cleanup_paths:
            try:
                probe_script.unlink(missing_ok=True)
            except OSError:
                pass
        return _memory_preflight
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    preexec_fn = os.setsid if sys.platform != "win32" else None
    trace_id = debug.current_trace_id() or ""
    command_id = await env_monitor.register_command(
        trace_id=trace_id,
        archive_id=env_ctx.archive_id,
        group_id=env_ctx.group_id,
        user_id=env_ctx.user_id,
        root_dir=env_ctx.root_dir,
        cwd=str(cwd),
        command=command,
    )
    emit_environment_event("command", {
        "kind": "start",
        "trace_id": trace_id,
        "command_id": command_id,
        "command": command,
        "cwd": str(cwd),
        "timeout_sec": timeout_sec,
    })
    start = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )
    await env_monitor.attach_process(command_id, proc, proc.pid)
    _limit_bytes, _min_available_bytes = workspace_memory_limits()

    def _memory_guard_kill_tree(pid: int, _proc=proc) -> None:
        _kill_process_tree(pid, proc_obj=_proc)

    memory_guard = WorkspaceMemoryGuard(
        pid=proc.pid,
        proc_id=f"env:{command_id}",
        command=command,
        kill_tree=_memory_guard_kill_tree,
        limit_bytes=_limit_bytes,
        min_available_bytes=_min_available_bytes,
        scope="env_run",
    ).start()
    communicate_task = asyncio.create_task(proc.communicate())
    abort_task = asyncio.create_task(env_monitor.wait_abort(command_id))
    try:
        done, pending = await asyncio.wait(
            {communicate_task, abort_task},
            timeout=timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate_task in done:
            stdout_b, stderr_b = communicate_task.result()
            timed_out = False
            aborted = False
            status = "completed"
        else:
            timed_out = not abort_task.done()
            aborted = abort_task.done()
            status = "aborted" if aborted else "timeout"
            try:
                _kill_process_tree(proc.pid, proc_obj=proc)
            except (ProcessLookupError, OSError):
                pass
            try:
                stdout_b, stderr_b = await asyncio.wait_for(communicate_task, timeout=3.0)
            except Exception:
                communicate_task.cancel()
                stdout_b = b""
                stderr_b = b""
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                pass
    finally:
        await memory_guard.stop()
        await _cancel_and_drain_task(abort_task)
        if communicate_task.cancelled():
            await _cancel_and_drain_task(communicate_task)
        _close_subprocess_transport(proc)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if memory_guard.triggered:
        status = "memory_limit"
        timed_out = False
        result = memory_limit_error(memory_guard.triggered, stdout=stdout, stderr=stderr)
        result.update({
            "command_id": command_id,
            "command": command,
            "cwd": _rel_to_root(cwd),
            "returncode": proc.returncode,
            "timed_out": False,
            "aborted": False,
            "elapsed_sec": round(time.monotonic() - start, 3),
            "stdout": stdout[:30000],
            "stderr": stderr[:20000],
            "truncated": len(stdout) > 30000 or len(stderr) > 20000,
        })
    else:
        result = {
            "ok": (proc.returncode == 0 and not timed_out and not (status == "aborted")),
            "command_id": command_id,
            "command": command,
            "cwd": _rel_to_root(cwd),
            "returncode": proc.returncode,
            "timed_out": timed_out,
            "aborted": status == "aborted",
            "elapsed_sec": round(time.monotonic() - start, 3),
            "stdout": stdout[:30000],
            "stderr": stderr[:20000],
            "truncated": len(stdout) > 30000 or len(stderr) > 20000,
        }
        fix_hint = _env_run_fix_hint(
            command,
            stdout,
            stderr,
            python_code_used=bool(temp_script_path),
            source_text=python_code if temp_script_path else "",
        )
        if fix_hint:
            result["FIX_HINT"] = fix_hint
    if normalized_from:
        result["normalized_from"] = "python -c"
        result["script_path"] = normalized_from
        result["script_location"] = "system_temp" if normalized_temp_script else "project_cwd"
        result["normalization_note"] = "env_run normalized python -c for Windows quoting/newline safety"
    if result["ok"] and not stdout.strip() and not stderr.strip():
        result["empty_output_warning"] = (
            "Command exited successfully but produced no stdout/stderr. "
            "Rerun with explicit print/output or inspect another reliable source before inferring requested data from this command.\n\n"
            "命令无输出；需要显式打印或换证据来源。"
        )
    if normalized_temp_script:
        try:
            normalized_temp_script.unlink(missing_ok=True)
            result["script_deleted"] = not normalized_temp_script.exists()
        except OSError as e:
            result["script_delete_error"] = str(e)
    elif normalized_from:
        try:
            (cwd / normalized_from).unlink(missing_ok=True)
        except OSError:
            pass
    if normalized_cleanup_paths:
        removed_probe_scripts = 0
        cleanup_errors: list[str] = []
        for probe_script in normalized_cleanup_paths:
            try:
                if probe_script.exists():
                    probe_script.unlink(missing_ok=True)
                    removed_probe_scripts += 1
            except OSError as e:
                cleanup_errors.append(f"{probe_script.name}: {e}")
        if removed_probe_scripts:
            result["normalized_probe_scripts_deleted"] = removed_probe_scripts
        if cleanup_errors:
            result["normalized_probe_cleanup_errors"] = cleanup_errors[:5]
    if temp_script_path:
        result["python_code"] = True
        result["script_location"] = "system_temp"
        result["script_deleted"] = False
        try:
            temp_script_path.unlink(missing_ok=True)
            result["script_deleted"] = not temp_script_path.exists()
        except OSError as e:
            result["script_delete_error"] = str(e)
    await env_monitor.finish_command(
        command_id,
        status=status,
        returncode=proc.returncode,
        timed_out=timed_out,
    )
    emit_environment_event("command", {
        "kind": "done",
        "trace_id": trace_id,
        "command_id": command_id,
        "command": command,
        "ok": result["ok"],
        "elapsed_sec": result["elapsed_sec"],
    })
    return result
