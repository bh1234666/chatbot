"""Stable user/project to archive mapping for environment mode."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.memory import archive as archive_dao


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAPPING_PATH = _PROJECT_ROOT / "data" / "environment_projects.json"
_LOCK_PATH = _PROJECT_ROOT / "data" / ".environment_projects.lock"
_LOCK_STALE_SEC = 60.0
_LOCK_TIMEOUT_SEC = 10.0


def normalize_root_dir(current_dir: str) -> str:
    raw = (current_dir or "").strip().strip('"')
    if not raw:
        raise ValueError("current_dir is required")
    root = Path(raw).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"current_dir is not an existing directory: {current_dir}")
    return str(root)


def normalize_optional_root_dir(current_dir: str) -> str:
    raw = (current_dir or "").strip().strip('"')
    if not raw:
        return ""
    return normalize_root_dir(raw)


def safe_group_id(user_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", (user_id or "user").strip())
    safe = safe.replace(":", "_")
    return f"env_user_{safe[:80] or 'user'}"


def project_key_for(user_id: str, root_dir: str) -> str:
    norm = os.path.normcase(os.path.abspath(root_dir)) if root_dir else "__chat_only__"
    payload = f"{user_id or 'user'}\n{norm}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()[:16]


def list_environment_projects(user_id: str = "") -> list[dict]:
    data = _read_mapping()
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        return []
    out = []
    for entry in projects.values():
        if not isinstance(entry, dict):
            continue
        if user_id and entry.get("user_id") != user_id:
            continue
        out.append(dict(entry))
    out.sort(key=lambda item: item.get("last_seen_at") or item.get("created_at") or "", reverse=True)
    return out


async def resolve_agent_project(
    *,
    user_id: str,
    current_dir: str = "",
    project_id: str = "",
    title: str = "",
    archive_id: str = "",
    group_id: str = "",
) -> dict:
    root_dir = normalize_optional_root_dir(current_dir)
    project_key = (project_id or "").strip() or project_key_for(user_id, root_dir)
    map_key = f"{user_id or 'user'}:{project_key}"
    group_id = (group_id or "").strip() or safe_group_id(user_id)
    project_name = (title or "").strip() or (Path(root_dir).name if root_dir else "bot chat")

    data = _read_mapping()
    projects = data.setdefault("projects", {})
    entry = projects.get(map_key)
    if not isinstance(entry, dict):
        entry = {}

    resolved_archive_id = (archive_id or entry.get("archive_id") or "").strip()
    archive_row = await archive_dao.get_archive(resolved_archive_id) if resolved_archive_id else None
    if not archive_row:
        created = await archive_dao.create_archive(f"bot:{user_id or 'user'}:{project_name}")
        resolved_archive_id = created["archive_id"]
        entry.setdefault("created_at", _now_iso())
    else:
        entry.setdefault("created_at", _now_iso())

    entry.update({
        "user_id": user_id,
        "project_key": project_key,
        "archive_id": resolved_archive_id,
        "group_id": group_id,
        "root_dir": root_dir,
        "project_name": project_name,
        "last_seen_at": _now_iso(),
    })
    return _merge_project_entry(map_key, entry)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_mapping() -> dict:
    if not _MAPPING_PATH.exists():
        return {"projects": {}}
    try:
        data = json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"projects": {}}
    if not isinstance(data, dict):
        return {"projects": {}}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        data["projects"] = {}
    return data


@contextmanager
def _mapping_file_lock(timeout_sec: float = _LOCK_TIMEOUT_SEC):
    """Cross-process lock for the small environment project mapping file."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_sec
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"{os.getpid()} {time.time():.6f}\n".encode("ascii", "replace"))
        except FileExistsError:
            try:
                age = time.time() - _LOCK_PATH.stat().st_mtime
                if age > _LOCK_STALE_SEC:
                    _LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for environment project mapping lock: {_LOCK_PATH}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def _write_mapping(data: dict) -> None:
    _MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".environment_projects.",
        suffix=".json",
        dir=str(_MAPPING_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        last_error: OSError | None = None
        for attempt in range(20):
            try:
                os.replace(tmp, _MAPPING_PATH)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(min(0.05 * (attempt + 1), 0.5))
        if last_error is not None:
            raise last_error
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _merge_project_entry(map_key: str, entry: dict) -> dict:
    with _mapping_file_lock():
        data = _read_mapping()
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        current = projects.get(map_key)
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(entry)
        projects[map_key] = merged
        _write_mapping(data)
        return dict(merged)


async def resolve_environment_project(
    *,
    user_id: str,
    current_dir: str,
    project_id: str = "",
) -> dict:
    root_dir = normalize_root_dir(current_dir)
    project_key = (project_id or "").strip() or project_key_for(user_id, root_dir)
    map_key = f"{user_id or 'user'}:{project_key}"
    group_id = safe_group_id(user_id)
    project_name = Path(root_dir).name or "project"

    data = _read_mapping()
    projects = data.setdefault("projects", {})
    entry = projects.get(map_key)
    archive_id = entry.get("archive_id") if isinstance(entry, dict) else ""
    archive_row = await archive_dao.get_archive(archive_id) if archive_id else None
    if not archive_row:
        created = await archive_dao.create_archive(f"env:{user_id or 'user'}:{project_name}")
        archive_id = created["archive_id"]
        entry = {
            "user_id": user_id,
            "project_key": project_key,
            "archive_id": archive_id,
            "group_id": group_id,
            "root_dir": root_dir,
            "project_name": project_name,
            "created_at": _now_iso(),
        }
    entry.update({
        "user_id": user_id,
        "project_key": project_key,
        "archive_id": archive_id,
        "group_id": group_id,
        "root_dir": root_dir,
        "project_name": project_name,
        "last_seen_at": _now_iso(),
    })
    return _merge_project_entry(map_key, entry)
