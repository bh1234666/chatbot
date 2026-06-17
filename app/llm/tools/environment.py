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
from app.llm.tools.output_spill import spill_text_field, write_tool_output_spill
from app.llm.tools.environment_background import ENV_BACKGROUND_SCHEMA, handle_background_tool
from app.llm.tools.process_utils import _kill_process_tree
from app.llm.tools.result_budget import apply_result_budget
from app.llm.tools.workspace import _translate_windows_command


TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".css", ".scss", ".html", ".xml", ".csv", ".sql",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".go", ".rs", ".cs", ".php",
    ".rb", ".sh", ".ps1", ".bat", ".cmd", ".log", ".dockerfile", ".gitignore",
}
MAX_READ_BYTES = 512 * 1024
ENV_READ_DEFAULT_MAX_CHARS = 20000
ENV_READ_ABSOLUTE_MAX_CHARS = 30000
ENV_RUN_STDOUT_VISIBLE_CHARS = 30000
ENV_RUN_STDERR_VISIBLE_CHARS = 20000
MAX_FETCH_BYTES = 5 * 1024 * 1024
MAX_LIST_ITEMS = 500

SOURCE_PROJECT_EXTS = {
    ".py", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
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


def _project_apply_acceptance_fact(action: str, path: str, *, helper_owned: bool) -> dict:
    owner_fact = (
        "The applied content came from a staged workspace/helper-owned source; content quality remains at the producer boundary."
        if helper_owned
        else "The applied content came from direct main-thread content; the main thread owns that narrow content change."
    )
    return {
        "kind": "project_apply_acceptance_fact",
        "action": action,
        "path": path,
        "helper_owned": helper_owned,
        "fact": (
            f"Fact: {action} changed the project state for `{path}`. {owner_fact} "
            "If the active task has an external verifier, test command, build command, or acceptance check that reads project state, "
            "that check covers the project state at the time it ran; final coverage should correspond to the final intended project state "
            "after all planned apply/create/replace operations. "
            "A check that ran before a later project apply covers the earlier state, not the later applied state."
        ),
        "summary_zh": (
            "项目状态已变化；helper 来源内容仍按生产者边界信任。若验收命令读取项目状态，覆盖范围应对应全部计划 apply 后的最终状态；"
            "早于后续 apply 的检查只覆盖旧状态。"
            if helper_owned
            else "项目状态已变化；主线程直接写入的内容由主线程承担该窄改动。若验收命令读取项目状态，覆盖范围应对应全部计划 apply 后的最终状态。"
        ),
    }


def _staged_apply_source_is_helper_owned(workspace_dir: str, workspace_path: str) -> bool:
    norm = str(workspace_path or "").replace("\\", "/").strip().lstrip("./")
    if not workspace_dir or not norm.startswith("_env/"):
        return False
    env_rel = norm[len("_env/"):]
    data = _load_env_provenance(workspace_dir)
    entry = (data.get("files") or {}).get(env_rel)
    if isinstance(entry, dict) and (entry.get("task_id") or entry.get("kind") or entry.get("terminal_reason")):
        return True
    registry = _environment_file_registry(workspace_dir)
    if registry is None:
        return False
    record = registry.find_by_workspace_path(norm)
    if record is None:
        return False
    return bool(record.owner_task_id or record.helper_kind)


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


def _blocked_create_candidate_fact(workspace_dir: str, *, target_rel: str, content: str) -> dict:
    """Save blocked main-authored create content as workspace evidence."""
    if not workspace_dir or not content:
        return {}
    try:
        workspace_root = Path(workspace_dir).resolve()
        safe_rel = target_rel.replace("\\", "/").lstrip("/").replace("..", "__")
        candidate = workspace_root / "_env" / ".blocked_creates" / safe_rel
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        candidate_workspace_path = str(candidate.relative_to(workspace_root)).replace("\\", "/")
        return {
            "candidate_preserved": True,
            "candidate_workspace_path": candidate_workspace_path,
            "candidate_sha256": _sha256(candidate),
            "candidate_size": candidate.stat().st_size,
            "candidate_preservation_fact": (
                f"Fact: the direct project create was blocked, and the already-supplied candidate content was "
                f"preserved at `{candidate_workspace_path}`. The real project file was not changed. This "
                "workspace candidate can be inspected, passed to a helper for revision, or reused by path in a "
                "later tool call when the active task boundary allows it. Avoid pasting the same long body into "
                "another tool call."
            ),
            "candidate_handoff_fact": (
                f"Fact: target project file `{target_rel}` is absent and candidate `{candidate_workspace_path}` exists. "
                "The candidate is preserved evidence from a blocked main-process create, not a clean producer-owned "
                "output by itself. Use it as input to a producer/verify helper, or apply a later staged file that has "
                "ready/verified provenance."
            ),
            "summary_zh": "被拦截的大段创建内容已保存为工作区候选文件；真实项目未改变，可作为 helper 或后续操作的输入证据。",
        }
    except OSError:
        return {}


def _env_create_source_delegation_hint(path: str, content_len: int, *, candidate: dict | None = None) -> dict:
    candidate = candidate or {}
    input_files = [candidate["candidate_workspace_path"]] if candidate.get("candidate_workspace_path") else []
    result = {
        "ok": False,
        "error": "main_thread_source_create_should_delegate",
        "error_kind": "main_thread_source_create_should_delegate",
        "path": path,
        "content_chars": content_len,
        "hint": (
            "This is a substantial new project source/script file being authored directly by the main process. "
            "No project file was written. The target remains absent, and any preserved candidate path is reported "
            "as evidence. A focused code helper with expected `_env/...` output and acceptance checks is the normal "
            "recovery shape for source authoring, but the next action remains a model decision from the active task.\n\n"
            "事实：本次未写入大段新源码；目标仍不存在，候选内容如有保存可作为输入证据，后续动作由模型基于任务决定。"
        ),
        "recovery_facts": {
            "matching_helper_kind": "code",
            "mode": "easy",
            "framework_fact": "<shared interface/schema/outline and ownership contract>",
            "input_files": input_files,
            "helper_prompt_fact": f"Create or update the focused project slice that includes {path}. If a candidate input file is listed, inspect it as prior draft/evidence instead of asking the main process to paste the body again.",
            "expected_outputs": [f"_env/{path}"],
            "acceptance_checks": ["read/inspect the produced file", "run the relevant local check"],
        },
    }
    result.update(candidate)
    return result


def _env_create_project_text_delegation_hint(path: str, content_len: int, *, candidate: dict | None = None) -> dict:
    candidate = candidate or {}
    input_files = [candidate["candidate_workspace_path"]] if candidate.get("candidate_workspace_path") else []
    result = {
        "ok": False,
        "error": "main_thread_project_artifact_create_should_delegate",
        "error_kind": "main_thread_project_artifact_create_should_delegate",
        "path": path,
        "content_chars": content_len,
        "hint": (
            "This is a substantial new project framework, contract, report, data, or documentation artifact being "
            "authored directly by the main process. No project file was written. The attempted path, size, and "
            "preserved candidate path are returned as facts. A focused edit helper with expected `_env/...` output "
            "is available when the candidate needs revision. If current evidence says the preserved candidate already "
            "contains useful material, pass it as helper input or apply a later staged output with ready/verified "
            "provenance rather than treating the blocked main-process draft as a clean producer output.\n\n"
            "事实：本次未写入项目侧框架、报告、数据或文档；返回路径、大小和候选文件事实，后续是否派发或应用由模型决定。"
        ),
        "recovery_facts": {
            "matching_helper_kind": "edit",
            "mode": "easy",
            "framework_fact": "<purpose, structure, source evidence, ownership, and acceptance checks>",
            "input_files": input_files,
            "helper_prompt_fact": f"Create the focused project artifact {path} from the provided framework, evidence, and any candidate input file. Keep the delegate prompt compact; do not paste long draft bodies.",
            "expected_outputs": [f"_env/{path}"],
            "acceptance_checks": ["inspect the produced file", "verify requested sections and source coverage"],
        },
    }
    result.update(candidate)
    return result


def _env_direct_content_create_guard(path: str, content: str, *, workspace_dir: str = "") -> dict | None:
    """Block substantial main-process direct-content project creates."""
    rel = (path or "").replace("\\", "/").strip().lstrip("./")
    content_text = content if isinstance(content, str) else str(content)
    content_len = len(content_text)
    line_count = content_text.count("\n") + (1 if content_text else 0)
    candidate = _blocked_create_candidate_fact(workspace_dir, target_rel=rel, content=content_text)

    if _should_delegate_main_env_create(rel, content_text):
        result = _env_create_source_delegation_hint(rel, content_len, candidate=candidate)
        result["content_lines"] = line_count
        return result
    if _should_delegate_main_project_text_create(rel, content_text):
        result = _env_create_project_text_delegation_hint(rel, content_len, candidate=candidate)
        result["content_lines"] = line_count
        return result
    return None


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
_TARGETED_OUTPUT_VALIDATION_RE = re.compile(
    r"\b(?:verify|validation|validate|inspect|check|spot[-_ ]?check|headings?|tables?|"
    r"image_count|paragraph_count|forbidden|path hygiene|section coverage|expected sections?)\b|"
    r"(?:验证|验收|检查|抽查|标题|表格|段落|路径|章节|卫生)",
    re.IGNORECASE,
)
_SINGLE_LITERAL_SOURCE_MATERIAL_RE = re.compile(
    r"(?P<quote>['\"])(?P<path>[^'\"]+\.(?:docx|doc|pdf|pptx|ppt|xlsx|xls|png|jpe?g|webp|bmp|gif|tiff?))(?P=quote)",
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
    literal_paths = {
        m.group("path").replace("\\", "/")
        for m in _SINGLE_LITERAL_SOURCE_MATERIAL_RE.finditer(text)
    }
    single_literal_source = len(literal_paths) == 1
    targeted_validation = (
        (len(ext_hits) <= 2 or single_literal_source)
        and not traversal
        and bool(_TARGETED_OUTPUT_VALIDATION_RE.search(text))
    )
    if targeted_validation:
        return None
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
            "env_run remains suitable for orientation, statistics, validation, and targeted spot checks. For "
            "content coverage, the current facts are: broad extraction was not executed, the source-material "
            "boundary was detected from the attempted command/code, and read helpers can own compact cited "
            "evidence split by natural source group, file batch, page range, or image range.\n\n"
            "事实：env_run 未执行批量材料正文抽取；返回边界事实，由模型决定是否按文件组、页段或图片段派发 read helper 并收集证据。"
        ),
        "recovery_facts": {
            "matching_helper_kind": "read",
            "env_run_scope": "orientation, statistics, validation, and targeted spot checks",
            "source_material_body_scope": "read helpers can extract compact cited evidence from assigned source groups",
            "possible_split_dimensions": ["natural source group", "file batch", "page range", "image range"],
            "acceptance_facts": ["assigned source files accounted for", "unread or failed files named with reasons"],
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
            "api", "architecture", "benchmark", "changelog", "change",
            "contract", "design", "doc", "docs", "framework", "guide",
            "instructions", "manual", "migration", "notes", "outline",
            "overview", "paper", "plan", "readme", "reference", "report",
            "requirements", "results", "schema", "spec", "tutorial", "usage",
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
            "api", "architecture", "benchmark", "changelog", "change",
            "contract", "doc", "docs", "framework", "guide",
            "instructions", "manual", "migration", "notes", "outline",
            "overview", "paper", "plan", "readme", "reference", "report",
            "requirements", "results", "schema", "spec", "tutorial", "usage",
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
    target_rel = str(target_path or "").replace("\\", "/").strip().lstrip("./")
    data = _load_env_provenance(workspace_dir)
    entry = (data.get("files") or {}).get(env_rel)
    entry_ready = isinstance(entry, dict) and str(entry.get("status") or "").lower() == "ready"
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
        if entry_ready:
            record.status = FileStatus.READY
            record.verified = True
            if isinstance(entry, dict):
                record.owner_task_id = str(entry.get("task_id") or record.owner_task_id or "")
                record.helper_kind = str(entry.get("kind") or record.helper_kind or "")
                record.metadata["terminal_reason"] = str(entry.get("terminal_reason") or "")
                record.metadata["outputs_complete"] = entry.get("outputs_complete")
            try:
                registry.add_or_update(record)
                registry.save()
            except Exception:
                pass
            return None
        task_id = record.owner_task_id
        terminal_reason = str(record.metadata.get("terminal_reason") or "")
        if isinstance(entry, dict):
            task_id = task_id or str(entry.get("task_id") or "")
            terminal_reason = terminal_reason or str(entry.get("terminal_reason") or "")
        return {
            "ok": False,
            "error": "staged_environment_file_not_ready",
            "error_kind": "staged_environment_file_not_ready",
            "path": target_path,
            "workspace_path": norm,
            "source_task_id": task_id,
            "source_status": str(record.status),
            "source_terminal_reason": terminal_reason,
            "hint": (
                "The staged `_env/...` file is registered, but the current registry/provenance facts do not mark it "
                "ready or verified. The model should decide from the current task state whether to inspect the staged "
                "file, collect/resume the same helper task, spawn a corrected helper, or report a blocker before "
                "applying it to the project.\n\n"
                "该暂存文件已登记但尚无 ready/verified 事实；需由模型结合任务状态决定检查、续作、重派或报告阻塞。"
            ),
            "recovery_facts": {
                "same_task_id": task_id,
                "helper_kind": record.helper_kind,
                "workspace_path": norm,
                "possible_actions": ["inspect staged file", "collect or resume same helper", "spawn corrected same-kind helper", "report blocker"],
            },
            "apply_type": "replace" if replacing else "create",
        }
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
                    "provenance record. Treat unknown staged files as unverified until project truth is checked. "
                    "The current facts are: the staged path exists in `_env`, no ready provenance was found, "
                    "and direct content creation or helper-owned staged output may be appropriate depending on "
                    "file size, risk, and task evidence.\n\n"
                    "事实：该 `_env` 暂存新文件缺少 ready 来源记录；模型需依据大小、风险和任务证据决定直接创建、检查、派发或报告阻塞。"
                ),
                "recovery_facts": {
                    "workspace_path": norm,
                    "target_path": target_path,
                    "source_status": "unknown",
                    "possible_actions": [
                        "inspect staged file",
                        "create compact low-risk file with direct content",
                        "spawn focused helper with declared expected_outputs",
                        "report blocker",
                    ],
                    "acceptance_facts": ["staged output inspected", "test or diff evidence checked when relevant"],
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
            "a clean acceptance step. Decide from the current task evidence whether to collect/read the helper result, "
            "inspect failure evidence, resume the same task_id, spawn a corrected same-kind helper, or report a blocker. "
            "After a clean helper completion or an explicit verification pass produces a ready staged file, apply that file.\n\n"
            "该暂存文件来自未干净完成的 helper；由模型依据事实决定读取、续作、重派或报告阻塞，再写入真实项目。"
        ),
        "recovery_facts": {
            "same_task_id": task_id,
            "helper_kind": entry.get("kind") or "",
            "workspace_path": norm,
            "possible_actions": ["read helper result", "inspect failure evidence", "resume same helper", "spawn corrected same-kind helper", "report blocker"],
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
    file_not_found = any(
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
    )
    mentions_env_staging = bool(re.search(r"(^|[\s\"'`=:/\\])_env[\\/]", attempted_source))
    mentions_absolute_source_path = bool(
        source_text
        and re.search(r"(?<![\w.-])(?:[A-Za-z]:[\\/][^\"'`\s]+|/[A-Za-z0-9_.-][^\"'`\s]*)", source_text)
    )
    if mentions_absolute_source_path and file_not_found:
        return (
            "The failed env_run script referenced an absolute filesystem path. env_run runs with cwd set to the real "
            "project directory, but absolute paths may point to a chat workspace, a stress-test run directory, a helper "
            "sandbox, or a stale location rather than the active project. Treat the failure as path evidence: for real "
            "project files, retry with project-relative paths from env_inventory/env_list_tree/env_search; for staged "
            "chat-workspace files, use read_file, inspect_file, office, or workspace tools; for external absolute paths, "
            "first verify that exact path exists before relying on it."
            "\n\n"
            "env_run 中的绝对路径可能指向聊天工作区、测试运行目录、helper 沙箱或过期位置；失败时先把路径当证据修正，"
            "真实项目文件用项目相对路径，暂存文件用 workspace/office/read 工具。"
        )
    if mentions_env_staging and file_not_found:
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


def _stream_text_facts(text: str) -> dict:
    line_count = 0 if text == "" else len(text.splitlines())
    trailing_blank_lines = 0
    if text:
        for line in reversed(text.splitlines()):
            if line == "":
                trailing_blank_lines += 1
            else:
                break
    crlf_count = text.count("\r\n")
    lone_lf_count = len(re.findall(r"(?<!\r)\n", text))
    lone_cr_count = len(re.findall(r"\r(?!\n)", text))
    result = {
        "chars": len(text),
        "bytes_utf8": len(text.encode("utf-8")),
        "line_count": line_count,
        "ends_with_newline": text.endswith(("\n", "\r")),
        "trailing_blank_lines": trailing_blank_lines,
        "newline_counts": {
            "crlf": crlf_count,
            "lf": lone_lf_count,
            "cr": lone_cr_count,
        },
    }
    if text and (crlf_count or lone_cr_count or trailing_blank_lines):
        result["tail_repr"] = repr(text[-240:])
    return result


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


def _env_run_empty_output_followup_fact(command: str, *, python_code_used: bool = False) -> str | None:
    if python_code_used:
        return None
    if not re.search(
        r"\b(?:python3|python|py)(?:\.(?:exe|cmd|bat))?\s+(?:-[A-Za-z0-9]+\s+)*-c\s+",
        command or "",
        re.IGNORECASE,
    ):
        return None
    return (
        "Fact: this empty-output command looks like a Python -c probe. For nontrivial or multiline Python "
        "inspection, env_run python_code avoids shell/shim quoting or routing ambiguity and executes with the "
        "same real project cwd.\n\n"
        "事实：本次空输出命令像 Python -c 探针；复杂或多行 Python 检查可改用 env_run 的 python_code，"
        "避免 shell/shim 引号或路由歧义。"
    )


def _extract_env_staged_path_tokens(text: str) -> list[str]:
    """Return distinct `_env/...` tokens mentioned in command/source text."""
    if not text:
        return []
    candidates: list[str] = []
    try:
        candidates.extend(shlex.split(text, posix=False))
    except ValueError:
        pass
    candidates.extend(re.findall(r"(?<![\w.-])_env[\\/][^\s\"'`|&<>),;]+", text))
    result: list[str] = []
    seen: set[str] = set()
    for token in candidates:
        clean = str(token or "").strip().strip("\"'`")
        match = re.search(r"(?<![\w.-])_env[\\/][^\s\"'`|&<>),;]+", clean)
        if not match:
            continue
        path = match.group(0).rstrip(".,;:")
        norm = path.replace("\\", "/")
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


def _env_run_staged_path_fact(
    command: str,
    *,
    project_cwd: Path,
    workspace_dir: str,
    python_code_used: bool = False,
    source_text: str = "",
) -> dict | None:
    """Expose facts when env_run command/source refers to chat-workspace `_env/...` paths."""
    paths = _extract_env_staged_path_tokens(f"{command or ''}\n{source_text or ''}")
    if not paths:
        return None
    workspace_root = Path(workspace_dir).resolve() if workspace_dir else None
    items: list[dict] = []
    for path in paths[:12]:
        project_path = (project_cwd / path).resolve()
        workspace_path = (workspace_root / path).resolve() if workspace_root is not None else None
        items.append({
            "path": path,
            "exists_in_env_run_project_cwd": project_path.exists(),
            "exists_in_chat_workspace": bool(workspace_path and workspace_path.exists()),
        })
    try:
        env_run_cwd = _rel_to_root(project_cwd)
    except Exception:
        env_run_cwd = str(project_cwd)
    return {
        "kind": "env_run_staged_path_fact",
        "python_code_used": bool(python_code_used),
        "env_run_cwd": env_run_cwd,
        "paths": items,
        "fact": (
            "env_run executes inside the real project cwd. Paths beginning with `_env/...` are interpreted relative "
            "to that project cwd during env_run; they are not automatically resolved from the chat workspace staging area."
        ),
        "事实": "env_run 在真实项目目录中执行；命令里的 `_env/...` 会按项目目录相对路径解释，不会自动指向聊天工作区暂存副本。",
    }


_PYTHON_LAUNCHERS = {
    "python", "python.exe", "python.cmd", "python.bat",
    "python3", "python3.exe", "python3.cmd", "python3.bat",
    "py", "py.exe", "py.cmd", "py.bat",
}
_DATA_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".csv", ".tsv", ".json", ".jsonl", ".parquet"}
_NON_PYTHON_SCRIPT_SUFFIXES = {
    ".cmd", ".bat", ".ps1", ".sh", ".bash",
    *_DATA_FILE_SUFFIXES,
}


def _env_run_python_data_file_fact(command: str, *, python_code_used: bool = False) -> dict | None:
    """Return a fact when a Python launcher is pointed at a data file."""
    if python_code_used or not command:
        return None
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return None
    if not parts:
        return None
    launcher = Path(parts[0].strip("\"'")).name.lower()
    if launcher not in _PYTHON_LAUNCHERS:
        return None
    script_arg = ""
    idx = 1
    while idx < len(parts):
        token = parts[idx].strip("\"'")
        if token in {"-c", "-m"}:
            return None
        if token.startswith("-"):
            idx += 2 if token in {"-X", "-W"} and idx + 1 < len(parts) else 1
            continue
        script_arg = token
        break
    if not script_arg:
        return None
    suffix = Path(script_arg).suffix.lower()
    if suffix not in _DATA_FILE_SUFFIXES:
        return None
    return {
        "kind": "python_launcher_data_file_fact",
        "data_argument": script_arg,
        "fact": (
            "A Python launcher treats its first non-option argument as a Python script path. "
            f"`{script_arg}` has data-file suffix `{suffix}`, so this command is data-file-as-script execution, "
            "not a SQLite/CSV/JSON data CLI invocation. For project data inspection, use env_run python_code "
            "with the relevant standard-library module or a separately verified project CLI."
        ),
        "事实": (
            "Python launcher 会把第一个非选项参数当脚本路径；该参数是数据文件后缀，"
            "这不是 SQLite/CSV/JSON CLI 调用。数据探测可改用 env_run python_code 或已验证的数据 CLI。"
        ),
    }


def _env_run_python_non_python_script_fact(command: str, *, python_code_used: bool = False) -> dict | None:
    """Return a fact when a Python launcher is asked to execute a non-Python script/data file."""
    if python_code_used or not command:
        return None
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return None
    if not parts:
        return None
    launcher = Path(parts[0].strip("\"'")).name.lower()
    if launcher not in _PYTHON_LAUNCHERS:
        return None
    script_arg = ""
    idx = 1
    while idx < len(parts):
        token = parts[idx].strip("\"'")
        if token in {"-c", "-m"}:
            return None
        if token.startswith("-"):
            idx += 2 if token in {"-X", "-W"} and idx + 1 < len(parts) else 1
            continue
        script_arg = token
        break
    if not script_arg:
        return None
    suffix = Path(script_arg).suffix.lower()
    if suffix not in _NON_PYTHON_SCRIPT_SUFFIXES:
        return None
    category = "data_file" if suffix in _DATA_FILE_SUFFIXES else "runner_or_shell_script"
    return {
        "kind": "python_launcher_non_python_script_fact",
        "launcher": launcher,
        "script_argument": script_arg,
        "script_suffix": suffix,
        "argument_category": category,
        "fact": (
            "A Python launcher treats its first non-option argument as Python source code. "
            f"In this command, `{script_arg}` is that first script argument and has suffix `{suffix}`, "
            "so Python will try to parse that file as Python source rather than execute it as a shell/runner command "
            "or inspect it as data."
        ),
        "事实": (
            "Python launcher 会把第一个非选项参数当 Python 源码文件；本次该参数不是 .py。"
            "这说明命令是在把非 Python 文件当 Python 源码解析，而不是运行 shell/runner 或读取数据。"
        ),
    }


_ACCEPTANCE_COMMAND_RE = re.compile(
    r"(?:^|[\/\s\"'])(?:verify|check|validate|grade|test|run[_-]?tests?)[\w.-]*\.(?:py|js|mjs|cjs|sh|ps1|bat|cmd)(?:$|[\s\"'])",
    re.IGNORECASE,
)

_ACCEPTANCE_MISSING_RE = re.compile(
    r"\b(?:workspace|project|file|content|artifact|output|required|expected|deliverable|summary)\b"
    r".{0,160}\b(?:missing|not found|absent|required|expected|contains no|no .* found|fail(?:ed)?)\b"
    r"|\bmissing\b.{0,120}\b(?:workspace|project|file|content|artifact|output|required|expected|deliverable|summary)\b"
    r"|\bno\b.{0,80}\b(?:file|artifact|output|summary|redacted|report)\b.{0,80}\bfound\b",
    re.IGNORECASE | re.DOTALL,
)


def _env_run_acceptance_failure_fact(command: str, stdout: str, stderr: str, returncode: int | None) -> dict | None:
    """Surface verifier failures as acceptance facts without choosing the repair path."""
    if returncode == 0:
        return None
    command_text = str(command or "")
    combined = f"{stdout or ''}\n{stderr or ''}".strip()
    if not combined or not _ACCEPTANCE_COMMAND_RE.search(command_text):
        return None
    if not _ACCEPTANCE_MISSING_RE.search(combined):
        return None
    return {
        "kind": "acceptance_failure_fact",
        "command": command_text[:300],
        "returncode": returncode,
        "observed_failure": combined[:500],
        "fact": (
            "A verifier/check command failed and its output reports missing workspace/project content or artifacts. "
            "This is current acceptance evidence; it is not proof that the task is complete."
        ),
        "事实": "验收/检查命令失败，输出指出工作区/项目内容或产物缺失；这是当前验收未满足的证据。",
        "available_recovery_paths": [
            "create or repair the user-facing artifact/content that the verifier searches",
            "rerun the verifier after the artifact/content exists",
            "report partial completion only if the active contract truly does not require that verifier",
        ],
    }


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
        "description": (
            "Read a text file or known line range from the project directory. Read-only. Large files return capped content; broad coverage and full-file understanding are helper-suitable. "
            "For coding/debugging, when env_inventory/env_list_tree identifies likely source or test paths, env_search path facts and input_files with acceptance_checks are usually enough for a code helper to do its own reading, diagnosis, edits, and tests. "
            "A batch of main-thread env_read calls over likely source/test files before delegation usually duplicates helper-owned reading and increases coordinator context. "
            "Use env_read mainly for one missing routing or acceptance fact, small spot checks, final acceptance/accounting facts, or deliberate main-thread analysis.\n"
            "读取项目文本文件或行范围；代码调试通常可把路径和验收项交给 code helper，主进程只在缺少路由事实、局部主责事实或验收记账事实时读取源码正文；批量读源码会重复 helper 工作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path.\n项目相对文件路径。"},
                "start_line": {"type": "integer", "description": "Starting line. Defaults to 1.\n起始行号。", "default": 1},
                "end_line": {"type": "integer", "description": "Ending line. Defaults to the truncation limit.\n结束行号。", "default": -1},
                "max_chars": {"type": "integer", "description": "Maximum returned characters. Defaults to 20000; use helpers for broad reading.\n返回字符上限。", "default": 20000},
                "include_content": {
                    "type": "boolean",
                    "description": "Return body content for non-source files when exact local text is intentionally needed. Main-thread full-file source/test reads stay compact even if true; use a narrow line range or a code helper for source/test text.\n非源码文件确需正文时设为 true；主进程源码/测试全文件读取仍压缩，源码/测试精确文本用窄行范围或 code helper。",
                    "default": False,
                },
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
            "Call this before the main thread edits/applies an existing project file. A code helper can receive "
            "project-relative paths in input_files even when they are not staged; helper startup stages exact copies. "
            "For helper handoff alone, project-relative input_files already carry the routing fact; env_fetch becomes "
            "useful after a helper returns a staged edit candidate and the main thread needs expected_hash, diff, or apply evidence. "
            "The returned `_env/...` path is a staged workspace copy for read_file/edit_file/multi_edit/insert_in_file; "
            "env_run uses real project-relative paths instead."
            "\n\n主进程编辑/应用既有项目文件前 fetch；helper 可直接接收项目相对 input_files 并自动暂存。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project-relative file path.\n项目相对文件路径。"},
                "force": {
                    "type": "boolean",
                    "description": "Overwrite a staged `_env/...` copy that differs from the project source. Default false keeps staged edits (helper outputs) and returns their hashes.\n暂存副本有未应用修改时默认保留；force=true 才覆盖。",
                    "default": False,
                },
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
            "Run a command inside the real project directory for tests, builds, checks, statistics, and focused probes; env_run uses the real project tree, so command and cwd use project-relative paths. "
            "Prefer this over workspace/bash for project validation. "
            "For coding/debugging, once likely paths and a test/build command are known, that command should usually be passed as a code-helper acceptance_check; "
            "main-thread env_run adds most value for missing routing facts, focused probes before a delegation choice, or narrow checks for main-owned changes. "
            "For Python inspection (incl. SQLite/CSV/JSON data files), pass python_code — it runs from a system temporary file outside the project tree with project cwd and is auto-deleted; running `python data.db` executes the data file as a script, not a data CLI. "
            "Keep metric names and units exact: Characters, bytes, file size, line count, and file count are different metrics. "
            "Staged `_env/...` copies are edited with read_file/edit_file or helpers, then env_diff/env_apply_replace updates real project files. "
            "For stdout acceptance, use stdout_facts and the verifier's comparison semantics; CRLF line endings and tail_repr are text facts, do not convert them into a byte-level output requirement unless the contract explicitly says bytes, binary, or byte-for-byte. "
            "Use env_run for statistics, inventories, and spot checks, not for bulk body extraction from Office/PDF/image sources — that belongs to read helpers. "
            "Use platform-native shell syntax (no POSIX `|| true` continuations unless the active shell supports them); after a SyntaxError, change the quoting or script shape before retrying. Inspection scripts are not project files: exclude transient inspection scripts, caches, and probe files from results unless explicitly requested.\n\n"
            "env_run 用于真实项目目录内验证、统计和定点抽查；数据文件探测优先 python_code；stdout 验收需查看 stdout_facts 并按验证器语义比较；临时检查脚本不属于项目文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run.\n要执行的命令。"},
                "python_code": {
                    "type": "string",
                    "description": (
                        "Alternative to command: Python script body for inspection/statistics. When set, env_run writes it to a system temporary file, "
                        "runs it with cwd set to the project directory, and deletes it automatically. Keep inspection scripts temporary instead of applying them as project files.\n"
                        "与 command 二选一的临时 Python 检查脚本正文。"
                    ),
                },
                "cwd": {"type": "string", "description": "Project-relative working directory. Defaults to root.\n项目相对工作目录。"},
                "timeout_sec": {"type": "integer", "description": "Timeout in seconds. Defaults to 30, max 1800.\n命令超时秒数。", "default": 30},
            },
            "oneOf": [
                {"required": ["command"], "not": {"required": ["python_code"]}},
                {"required": ["python_code"], "not": {"required": ["command"]}},
            ],
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
    ENV_BACKGROUND_SCHEMA,
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
            result = await _handle_run(args, workspace_dir=workspace_dir)
        elif name == "env_background":
            result = await handle_background_tool(workspace_dir, args)
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
    raw_result = json.dumps(result, ensure_ascii=False)
    budgeted_result = apply_result_budget(name, raw_result, spill_root=workspace_dir)
    debug.log(f"environment.{name}", str(result)[:500], result)
    return budgeted_result


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
    file_paths = [
        str(row.get("path") or "").replace("\\", "/")
        for row in rows
        if isinstance(row, dict) and row.get("type") == "file"
    ]
    acceptance_scripts = _acceptance_script_paths(file_paths)
    if acceptance_scripts:
        result["acceptance_script_paths"] = acceptance_scripts[:20]
    hint = _list_tree_workflow_hint(rows)
    if hint:
        result["next_action_instruction"] = hint
    text_material_fact = _compact_text_material_fact(rows)
    if text_material_fact:
        result["text_material_handoff_fact"] = text_material_fact
    handoff_fact = _list_tree_code_helper_handoff_fact(rows)
    if handoff_fact:
        result["helper_handoff_fact"] = handoff_fact
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
    acceptance_scripts = _acceptance_script_paths([
        str(item.get("project_path") or "").replace("\\", "/")
        for item in resources
        if isinstance(item, dict)
    ])
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
    result = {
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
            "Use the generated manifest paths as routing evidence. Use project_path values in helper input_files; "
            "use env_fetch only when the main thread must stage a file for apply/diff or a narrow main-owned evidence gap. If many files "
            "must be staged, call env_inventory with stage=true and narrow filters. Use existing staged_path values for "
            "helper prompts. For coding/debugging, these paths are enough for a compact code-helper request; do not expand "
            "them into source bodies or long bug analysis in the main thread. Split broad content extraction by category, "
            "directory, file batch, page range, or image range and delegate read/code helpers before synthesis.\n"
            "清单用于路径和交接事实；代码调试可直接把路径放入 helper input_files，不在主进程展开源码或长分析。"
        ),
    }
    text_material_fact = _compact_text_material_fact(rows)
    if text_material_fact:
        result["text_material_handoff_fact"] = text_material_fact
        result["next_action_instruction"] = (
            "Use the generated manifest paths and project_path values as routing evidence. "
            "Fact: this inventory is a compact text-material set; a single read/edit/code helper can receive the listed material paths as input_files and produce the requested report, drafts, data, or project-facing artifacts from them. "
            "Per-file helper fan-out or main-thread full-body expansion usually adds coordinator context unless independent extraction, media/OCR/Office handling, or broad coverage proof is required. "
            "Use env_fetch only when the main thread must stage a file for apply/diff or a narrow main-owned evidence gap.\n"
            "清单是紧凑文本材料集；通常一次性交给一个 helper 读取和产出，主线程只在应用、diff 或主进程自有证据缺口需要时暂存文件。"
        )
    if acceptance_scripts:
        result["acceptance_script_paths"] = acceptance_scripts[:20]
        result["acceptance_script_note"] = (
            "Project-provided verify/check/validate scripts are acceptance facts. Their contents or command output can reveal "
            "required paths and completion criteria when output targets or checks are ambiguous."
        )
    return result


def _acceptance_script_paths(file_paths: list[str]) -> list[str]:
    script_suffixes = {".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".ps1", ".bat", ".cmd"}
    accepted: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"^(?:"
        r"verify(?:[_-].+)?|"
        r"check(?:[_-].+)?|"
        r"validate(?:[_-].+)?|"
        r"run[_-]?tests?|"
        r"test[_-]?runner|"
        r"grade(?:r)?(?:[_-].+)?"
        r")$",
        re.IGNORECASE,
    )
    for raw in file_paths:
        path = str(raw or "").replace("\\", "/").strip()
        if not path:
            continue
        name = Path(path).name
        suffix = Path(name).suffix.lower()
        stem = Path(name).stem.lower()
        if suffix not in script_suffixes:
            continue
        if not pattern.match(stem):
            continue
        if path not in seen:
            accepted.append(path)
            seen.add(path)
    return accepted


def _list_tree_code_helper_handoff_fact(rows: list[dict]) -> dict | None:
    file_paths = [
        str(row.get("path") or "").replace("\\", "/")
        for row in rows
        if isinstance(row, dict) and row.get("type") == "file"
    ]
    dir_paths = [
        str(row.get("path") or "").replace("\\", "/")
        for row in rows
        if isinstance(row, dict) and row.get("type") == "dir"
    ]
    code_suffixes = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go",
        ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".rb", ".swift",
        ".kt", ".kts", ".scala", ".sql", ".r", ".lua",
    }
    data_suffixes = {
        ".db", ".sqlite", ".sqlite3", ".csv", ".tsv", ".json", ".jsonl", ".parquet",
    }
    config_names = {
        "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
        "pyproject.toml", "requirements.txt", "poetry.lock", "uv.lock", "setup.py",
        "cargo.toml", "cargo.lock", "go.mod", "go.sum", "cmakelists.txt",
        "makefile", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "tsconfig.json", "vite.config.ts", "webpack.config.js", "rollup.config.js",
        "pytest.ini", "tox.ini", "ruff.toml", ".eslintrc", ".prettierrc",
    }
    command_shims = [
        path for path in file_paths
        if Path(path).suffix.lower() in {".cmd", ".bat", ".ps1", ".sh"}
        and Path(path).name.lower().startswith(("python", "node", "npm", "pnpm", "yarn", "pytest"))
    ]
    root_hidden_files = [
        path for path in file_paths
        if "/" not in path and Path(path).name.startswith(".")
    ]
    non_source_routing_paths = set(command_shims) | set(root_hidden_files)
    test_paths = [
        path for path in file_paths
        if path not in non_source_routing_paths
        and (
            "/test" in f"/{path.lower()}"
            or Path(path).name.lower().startswith("test_")
            or ".test." in Path(path).name.lower()
            or ".spec." in Path(path).name.lower()
        )
    ]
    acceptance_scripts = [
        path for path in _acceptance_script_paths(file_paths)
        if path not in non_source_routing_paths
    ]
    code_or_config_paths = [
        path for path in file_paths
        if path not in non_source_routing_paths
        and (Path(path).suffix.lower() in code_suffixes or Path(path).name.lower() in config_names)
    ]
    data_paths = [
        path for path in file_paths
        if path not in non_source_routing_paths
        and Path(path).suffix.lower() in data_suffixes
    ]
    relevant_paths: list[str] = []
    seen: set[str] = set()
    for path in [*data_paths, *code_or_config_paths, *test_paths, *acceptance_scripts]:
        if path and path not in non_source_routing_paths and path not in seen:
            relevant_paths.append(path)
            seen.add(path)
    compact_code_listing = (
        (code_or_config_paths or data_paths)
        and (test_paths or acceptance_scripts)
        and len(file_paths) <= 16
        and len(dir_paths) <= 8
    )
    if not compact_code_listing:
        return None
    fact = {
        "kind": "code_helper_handoff_ready",
        "project_paths": relevant_paths[:12],
        "path_basis": "env_list_tree exposed source/config/data/test/acceptance paths suitable for helper input_files.",
        "main_thread_read_value": (
            "Additional main-thread env_read/env_run adds value mainly for missing routing facts, diff/apply preparation, "
            "missing acceptance constraints, or narrow checks for main-owned changes. Reading every listed source/test body or probing "
            "every listed data file in the main thread before delegation can duplicate helper-owned work and increases coordinator context."
        ),
        "summary_zh": "目录已暴露 helper 可用的源码/配置/数据/测试路径；主线程批量展开或探测会重复 helper 工作并扩大上下文。",
    }
    if data_paths:
        fact["data_paths"] = data_paths[:8]
        fact["data_path_note"] = (
            "Data files such as databases, CSV/TSV, JSON/JSONL, or columnar files are routing facts for data/query "
            "artifact tasks. A compact code helper can receive them as input_files and run focused local probes itself."
        )
    if command_shims:
        fact["command_shim_paths"] = command_shims[:6]
        fact["command_shim_note"] = "Interpreter or runner shim paths are command facts; their file bodies are rarely source-diagnosis inputs."
    if acceptance_scripts:
        fact["acceptance_script_paths"] = acceptance_scripts[:8]
        fact["acceptance_script_note"] = (
            "Project-provided verify/check/validate scripts are acceptance facts. Their contents or command output can reveal "
            "required paths and completion criteria when output targets or checks are ambiguous."
        )
    return fact


def _compact_text_material_fact(rows: list[dict]) -> dict | None:
    files = [
        r for r in rows
        if isinstance(r, dict) and (r.get("type") == "file" or r.get("project_path"))
    ]
    dirs = [r for r in rows if isinstance(r, dict) and r.get("type") == "dir"]
    if not files:
        return None

    compact_text_suffixes = {
        ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".tsv",
        ".json", ".jsonl", ".yaml", ".yml", ".xml",
    }
    binary_or_special_suffixes = {
        ".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx", ".ppt",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
        ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".avi", ".zip", ".rar", ".7z",
    }
    source_code_suffixes = {
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

    file_paths = [str(row.get("path") or row.get("project_path") or "").replace("\\", "/") for row in files]
    command_shims = {
        path for path in file_paths
        if Path(path).suffix.lower() in {".cmd", ".bat", ".ps1", ".sh"}
        and Path(path).name.lower().startswith(("python", "node", "npm", "pnpm", "yarn", "pytest"))
    }
    acceptance_scripts = set(_acceptance_script_paths(file_paths))
    non_material_routing_paths = command_shims | acceptance_scripts

    material_paths: list[str] = []
    material_bytes = 0
    unknown_sizes = 0
    binary_or_special_paths: list[str] = []
    code_project_paths: list[str] = []
    config_paths: list[str] = []
    for row, path in zip(files, file_paths):
        suffix = Path(path).suffix.lower()
        name = Path(path).name.lower()
        size_raw = row.get("size")
        size = int(size_raw) if isinstance(size_raw, int) and size_raw >= 0 else None
        if path in non_material_routing_paths:
            continue
        if name in config_names:
            config_paths.append(path)
            continue
        if suffix in binary_or_special_suffixes:
            binary_or_special_paths.append(path)
            continue
        if suffix in compact_text_suffixes:
            material_paths.append(path)
            if size is None:
                unknown_sizes += 1
            else:
                material_bytes += size
            continue
        if suffix in source_code_suffixes:
            code_project_paths.append(path)
            continue

    if binary_or_special_paths:
        return None
    if code_project_paths:
        return None
    if len(config_paths) >= 2:
        return None
    if not (3 <= len(material_paths) <= 20):
        return None
    if len(files) > 28 or len(dirs) > 10:
        return None
    if unknown_sizes > 2:
        return None
    if material_bytes > 150_000:
        return None

    return {
        "kind": "compact_text_material_set",
        "material_paths": material_paths[:20],
        "material_count": len(material_paths),
        "total_known_material_bytes": material_bytes,
        "acceptance_script_paths": sorted(acceptance_scripts)[:8],
        "command_shim_paths": sorted(command_shims)[:6],
        "fact": (
            "This listing is a compact text-material set: listed source materials are small text-like files, "
            "and no Office/PDF/image/audio/archive material or code-project source body is visible in the listing. "
            "A single read/edit/code helper can receive these paths as input_files and produce the requested report, "
            "drafts, data, or project-facing artifacts from them. Per-file helper fan-out or main-thread full-body "
            "expansion usually adds coordinator context unless independent extraction, media/OCR/Office handling, "
            "or broad coverage proof is required."
        ),
        "summary_zh": "目录是紧凑文本材料集；通常可把这些路径一次性交给一个 helper 读取和产出，逐文件扇出或主线程全文展开会增加协调上下文。",
    }


def _list_tree_workflow_hint(rows: list[dict]) -> str:
    files = [r for r in rows if r.get("type") == "file"]
    dirs = [r for r in rows if r.get("type") == "dir"]
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
    data_suffixes = {
        ".db", ".sqlite", ".sqlite3", ".csv", ".tsv", ".json", ".jsonl", ".parquet",
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
    command_shims = [
        path for path in file_paths
        if Path(path).suffix.lower() in {".cmd", ".bat", ".ps1", ".sh"}
        and Path(path).name.lower().startswith(("python", "node", "npm", "pnpm", "yarn", "pytest"))
    ]
    root_hidden_files = [
        path for path in file_paths
        if "/" not in path and Path(path).name.startswith(".")
    ]
    non_source_routing_paths = set(command_shims) | set(root_hidden_files)
    source_material_count = sum(
        1 for path in file_paths
        if Path(path).suffix.lower() in source_material_suffixes
    )
    code_count = sum(
        1 for path in file_paths
        if path not in non_source_routing_paths and Path(path).suffix.lower() in code_suffixes
    )
    config_count = sum(1 for path in file_paths if Path(path).name.lower() in config_names)
    doc_count = sum(1 for path in file_paths if Path(path).name.lower() in doc_names)
    test_count = sum(
        1 for path in file_paths
        if "/test" in f"/{path.lower()}" or Path(path).name.lower().startswith("test_")
        or ".test." in Path(path).name.lower() or ".spec." in Path(path).name.lower()
    )
    data_count = sum(
        1 for path in file_paths
        if path not in non_source_routing_paths and Path(path).suffix.lower() in data_suffixes
    )
    acceptance_scripts = _acceptance_script_paths(file_paths)
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
    compact_code_listing = (
        code_count > 0
        and (test_count > 0 or acceptance_scripts)
        and len(files) <= 16
        and len(rows) <= 30
    )
    compact_data_listing = (
        data_count > 0
        and acceptance_scripts
        and len(files) <= 16
        and len(rows) <= 30
    )
    compact_text_fact = _compact_text_material_fact(rows)
    if compact_text_fact and not complex_project:
        material_preview = ", ".join(compact_text_fact["material_paths"][:8])
        acceptance_note = (
            " Verify/check scripts in the listing are acceptance facts; their source or command output can define required files and completion checks."
            if compact_text_fact.get("acceptance_script_paths")
            else ""
        )
        command_note = (
            " Interpreter/runner shim paths in the listing are command facts rather than material bodies."
            if compact_text_fact.get("command_shim_paths")
            else ""
        )
        return (
            "This listing is a compact text-material set"
            + (f" ({material_preview})" if material_preview else "")
            + ". The listed materials are small text-like files, with no visible Office/PDF/image/audio/archive material or code-project source body. "
            "A single read/edit/code helper can receive these paths as input_files and produce the requested report, drafts, data, or project-facing artifacts from them. "
            "Per-file helper fan-out or main-thread full-body expansion usually adds coordinator context unless independent extraction, media/OCR/Office handling, or broad coverage proof is required."
            f"{acceptance_note}{command_note}\n\n"
            "目录是紧凑文本材料集；通常可把路径一次性交给一个 helper 读取和产出，逐文件扇出或主线程全文展开会增加协调上下文。"
        )
    if (compact_code_listing or compact_data_listing) and not complex_project:
        handoff_fact = _list_tree_code_helper_handoff_fact(rows) or {}
        likely_paths = ", ".join(path for path in (handoff_fact.get("project_paths") or [])[:8] if path)
        command_shim_note = (
            " Interpreter/runner shim paths in the listing are command facts rather than source-body targets."
            if handoff_fact.get("command_shim_paths")
            else ""
        )
        acceptance_note = (
            " Project-provided verify/check/validate scripts in the listing are acceptance facts; read or run them when "
            "target paths or completion checks are ambiguous."
            if acceptance_scripts
            else ""
        )
        data_note = (
            " Data files in the listing are routing facts for data/query artifact tasks; a compact code helper can receive them as input_files and run focused probes."
            if compact_data_listing
            else ""
        )
        return (
            "This compact listing already exposes source/test/acceptance project paths or data/acceptance project paths"
            + (f" ({likely_paths})" if likely_paths else "")
            + ". For coding/debugging or data/query artifact work, these facts plus acceptance checks are enough for a compact code-helper request with input_files; "
            "the helper can read source bodies, probe data files, and test. source reading is not the same pre-edit evidence when browser reproduction is requested. "
            "Main-thread env_read/env_run is mainly useful for one missing routing fact, diff/apply preparation, missing acceptance constraints, or narrow checks for main-owned changes. "
            "A parallel batch of main-thread env_read calls or env_run probes usually duplicates helper work; main-thread env_read/env_run calls over listed source/test/data files are usually redundant once routing facts are known; "
            "for source/test files this duplicates code-helper work."
            f"{command_shim_note}{acceptance_note}{data_note}\n\n"
            "目录已暴露源码/测试/验收路径和数据路径；通常可用 input_files 派发 helper，主进程批量展开源码会重复 helper 工作，数据探测同理。"
        )
    if len(files) < 8 and len(rows) < 20:
        return ""
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


_SOURCE_BODY_HINT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".php", ".rb", ".swift", ".scala", ".sh", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".json",
}


def _env_read_looks_source_or_test(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    normalized = path.replace("\\", "/")
    parts = {part.lower() for part in Path(normalized).parts}
    basename = Path(normalized).name.lower()
    return (
        suffix in _SOURCE_BODY_HINT_SUFFIXES
        or basename.startswith("test_")
        or basename.endswith("_test.py")
        or bool({"tests", "test", "src", "app"}.intersection(parts))
    )


def _is_main_thread_env_tool_call() -> bool:
    try:
        from app.core.core_processes import current_helper_proc_id, current_helper_kind
        return current_helper_proc_id() is None and not current_helper_kind()
    except Exception:
        return True


def _env_read_source_body_hint(path: str, total_lines: int, shown_lines: int) -> str | None:
    if not _env_read_looks_source_or_test(path) or shown_lines < 5:
        return None
    return (
        f"Fact: env_read returned a source/test body for {path} ({shown_lines}/{total_lines} lines shown). "
        "For coding/debugging, if likely source/test paths are now known, stop expanding additional source bodies in the main thread and delegate a compact code helper with input_files + acceptance_checks. "
        "The code helper can use the shown source/test evidence, run a baseline only when it adds diagnostic value or is requested, diagnose, edit, and test from those files. "
        "If the user explicitly requested browser/host-browser reproduction, this source read is not the same pre-edit evidence. "
        "Additional main reads are useful mainly for missing routing facts, missing acceptance constraints, or main-owned gaps that cannot be answered by helper output or env_diff.\n\n"
        "事实：已在主进程展开一个源码/测试正文；路径已知时停止继续展开，通常把 input_files 和验收项交给 helper。"
    )


def _env_read_compact_source_handoff_fact(path: str, total_lines: int, start_line: int, end_line: int) -> dict:
    return {
        "kind": "main_thread_source_body_compacted",
        "path": path,
        "total_lines": total_lines,
        "requested_start_line": start_line,
        "requested_end_line": end_line,
        "fact": (
            f"Fact: `{path}` is a source/test file. The main-thread env_read returned compact path/hash/line facts "
            "instead of source body content because likely source/test paths can be handed to a code helper through "
            "`input_files` with acceptance checks. This preserves coordinator context; it does not decide the task outcome. "
            "If exact local text is intentionally needed in the main thread, read only the smallest relevant line range."
        ),
        "summary_zh": (
            "源码/测试文件默认不把正文塞入主进程上下文；可交给 code helper 自读自测。"
            "主进程确需精确文本时，只读取最小相关行范围。"
        ),
    }


def _env_fetch_helper_handoff_hint(project_path: str, workspace_path: str) -> str | None:
    suffix = Path(project_path).suffix.lower()
    normalized = project_path.replace("\\", "/")
    parts = {part.lower() for part in Path(normalized).parts}
    basename = Path(normalized).name.lower()
    looks_source_or_test = (
        suffix in _SOURCE_BODY_HINT_SUFFIXES
        or basename.startswith("test_")
        or basename.endswith("_test.py")
        or bool({"tests", "test", "src", "app"}.intersection(parts))
    )
    if not looks_source_or_test:
        return None
    return (
        f"Fact: env_fetch staged {project_path} as {workspace_path}. For coding/debugging helper handoff, this staged path is already usable in input_files; "
        "a helper can read source bodies, use existing failure evidence or run a baseline only when useful, diagnose, edit, and test from the staged files. "
        "If the user explicitly requested browser/host-browser reproduction, a staged source read is not the same pre-edit evidence. "
        "If the next step is a source/project edit in the main workflow, delegate the staged output to a helper with expected_outputs; "
        "main-thread reads/diff/apply remain useful for missing routing facts, apply preparation, and narrow checks for main-owned changes.\n\n"
        "事实：文件已暂存为 helper 可读输入；项目源码修改通常交给 helper 产出并自验 `_env/...`，主进程负责局部主责事实、diff/apply 和验收记账。"
    )


def _exact_reference_hint(path: str, text: str) -> dict | None:
    normalized = str(path or "").replace("\\", "/").strip()
    lower_parts = {part.lower() for part in Path(normalized).parts}
    basename = Path(normalized).name.lower()
    reference_dirs = {"expected", "golden", "snapshot", "snapshots", "fixture", "fixtures", "baseline", "reference"}
    reference_name = (
        basename.startswith(("expected.", "expected_", "golden.", "golden_", "snapshot.", "snapshot_"))
        or basename.endswith((".golden", ".expected", ".snapshot"))
    )
    if not (lower_parts & reference_dirs or reference_name):
        return None
    return {
        "kind": "exact_text_reference",
        "path": normalized,
        "text_facts": _stream_text_facts(text),
        "fact": (
            "This path looks like an expected/golden/snapshot/reference file. When the task or verifier compares "
            "program output to it, line order, delimiters, visible text, and trailing blank lines are acceptance facts. "
            "Reference-file bytes may contain platform line endings; for stdout checks, follow the verifier's text-vs-byte comparison semantics rather than assuming byte-for-byte stdout is required. "
            "Semantic equivalence alone may not satisfy exact-output checks.\n\n"
            "该路径看起来是 expected/golden/snapshot/reference 参考文件；用于输出比对时，行序、分隔符、可见文本和尾部空行都是验收事实；stdout 是否需要逐字节匹配取决于验证器语义。"
        ),
    }


_ACCEPTANCE_SCRIPT_STEM_RE = re.compile(
    r"^(?:verify(?:[_-].+)?|check(?:[_-].+)?|validate(?:[_-].+)?|run[_-]?tests?|test[_-]?runner|grade(?:r)?(?:[_-].+)?)$",
    re.IGNORECASE,
)
_SCRIPT_WORKSPACE_TEXT_SCAN_RE = re.compile(
    r"\b(?:rglob|os\.walk|glob\.glob|workspace_blob|iter_workspace_text_files)\b",
    re.IGNORECASE,
)
_SCRIPT_CURRENT_ROOT_RE = re.compile(r"Path\(\s*['\"]\.\s*['\"]\s*\)|root\s*:\s*Path\s*=\s*Path\(", re.IGNORECASE)
_SCRIPT_TEXT_READ_RE = re.compile(r"\b(?:read_text|open\(|read\(|TEXT_SUFFIXES|suffix)\b", re.IGNORECASE)
_SCRIPT_LOWERCASE_RE = re.compile(r"\.lower\s*\(", re.IGNORECASE)
_SCRIPT_EXCLUDES_RE = re.compile(r"\b(?:EXCLUDE|exclude|verify_|BOOTSTRAP|IDENTITY|AGENTS)\b", re.IGNORECASE)
_SCRIPT_LITERAL_LIST_ASSIGN_RE = re.compile(
    r"\b(?P<name>needed|any_of|all_of|required|must_have|expected|forbidden|missing)\b\s*=\s*\[(?P<body>[^\]]{0,600})\]",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_QUOTED_STRING_RE = re.compile(r"['\"]([^'\"]{1,120})['\"]")


def _env_read_acceptance_script_fact(path: str, text: str) -> dict | None:
    normalized = str(path or "").replace("\\", "/").strip()
    basename = Path(normalized).name
    suffix = Path(basename).suffix.lower()
    stem = Path(basename).stem
    if suffix not in {".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".ps1", ".bat", ".cmd"}:
        return None
    if not _ACCEPTANCE_SCRIPT_STEM_RE.match(stem):
        return None

    scans_text = bool(_SCRIPT_WORKSPACE_TEXT_SCAN_RE.search(text) and _SCRIPT_TEXT_READ_RE.search(text))
    current_root = bool(_SCRIPT_CURRENT_ROOT_RE.search(text))
    lowercases = bool(_SCRIPT_LOWERCASE_RE.search(text))
    excludes = bool(_SCRIPT_EXCLUDES_RE.search(text))
    literal_lists: list[dict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for match in _SCRIPT_LITERAL_LIST_ASSIGN_RE.finditer(text):
        strings = [s for s in _SCRIPT_QUOTED_STRING_RE.findall(match.group("body")) if s.strip()]
        if not strings:
            continue
        strings = strings[:12]
        key = (match.group("name"), tuple(strings))
        if key in seen:
            continue
        seen.add(key)
        literal_lists.append({"name": match.group("name"), "strings": strings})
        if len(literal_lists) >= 8:
            break

    fact = {
        "kind": "acceptance_script_read_fact",
        "path": normalized,
        "scans_project_or_workspace_text": scans_text,
        "uses_current_directory_root": current_root,
        "lowercases_text_before_checks": lowercases,
        "has_excluded_paths_or_fragments": excludes,
        "literal_string_lists": literal_lists,
        "fact": (
            "env_read returned an acceptance/check script. Its source is current acceptance evidence; use the script's "
            "observed checks, scan scope, and command result as facts before claiming completion."
        ),
        "事实": "env_read 返回的是验收/检查脚本；脚本源码中的检查项、扫描范围和运行结果是当前验收事实。",
    }
    if scans_text:
        fact["content_visibility_fact"] = (
            "This script appears to build acceptance checks from text files it reads under the project/workspace. "
            "A chat-only final response is not part of that scanned text."
        )
    literal_summary = ""
    if literal_lists:
        chunks = []
        for item in literal_lists[:4]:
            values = ", ".join(repr(s) for s in item["strings"][:8])
            chunks.append(f"{item['name']}=[{values}]")
        literal_summary = " Literal string lists seen in the script: " + "; ".join(chunks) + "."
    scan_summary = (
        " The script appears to recursively read project/workspace text files; a chat-only final response is not included in that scan."
        if scans_text
        else " Use the script body and its command output to identify the checked artifact or state."
    )
    root_summary = " It appears to use the current working directory as the scan root." if current_root else ""
    case_summary = " It lowercases scanned text before checks." if lowercases else ""
    exclude_summary = " It excludes some paths/fragments from output evidence." if excludes else ""
    fact["next_action_instruction"] = (
        "Fact: env_read returned an acceptance/check script."
        f"{scan_summary}{root_summary}{case_summary}{exclude_summary}{literal_summary} "
        "If the active task must satisfy this verifier, ensure the verifier-visible project/workspace state exists, "
        "then run the verifier and use its result as acceptance evidence.\n\n"
        "事实：本次读取的是验收脚本；若当前任务受该脚本约束，需让脚本可见的项目/工作区状态满足检查，再运行脚本验收。"
    )
    return fact


def _env_read_command_shim_fact(path: str, text: str) -> dict | None:
    name = Path(path).name.lower()
    suffix = Path(name).suffix.lower()
    runner_prefixes = (
        "python", "python3", "node", "npm", "pnpm", "yarn", "pytest", "uv", "pip",
        "bash", "sh", "cmd",
    )
    is_runner_script = suffix in {".cmd", ".bat", ".ps1", ".sh"} and name.startswith(runner_prefixes)
    is_shim_script = "shim" in name and suffix in {".py", ".js", ".mjs", ".cjs", ".cmd", ".bat", ".ps1", ".sh"}
    if not (is_runner_script or is_shim_script):
        return None
    first_lines = "\n".join(text.splitlines()[:6]).lower()
    command_markers = ("python", "node", "exec", "%*", "$@", "subprocess", "runpy", "sys.argv")
    if not any(marker in first_lines for marker in command_markers):
        return None
    return {
        "kind": "command_shim_read_fact",
        "path": path,
        "fact": (
            "env_read returned a runner/interpreter shim. This file is command-routing evidence, not business "
            "schema, source logic, or task data by itself. If a command using this shim already runs, prefer the "
            "command stdout/stderr facts over reading deeper shim internals unless the active issue is a runner failure."
        ),
        "summary_zh": "本文件是命令/解释器 shim；除非正在排查运行器故障，否则优先看命令输出，不把 shim 内部当业务证据。",
    }


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
    include_content = bool(args.get("include_content", False))
    text = _read_text(path)
    lines = text.splitlines()
    exact_reference = _exact_reference_hint(display_path, text)
    acceptance_script_fact = _env_read_acceptance_script_fact(display_path, text)
    command_shim_fact = _env_read_command_shim_fact(display_path, text)
    is_default_full_read = start_line == 1 and end_line < start_line
    should_compact_source_body = (
        _is_main_thread_env_tool_call()
        and source_zone == "project"
        and is_default_full_read
        and _env_read_looks_source_or_test(display_path)
        and exact_reference is None
        and acceptance_script_fact is None
        and command_shim_fact is None
    )
    if should_compact_source_body:
        return {
            "ok": True,
            "path": display_path,
            "source_zone": source_zone,
            "sha256": _sha256(path),
            "start_line": start_line,
            "end_line": 0,
            "total_lines": len(lines),
            "content": "",
            "content_compacted": True,
            "truncated": False,
            "source_handoff_fact": _env_read_compact_source_handoff_fact(display_path, len(lines), start_line, end_line),
            "next_action_instruction": (
                f"Fact: env_read identified source/test file `{display_path}` ({len(lines)} lines) and returned no body content by default. "
                "For coding/debugging, delegate a compact code helper with this path in input_files and the active acceptance checks; the helper owns reading, diagnosis, edits, and tests. "
                "If the main thread intentionally needs exact local text, read only the smallest relevant line range.\n\n"
                "事实：源码/测试正文默认未注入主进程上下文；通常把路径和验收项交给 code helper，需要精确文本时读取最小相关行范围。"
            ),
        }
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
    if exact_reference:
        result["exact_text_reference"] = exact_reference
    if acceptance_script_fact:
        result["acceptance_script_fact"] = acceptance_script_fact
    if command_shim_fact:
        result["command_shim_read_fact"] = command_shim_fact
    if truncated:
        if workspace_dir:
            saved_path = write_tool_output_spill(
                root_dir=workspace_dir,
                tool_name="env_read",
                label="content",
                text=text,
            )
            result["content_full_saved_path"] = saved_path
            result["content_original_chars"] = len(text)
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
            f"Content was capped at {max_chars} characters. Broad coverage is read helper-suitable; main-process "
            "env_read is for already-located local evidence or narrow main-owned checks.\n"
            "内容已按上限返回；大范围阅读优先交给 helper，主进程只做已定位局部证据或主进程自有窄核查。"
        )
        if workspace_dir:
            result["note"] += (
                f"\nFull normalized file content was saved at `{result['content_full_saved_path']}`; read a targeted "
                "segment of that saved file only if the active task needs omitted details.\n"
                "完整规范化正文已保存；需要缺失细节时再定向读取保存文件。"
            )
        if acceptance_script_fact:
            result["note"] += (
                "\nFact: the truncated file is an acceptance/check script; pass it to the producer/verify helper, or read/run only the missing part if this is a main-owned evidence boundary."
                "\n事实：截断文件是验收脚本；优先交给生产/验证 helper，只有主进程自有证据边界才补读或运行缺失部分。"
            )
    else:
        hint = (
            acceptance_script_fact.get("next_action_instruction")
            if acceptance_script_fact
            else (
                command_shim_fact.get("fact")
                if command_shim_fact
                else _env_read_source_body_hint(display_path, len(lines), len(out_lines))
            )
        )
        if hint:
            result["next_action_instruction"] = hint
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
        result = {
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
        # 2026-06-10 Round 7: when a staged/workspace copy of the same name
        # exists, the model is usually mid-apply (fetch was for a hash that a
        # nonexistent target cannot have). Say so directly instead of letting
        # it guess (20260610_163156: two such misses dinged recovery score).
        if workspace_dir:
            rel_guess = requested_path.replace("\\", "/").strip().lstrip("./")
            for probe in (rel_guess, f"_env/{rel_guess}"):
                try:
                    staged_probe = Path(workspace_dir).resolve() / probe
                    if staged_probe.is_file():
                        result["staged_copy_exists"] = probe
                        result["next_action"] = (
                            f"The project file does not exist, but workspace copy `{probe}` does. New project files "
                            "need no fetch/hash: create directly with "
                            f"env_apply_create(path={rel_guess!r}, workspace_path={probe!r}).\n\n"
                            "项目文件不存在但工作区副本存在；新建项目文件不需要 fetch/hash，直接 env_apply_create。"
                        )
                        break
                except OSError:
                    pass
        return result
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
                "Recovery facts: env_run can collect metadata or targeted extraction; a read helper can receive the exact project_path from env_inventory; OCR/Office extraction tools may apply for relevant formats. "
                "If only a small excerpt is needed, bounded project-side extraction can save a small evidence file before reading it.\n\n"
                "大文件恢复事实：可用 env_run 做元数据/小片段抽取，或把精确 project_path 交给 read helper 分批处理。"
            ),
            "recovery_facts": {
                "matching_helper_kind": "read",
                "matching_project_path": _rel_to_root(src),
                "available_shapes": [
                    "env_run metadata or bounded extraction",
                    "read helper with exact project_path",
                    "chunked OCR/Office extraction when format requires it",
                ],
            },
            "suggested_helper_kind": "read",
            "suggested_project_path": _rel_to_root(src),
        }
        if suffix == ".pdf":
            result["observed_recovery_options"] = [
                "env_run can inspect PDF page count or metadata without copying the whole file.",
                "a read helper can receive this exact project_path for selected pages or summarized extraction, using OCR when needed.",
                "full OCR needs page batching and chunked evidence summaries instead of moving the source PDF into _env.",
            ]
        elif suffix in {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}:
            result["observed_recovery_options"] = [
                "env_run or an Office/read helper can extract bounded metadata or selected body text in chunks.",
                "full content extraction needs read-helper coverage summaries plus evidence files.",
            ]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
            result["observed_recovery_options"] = [
                "env_run can inspect image dimensions or metadata.",
                "OCR/vision reading can use the exact project_path and chunked evidence.",
            ]
        return result
    rel = _rel_to_root(src)
    # 2026-06-10: protect staged work from refetch clobbering. In run
    # 20260610_134512 the model issued env_diff + env_fetch in one parallel
    # turn; env_fetch overwrote the helper-edited `_env/...` copy with pristine
    # project content, env_apply_replace then applied the unchanged file, and
    # the task burned two extra helper rounds recovering. If the staged copy
    # already differs from the project source, keep it and return facts.
    if not bool(args.get("force")):
        existing_dst = _workspace_copy_path(workspace_dir, rel)
        if existing_dst.exists() and existing_dst.is_file():
            staged_hash = _sha256(existing_dst)
            source_hash = _sha256(src)
            if staged_hash != source_hash:
                workspace_rel = str(existing_dst.relative_to(Path(workspace_dir).resolve())).replace("\\", "/")
                # 2026-06-11 Round 17: the preserve branch must still register
                # the staged copy. Skipping registration left the file invisible
                # to the apply guard's registry checks (test_filesystem_registry
                # regressions), so a preserved helper edit could not be applied
                # through the ready-record path.
                registry = _environment_file_registry(workspace_dir)
                if registry is not None and registry.find_by_workspace_path(workspace_rel) is None:
                    try:
                        from app.core.filesystem.models import FileKind, FileStatus, Visibility
                        record = registry.upsert_project_file(
                            src,
                            kind=FileKind.STAGED_INPUT,
                            status=FileStatus.STAGED,
                            visibility=Visibility.PROJECT,
                            origin="env_fetch_preserved_staged_copy",
                            staged=True,
                            metadata={"staged_path": workspace_rel, "preserved_unapplied_edits": True},
                        )
                        record.workspace_path = workspace_rel
                        registry.add_or_update(record)
                        registry.save()
                    except Exception:
                        debug.log(
                            "env_fetch.preserved_registry_record_failed",
                            "preserved staged copy registry record failed (non-fatal)",
                        )
                return {
                    "ok": True,
                    "path": rel,
                    "workspace_path": workspace_rel,
                    # sha256 keeps normal fetch semantics: current project source hash,
                    # exactly what env_apply_replace expects as expected_hash.
                    "sha256": source_hash,
                    "staged_copy_preserved": True,
                    "staged_sha256": staged_hash,
                    "source_sha256": source_hash,
                    "fact": (
                        f"The staged copy `{workspace_rel}` already differs from the project source — it carries "
                        "edits (often a helper's output). env_fetch kept it instead of overwriting. Use env_diff to "
                        "review and env_apply_replace with the current source hash to apply. If the pristine source "
                        "copy is genuinely needed, retry with force=true after the staged work is applied or saved.\n"
                        "暂存副本与项目源不同(通常是已完成的修改)，本次 fetch 未覆盖；先 diff/apply，确需原始副本再 force=true。"
                    ),
                    "suggested_next_tools": ["env_diff", "env_apply_replace"],
                }
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
    result = {"ok": True, "path": rel, "workspace_path": manifest["files"][rel]["workspace_path"], "sha256": digest, "size": size}
    hint = _env_fetch_helper_handoff_hint(rel, str(result["workspace_path"]))
    if hint:
        result["next_action_instruction"] = hint
    return result


def _workspace_copy_path(workspace_dir: str, rel_path: str, workspace_path: str = "") -> Path:
    if workspace_path:
        from app.llm.tools.workspace_paths import _safe_resolve
        return Path(_safe_resolve(workspace_dir, workspace_path))
    return _workspace_env_path(workspace_dir, rel_path)


def _handle_diff(workspace_dir: str, args: dict) -> dict:
    if not workspace_dir:
        return {"ok": False, "error": "workspace is required for env_diff"}
    raw_path = str(args.get("path") or "")
    workspace_path_arg = str(args.get("workspace_path") or "")
    try:
        src = _resolve_env_path(raw_path, must_exist=True)
    except FileNotFoundError:
        target = _resolve_env_path(raw_path, must_exist=None)
        rel_missing = _rel_to_root(target)
        dst = _workspace_copy_path(workspace_dir, rel_missing, workspace_path_arg)
        try:
            workspace_root = Path(workspace_dir).resolve()
            workspace_path = str(dst.resolve().relative_to(workspace_root)).replace("\\", "/")
        except Exception:
            workspace_path = workspace_path_arg or f"_env/{rel_missing}"
        workspace_exists = dst.exists() and dst.is_file()
        result = {
            "ok": True,
            "diff_available": False,
            "fact_kind": "env_diff_project_target_missing",
            "path": rel_missing,
            "project_file_exists": False,
            "workspace_path": workspace_path,
            "workspace_path_exists": workspace_exists,
            "fact": (
                f"Project file `{rel_missing}` does not exist. env_diff is for comparing an existing project file "
                "with a staged replacement. If the active task needs this absent project file to be created, "
                "env_apply_create is the create-path tool; if the target path is wrong, choose the correct project path."
            ),
            "summary_zh": "项目目标文件不存在；env_diff 只用于既有文件替换前对比，新文件候选属于 create 路径。",
            "possible_next_tools": ["env_apply_create", "env_search", "env_list_tree"],
        }
        if workspace_exists:
            result["staged_candidate_fact"] = (
                f"Staged workspace file `{workspace_path}` exists. It can be inspected before the model decides "
                "whether to create the absent project file from that staged candidate."
            )
            result["recovery_facts"] = {
                "matching_tool_shape": "env_apply_create",
                "tool": "env_apply_create",
                "arguments": {
                    "path": rel_missing,
                    "workspace_path": workspace_path,
                },
            }
        return result
    if not src.is_file():
        return {"ok": False, "error": "source is not a file", "path": _rel_to_root(src)}
    rel = _rel_to_root(src)
    dst = _workspace_copy_path(workspace_dir, rel, workspace_path_arg)
    if not dst.exists() or not dst.is_file():
        return {"ok": False, "error": "workspace copy not found", "workspace_path": str(dst)}
    source_hash = _sha256(src)
    workspace_hash = _sha256(dst)
    try:
        workspace_root = Path(workspace_dir).resolve()
        workspace_path = str(dst.resolve().relative_to(workspace_root)).replace("\\", "/")
    except Exception:
        workspace_path = str(args.get("workspace_path") or f"_env/{rel}").replace("\\", "/")
    try:
        manifest = _load_manifest(workspace_dir)
        manifest["files"][rel] = {
            "source_path": rel,
            "workspace_path": workspace_path,
            "sha256": source_hash,
            "workspace_sha256": workspace_hash,
            "size": src.stat().st_size,
            "diff_observed_at": time.time(),
            "source": "diff_observed",
        }
        _save_manifest(workspace_dir, manifest)
    except Exception:
        pass
    if not _looks_text(src) or not _looks_text(dst):
        return {
            "ok": True,
            "path": rel,
            "binary": True,
            "source_sha256": source_hash,
            "workspace_sha256": workspace_hash,
            "changed": source_hash != workspace_hash,
        }
    old = _read_text(src).splitlines()
    new = _read_text(dst).splitlines()
    diff = "\n".join(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
    max_chars = max(1000, min(int(args.get("max_chars", 30000) or 30000), 100000))
    result = {
        "ok": True,
        "path": rel,
        "changed": old != new,
        "source_sha256": source_hash,
        "workspace_sha256": workspace_hash,
        "diff": diff[:max_chars],
        "truncated": len(diff) > max_chars,
    }
    if len(diff) > max_chars and workspace_dir:
        saved_path = write_tool_output_spill(
            root_dir=workspace_dir,
            tool_name="env_diff",
            label="diff",
            text=diff,
        )
        result.update({
            "diff_truncated": True,
            "diff_original_chars": len(diff),
            "diff_full_saved_path": saved_path,
            "output_truncated": True,
            "tool_result_truncated": True,
            "visible_excerpt_policy": (
                f"Full diff was saved at `{saved_path}` (`diff_full_saved_path`); "
                "only the head excerpt is returned."
            ),
        })
    return result


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
        if expected != current_hash:
            return {
                "ok": False,
                "error": (
                    "env_apply_replace needs an expected_hash that matches the current project file. If a helper already "
                    "produced the staged `_env/...` output, do not refetch over it; inspect/diff the staged output and "
                    "use the current project file sha256 as expected_hash. If the current source changed, re-read and "
                    "merge before applying.\n\n"
                    "应用需要 expected_hash 匹配当前真实项目文件；helper 已产出暂存文件时不要用 env_fetch 覆盖它。"
                ),
                "path": rel,
                "expected_hash": expected,
                "current_hash": current_hash,
                "suggested_next_tools": ["env_read", "env_diff", "env_apply_replace"],
            }
        manifest_entry = {
            "source_path": rel,
            "workspace_path": str(args.get("workspace_path") or f"_env/{rel}").replace("\\", "/"),
            "sha256": current_hash,
            "size": src.stat().st_size,
            "baseline_observed_at": time.time(),
            "source": "apply_current_hash_baseline",
        }
        manifest.setdefault("files", {})[rel] = manifest_entry
        _save_manifest(workspace_dir, manifest)
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
    apply_workspace_path = workspace_path_arg or f"_env/{rel}"
    provenance_guard = _env_apply_provenance_guard(
        workspace_dir,
        workspace_path=apply_workspace_path,
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
        workspace_path=apply_workspace_path,
        replacing=True,
        sha256=new_hash,
    )
    return {
        "ok": True,
        "action": "env_apply_replace",
        "path": rel,
        "new_sha256": new_hash,
        "backup_workspace_path": str(backup.relative_to(Path(workspace_dir).resolve())).replace("\\", "/"),
        "backup_project_path": None,
        # 2026-06-10: the loop mirrors successful applies into agent_state as
        # ready artifacts automatically. Without this fact the main model spent
        # a full turn calling agent_state register_artifact for the same path
        # (t4-cross-repo-migration 20260609_113326, ~14s wasted).
        "artifact_registered": True,
        "artifact_fact": (
            "This applied file is auto-recorded as a ready artifact with apply evidence; "
            "a separate agent_state register_artifact call for this path is redundant.\n"
            "应用成功的文件已自动登记为 ready 产物，无需再调 agent_state 注册。"
        ),
        "acceptance_fact": _project_apply_acceptance_fact(
            "env_apply_replace",
            rel,
            helper_owned=_staged_apply_source_is_helper_owned(workspace_dir, apply_workspace_path),
        ),
    }


def _stage_rejected_create_candidate(
    workspace_dir: str,
    *,
    target_rel: str,
    args: dict,
) -> dict:
    """Persist candidate content from a rejected create call for optional replace.

    The create call still fails when the target exists. This only preserves the
    already-supplied candidate as a workspace file so a later model step can
    inspect, diff, or apply it without re-emitting the same content.
    """
    if not workspace_dir:
        return {}
    workspace_root = Path(workspace_dir).resolve()
    workspace_path_arg = str(args.get("workspace_path") or "").strip()
    source: Path | None = None
    candidate_workspace_path = ""
    if workspace_path_arg:
        try:
            source = _workspace_copy_path(workspace_dir, target_rel, workspace_path_arg)
            if source.is_file():
                candidate_workspace_path = str(source.resolve().relative_to(workspace_root)).replace("\\", "/")
        except Exception:
            source = None
    if source is None and "content" in args:
        content = str(args.get("content") or "")
        safe_rel = target_rel.replace("\\", "/").lstrip("/").replace("..", "__")
        candidate = workspace_root / "_env" / ".pending_replacements" / safe_rel
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        source = candidate
        candidate_workspace_path = str(candidate.relative_to(workspace_root)).replace("\\", "/")
    if source is None or not source.is_file():
        return {}
    try:
        candidate_hash = _sha256(source)
        candidate_size = source.stat().st_size
    except OSError:
        return {}
    return {
        "candidate_preserved": True,
        "candidate_workspace_path": candidate_workspace_path,
        "candidate_sha256": candidate_hash,
        "candidate_size": candidate_size,
        "candidate_preservation_fact": (
            f"Fact: env_apply_create rejected the create because `{target_rel}` already exists, but the supplied "
            f"candidate content/source was preserved at `{candidate_workspace_path}`. The project file was not changed. "
            "If the active task requires replacement, inspect or diff this candidate and call env_apply_replace with "
            "expected_hash equal to current_hash."
        ),
        "summary_zh": "目标已存在；本次候选内容已保存到工作区，真实项目文件未改变，可由模型决定是否 diff/replace。",
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
    try:
        target = _resolve_env_path(raw_path, must_exist=False)
    except FileExistsError:
        target = _resolve_env_path(raw_path, must_exist=None)
    if target.exists():
        rel_existing = _rel_to_root(target)
        result = {
            "ok": False,
            "error": "target already exists",
            "error_kind": "env_apply_create_target_exists",
            "path": rel_existing,
            "current_hash": _sha256(target) if target.is_file() else "",
            "existing_file_fact": (
                f"Fact: project file `{rel_existing}` already exists. env_apply_create only creates absent files. "
                "If the active task requires changing this file, env_apply_replace is the matching replacement shape and preserves a backup. "
                "Deleting the file first is not required for replacement and can temporarily remove verifier-visible output."
            ),
            "recovery_facts": {
                "matching_tool_shape": "env_apply_replace",
                "existing_path": rel_existing,
                "backup_preserved": True,
                "candidate_tools": ["env_diff", "env_apply_replace", "env_run"],
            },
            "suggested_tools": ["env_diff", "env_apply_replace", "env_run"],
            "summary_zh": "目标文件已存在；如需修改用 env_apply_replace（会备份），不需要先删除验收可见产物。",
        }
        result.update(_stage_rejected_create_candidate(workspace_dir, target_rel=rel_existing, args=args))
        return result
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
        direct_content_guard = _env_direct_content_create_guard(rel_path, content, workspace_dir=workspace_dir)
        if direct_content_guard is not None:
            return direct_content_guard
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
    return {
        "ok": True,
        "action": "env_apply_create",
        "path": rel_created,
        "sha256": created_hash,
        "source": source_kind,
        "artifact_registered": True,
        "artifact_fact": (
            "This created file is auto-recorded as a ready artifact with apply evidence; "
            "a separate agent_state register_artifact call for this path is redundant.\n"
            "创建成功的文件已自动登记为 ready 产物，无需再调 agent_state 注册。"
        ),
        "acceptance_fact": _project_apply_acceptance_fact(
            "env_apply_create",
            rel_created,
            helper_owned=_staged_apply_source_is_helper_owned(workspace_dir, workspace_path),
        ),
    }


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
    if re.search(
        r"\b(?:python3|python|py)(?:\.(?:exe|cmd|bat))?\s+(?:-[A-Za-z0-9]+\s+)*-c\s+",
        command,
        re.IGNORECASE,
    ) and any(
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


def _node_tool_paths(project_root: Path) -> tuple[Path | None, Path | None]:
    """Return a usable Node executable and shared node_modules directory if available."""
    home = Path.home()
    node_candidates: list[Path] = []
    for env_name in ("CODEX_NODE_EXE", "CLAWBENCH_NODE_EXE"):
        env_value = os.environ.get(env_name)
        if env_value:
            node_candidates.append(Path(env_value))
    node_candidates.extend(
        [
            home
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "node"
            / "bin"
            / ("node.exe" if sys.platform == "win32" else "node"),
            Path(sys.executable).resolve().parent / ("node.exe" if sys.platform == "win32" else "node"),
            project_root / "node" / "bin" / ("node.exe" if sys.platform == "win32" else "node"),
        ]
    )
    node_exe = next((path for path in node_candidates if path.is_file()), None)

    module_candidates: list[Path] = []
    env_node_path = os.environ.get("NODE_PATH") or os.environ.get("CLAWBENCH_NODE_PATH")
    if env_node_path:
        module_candidates.extend(Path(part) for part in env_node_path.split(os.pathsep) if part)
    module_candidates.extend(
        [
            home
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "node"
            / "node_modules",
            project_root / "node_modules",
        ]
    )
    node_modules = next((path for path in module_candidates if path.is_dir()), None)
    return node_exe, node_modules


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


def _env_run_project_snapshot(root: Path, *, max_files: int = 5000) -> dict[str, tuple[int, int]] | None:
    skip_dirs = {
        ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
        ".pytest_cache", ".openclaw",
    }
    snapshot: dict[str, tuple[int, int]] = {}
    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            cur = Path(current)
            for name in files:
                path = cur / name
                try:
                    rel = path.resolve().relative_to(root.resolve()).as_posix()
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[rel] = (int(stat.st_mtime_ns), int(stat.st_size))
                if len(snapshot) > max_files:
                    return None
    except OSError:
        return None
    return snapshot


def _env_run_project_mutation_fact(
    before: dict[str, tuple[int, int]] | None,
    after: dict[str, tuple[int, int]] | None,
) -> dict | None:
    if before is None or after is None:
        return None
    created = sorted(path for path in after.keys() - before.keys() if not Path(path).name.startswith(".env_run_"))
    modified = sorted(
        path for path in before.keys() & after.keys()
        if before.get(path) != after.get(path) and not Path(path).name.startswith(".env_run_")
    )
    if not created and not modified:
        return None
    shown_created = created[:12]
    shown_modified = modified[:12]
    return {
        "kind": "env_run_project_mutation_fact",
        "created_project_files": shown_created,
        "modified_project_files": shown_modified,
        "created_truncated": len(created) > len(shown_created),
        "modified_truncated": len(modified) > len(shown_modified),
        "fact": (
            "This env_run changed the real project tree. env_run is an execution tool, so these files are now project-visible facts. "
            "For future project artifact creation or replacement, env_apply_create/env_apply_replace or declared helper outputs provide "
            "clearer ownership, provenance, and apply evidence."
        ),
        "summary_zh": "本次 env_run 改动了真实项目树；后续项目产物创建/替换优先使用 env_apply 或 helper 声明产物以保留归属和证据。",
        "possible_next_tools": ["env_read", "env_run", "env_apply_create", "env_apply_replace", "delegate"],
    }


async def _handle_run(args: dict, *, workspace_dir: str = "") -> dict:
    env_ctx = current_environment()
    if env_ctx is None:
        return {"ok": False, "error": "environment context is required"}
    command = str(args.get("command") or "").strip()
    python_code = str(args.get("python_code") or "")
    if command and python_code.strip():
        return {
            "ok": False,
            "error_kind": "both_command_and_python_code",
            "error": "Provide either command or python_code, not both.\n\ncommand 与 python_code 二选一。",
            "FIX_HINT": _env_run_usage_hint("both_command_and_python_code"),
        }
    if not command and not python_code.strip():
        return {
            "ok": False,
            "error_kind": "missing_execution_body",
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
    env["ENV_PROJECT_ROOT"] = str(Path(env_ctx.root_dir).resolve())
    project_root = Path(__file__).resolve().parents[3]
    node_exe, node_modules = _node_tool_paths(project_root)
    toolchain_bins = [
        Path(sys.executable).resolve().parent,
        project_root / "mingw64" / "bin",
    ]
    if node_exe is not None:
        toolchain_bins.insert(0, node_exe.parent)
    existing_path = env.get("PATH", "")
    extra_path = [
        str(path)
        for path in toolchain_bins
        if path.exists() and str(path) not in existing_path
    ]
    if extra_path:
        env["PATH"] = os.pathsep.join(extra_path + [existing_path])
    if node_modules is not None:
        existing_node_path = env.get("NODE_PATH", "")
        node_path_parts = [str(node_modules)]
        pnpm_public_root = node_modules / ".pnpm" / "node_modules"
        if pnpm_public_root.is_dir():
            node_path_parts.append(str(pnpm_public_root))
        if existing_node_path:
            node_path_parts.append(existing_node_path)
        env["NODE_PATH"] = os.pathsep.join(dict.fromkeys(node_path_parts))
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
    project_root = Path(env_ctx.root_dir).resolve()
    project_snapshot_before = _env_run_project_snapshot(project_root)
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
            "stdout": stdout,
            "stderr": stderr,
            "stdout_facts": _stream_text_facts(stdout),
            "stderr_facts": _stream_text_facts(stderr),
            "truncated": False,
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
            "stdout": stdout,
            "stderr": stderr,
            "stdout_facts": _stream_text_facts(stdout),
            "stderr_facts": _stream_text_facts(stderr),
            "truncated": False,
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
        staged_path_fact = _env_run_staged_path_fact(
            command,
            project_cwd=cwd,
            workspace_dir=workspace_dir,
            python_code_used=bool(temp_script_path),
            source_text=python_code if temp_script_path else "",
        )
        if staged_path_fact:
            result["env_run_staged_path_fact"] = staged_path_fact
        acceptance_failure_fact = _env_run_acceptance_failure_fact(
            command,
            stdout,
            stderr,
            proc.returncode,
        )
        if acceptance_failure_fact:
            result["acceptance_failure_fact"] = acceptance_failure_fact
        python_data_file_fact = _env_run_python_data_file_fact(
            command,
            python_code_used=bool(temp_script_path),
        )
        if python_data_file_fact:
            result["python_launcher_data_file_fact"] = python_data_file_fact
        python_non_python_script_fact = _env_run_python_non_python_script_fact(
            command,
            python_code_used=bool(temp_script_path),
        )
        if python_non_python_script_fact:
            result["python_launcher_non_python_script_fact"] = python_non_python_script_fact
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
        followup_fact = _env_run_empty_output_followup_fact(
            command,
            python_code_used=bool(temp_script_path),
        )
        if followup_fact:
            result["empty_output_followup_fact"] = followup_fact
    elif result["ok"] and (
        result["stdout_facts"]["trailing_blank_lines"] > 0
        or result["stdout_facts"]["newline_counts"]["crlf"] > 0
        or result["stdout_facts"]["newline_counts"]["cr"] > 0
    ):
        result["stdout_exact_text_warning"] = (
            "stdout contains exact-text details that can affect expected-file comparisons: "
            f"newline_counts={result['stdout_facts']['newline_counts']}, "
            f"trailing_blank_lines={result['stdout_facts']['trailing_blank_lines']}. "
            "Exact stdout checks can fail even when the visible nonblank lines look identical. Follow the verifier's comparison semantics: "
            "use text comparison for text stdout checks, and use repr/bytes only when byte-level output is explicitly required.\n\n"
            "stdout 存在会影响精确文本验收的换行/尾部空行事实；按验证器语义比较，只有明确要求字节级输出时才按 repr/字节处理。"
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
        result["python_code_project_root_fact"] = (
            "env_run python_code is executed from a system temporary script file. "
            "`__file__` points to that temp script, not the project. The subprocess cwd is the env_run cwd, "
            "and ENV_PROJECT_ROOT contains the real project root path.\n\n"
            "python_code 的 __file__ 指向系统临时脚本，不是项目文件；项目根请用当前 cwd 或 ENV_PROJECT_ROOT。"
        )
        result["script_deleted"] = False
        try:
            temp_script_path.unlink(missing_ok=True)
            result["script_deleted"] = not temp_script_path.exists()
        except OSError as e:
            result["script_delete_error"] = str(e)
    mutation_fact = _env_run_project_mutation_fact(
        project_snapshot_before,
        _env_run_project_snapshot(project_root),
    )
    if mutation_fact is not None:
        result["project_mutation_fact"] = mutation_fact
        result["project_mutations"] = {
            "created": mutation_fact.get("created_project_files", []),
            "modified": mutation_fact.get("modified_project_files", []),
        }
    spill_root = workspace_dir or str(project_root)
    spill_text_field(
        result,
        root_dir=spill_root,
        tool_name="env_run",
        field="stdout",
        text=stdout,
        visible_chars=ENV_RUN_STDOUT_VISIBLE_CHARS,
    )
    spill_text_field(
        result,
        root_dir=spill_root,
        tool_name="env_run",
        field="stderr",
        text=stderr,
        visible_chars=ENV_RUN_STDERR_VISIBLE_CHARS,
    )
    if isinstance(result.get("partial_stdout"), str):
        spill_text_field(
            result,
            root_dir=spill_root,
            tool_name="env_run",
            field="partial_stdout",
            text=str(result.get("partial_stdout") or ""),
            visible_chars=4000,
        )
    if isinstance(result.get("partial_stderr"), str):
        spill_text_field(
            result,
            root_dir=spill_root,
            tool_name="env_run",
            field="partial_stderr",
            text=str(result.get("partial_stderr") or ""),
            visible_chars=4000,
        )
    result["truncated"] = bool(result.get("output_truncated"))
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
