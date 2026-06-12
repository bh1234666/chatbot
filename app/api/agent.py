"""Local agent client support APIs.

These endpoints manage the frontend's account/project mapping only. The chat
runtime remains `/v1/chat/stream`; a non-empty `current_dir` still enables the
extra project tools, while an empty directory behaves like normal chat.
"""
from __future__ import annotations

import difflib
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from app.core.environment_projects import (
    list_environment_projects,
    resolve_agent_project,
)
from app.core.file_preview import preview_file
from app.llm.tools.environment import _augment_pytest_command
from app.llm.tools.command_risk import analyze_command
from app.llm.tools.output_spill import spill_text_field, write_tool_output_spill
from app.memory import archive as archive_dao
from app.memory import bot_config
from app.memory import persona_files
from app.schemas.api import (
    AgentProjectCreateRequest,
    AgentProjectResponse,
    AgentProjectUpdateRequest,
)


router = APIRouter(prefix="/v1/agent", tags=["agent"])

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", ".mypy_cache"}
TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".css", ".scss", ".html", ".xml", ".csv", ".tsv",
    ".log", ".sql", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".sh",
    ".ps1", ".bat", ".cmd",
}


async def _ensure_bot_persona(archive_id: str) -> None:
    if await archive_dao.get_persona_full(archive_id):
        return
    pf = persona_files.load_persona("environment")
    if pf:
        await archive_dao.upsert_persona(archive_id, pf.content)


async def _activate_project(entry: dict) -> None:
    await _ensure_bot_persona(entry["archive_id"])
    await bot_config.join_group(
        entry["group_id"],
        entry["archive_id"],
        entry.get("project_name") or "bot",
        "bot",
    )


def _response(entry: dict) -> AgentProjectResponse:
    return AgentProjectResponse(**{
        "user_id": entry.get("user_id", ""),
        "project_key": entry.get("project_key", ""),
        "archive_id": entry.get("archive_id", ""),
        "group_id": entry.get("group_id", ""),
        "root_dir": entry.get("root_dir", ""),
        "project_name": entry.get("project_name", ""),
        "created_at": entry.get("created_at", ""),
        "last_seen_at": entry.get("last_seen_at", ""),
    })


def _project_entry(user_id: str, project_id: str) -> dict:
    current = next(
        (item for item in list_environment_projects(user_id) if item.get("project_key") == project_id),
        None,
    )
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if not current.get("root_dir"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project has no bound directory")
    root = Path(current["root_dir"]).resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project directory not found")
    return current


def _resolve_project_path(root_dir: str, rel_path: str = ".", *, must_exist: bool = True) -> Path:
    root = Path(root_dir).resolve()
    target = (root / (rel_path or ".")).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path escapes project directory") from e
    if must_exist and not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "path not found")
    return target


def _looks_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def _rel(root: Path, path: Path) -> str:
    if path == root:
        return "."
    return str(path.relative_to(root)).replace("\\", "/")


@router.get("/projects", response_model=list[AgentProjectResponse])
async def list_projects(user_id: str = "") -> list[AgentProjectResponse]:
    return [_response(item) for item in list_environment_projects(user_id)]


@router.post("/projects", response_model=AgentProjectResponse, status_code=201)
async def create_project(req: AgentProjectCreateRequest) -> AgentProjectResponse:
    try:
        entry = await resolve_agent_project(
            user_id=req.user_id,
            current_dir=req.current_dir,
            project_id=req.project_id,
            title=req.title,
            archive_id=req.archive_id,
            group_id=req.group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await _activate_project(entry)
    return _response(entry)


@router.patch("/projects/{project_id}", response_model=AgentProjectResponse)
async def update_project(project_id: str, req: AgentProjectUpdateRequest, user_id: str) -> AgentProjectResponse:
    current = next(
        (item for item in list_environment_projects(user_id) if item.get("project_key") == project_id),
        None,
    )
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    try:
        entry = await resolve_agent_project(
            user_id=user_id,
            current_dir=current.get("root_dir", "") if req.current_dir is None else req.current_dir,
            project_id=project_id,
            title=current.get("project_name", "") if req.title is None else req.title,
            archive_id=current.get("archive_id", ""),
            group_id=current.get("group_id", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await _activate_project(entry)
    return _response(entry)


@router.get("/projects/{project_id}/tree")
async def project_tree(
    project_id: str,
    user_id: str,
    path: str = ".",
    max_depth: int = 3,
    limit: int = 500,
) -> dict:
    entry = _project_entry(user_id, project_id)
    root = Path(entry["root_dir"]).resolve()
    base = _resolve_project_path(entry["root_dir"], path)
    if not base.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path is not a directory")
    max_depth = max(0, min(int(max_depth or 3), 8))
    limit = max(1, min(int(limit or 500), 2000))
    items = []
    root_depth = len(base.parts)
    for current, dirs, files in os.walk(base):
        cur = Path(current)
        depth = len(cur.parts) - root_depth
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS]
        if depth >= max_depth:
            dirs[:] = []
        for d in dirs:
            p = cur / d
            items.append({"path": _rel(root, p), "type": "dir"})
            if len(items) >= limit:
                return {"ok": True, "root": str(root), "path": _rel(root, base), "items": items, "truncated": True}
        for f in sorted(files):
            p = cur / f
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            items.append({"path": _rel(root, p), "type": "file", "size": size})
            if len(items) >= limit:
                return {"ok": True, "root": str(root), "path": _rel(root, base), "items": items, "truncated": True}
    return {"ok": True, "root": str(root), "path": _rel(root, base), "items": items, "truncated": False}


@router.get("/projects/{project_id}/file")
async def project_file(project_id: str, user_id: str, path: str, max_chars: int = 120000) -> dict:
    entry = _project_entry(user_id, project_id)
    target = _resolve_project_path(entry["root_dir"], path)
    if not target.is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path is not a file")
    return preview_file(target, max_chars=max_chars)


@router.get("/projects/{project_id}/search")
async def project_search(
    project_id: str,
    user_id: str,
    query: str,
    path: str = ".",
    regex: bool = False,
    limit: int = 200,
) -> dict:
    import re

    entry = _project_entry(user_id, project_id)
    root = Path(entry["root_dir"]).resolve()
    base = _resolve_project_path(entry["root_dir"], path)
    if not query:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query is required")
    limit = max(1, min(int(limit or 200), 500))
    pattern = re.compile(query) if regex else None
    files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file() and _looks_text(p)]
    matches = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            hit = bool(pattern.search(line)) if pattern else query.lower() in line.lower()
            if not hit:
                continue
            matches.append({"path": _rel(root, file), "line": line_no, "text": line[:500]})
            if len(matches) >= limit:
                return {"ok": True, "matches": matches, "truncated": True}
    return {"ok": True, "matches": matches, "truncated": False}


@router.get("/projects/{project_id}/diff")
async def project_diff(
    project_id: str,
    user_id: str,
    path: str,
    compare_path: str = "",
    max_chars: int = 60000,
) -> dict:
    entry = _project_entry(user_id, project_id)
    root = Path(entry["root_dir"]).resolve()
    source = _resolve_project_path(entry["root_dir"], path)
    other = _resolve_project_path(entry["root_dir"], compare_path, must_exist=True) if compare_path else source
    if not source.is_file() or not other.is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "both paths must be files")
    if not _looks_text(source) or not _looks_text(other):
        return {"ok": True, "path": _rel(root, source), "compare_path": _rel(root, other), "binary": True, "changed": source.read_bytes() != other.read_bytes()}
    old = source.read_text(encoding="utf-8", errors="replace").splitlines()
    new = other.read_text(encoding="utf-8", errors="replace").splitlines()
    diff = "\n".join(difflib.unified_diff(old, new, fromfile=f"a/{_rel(root, source)}", tofile=f"b/{_rel(root, other)}", lineterm=""))
    max_chars = max(1000, min(int(max_chars or 60000), 200000))
    result = {
        "ok": True,
        "path": _rel(root, source),
        "compare_path": _rel(root, other),
        "changed": old != new,
        "diff": diff[:max_chars],
        "truncated": len(diff) > max_chars,
    }
    if len(diff) > max_chars:
        saved_path = write_tool_output_spill(
            root_dir=str(root),
            tool_name="project_diff",
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


@router.post("/projects/{project_id}/run")
async def project_run(project_id: str, body: dict, user_id: str) -> dict:
    entry = _project_entry(user_id, project_id)
    cwd = _resolve_project_path(entry["root_dir"], str(body.get("cwd") or "."))
    if not cwd.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cwd is not a directory")
    command = str(body.get("command") or "").strip()
    if not command:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "command is required")
    timeout_sec = max(1, min(int(body.get("timeout_sec", 60) or 60), 1800))
    command = _augment_pytest_command(command, cwd)
    decision = analyze_command(command, str(cwd), is_main_thread=True)
    if not decision.allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, decision.reason)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["NVIDIA_VISIBLE_DEVICES"] = "none"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    start = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    result = {
        "ok": proc.returncode == 0 and not timed_out,
        "command": command,
        "cwd": _rel(Path(entry["root_dir"]).resolve(), cwd),
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "elapsed_sec": round(time.monotonic() - start, 3),
        "stdout": stdout,
        "stderr": stderr,
        "truncated": False,
    }
    spill_text_field(
        result,
        root_dir=entry["root_dir"],
        tool_name="project_run",
        field="stdout",
        text=stdout,
        visible_chars=50000,
    )
    spill_text_field(
        result,
        root_dir=entry["root_dir"],
        tool_name="project_run",
        field="stderr",
        text=stderr,
        visible_chars=30000,
    )
    result["truncated"] = bool(result.get("output_truncated"))
    return result
