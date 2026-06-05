"""Canonical path handling for project and workspace files."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PathZone(StrEnum):
    PROJECT = "project"
    WORKSPACE = "workspace"
    STAGED_ROOT = "staged_root"
    STAGED_FILE = "staged_file"
    HELPER_SHARED = "helper_shared"
    DELIVERABLE = "deliverable"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class PathClassification:
    original: str
    normalized: str
    zone: PathZone
    project_path: str = ""
    workspace_path: str = ""
    is_directory_hint: bool = False
    message: str = ""


def normalize_project_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip().strip('"').strip("'")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("_env/"):
        text = text[5:]
    parts: list[str] = []
    for part in Path(text).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            raise ValueError(f"path traversal detected: {path!r}")
        parts.append(part)
    return "/".join(parts)


def normalize_workspace_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip().strip('"').strip("'")
    while text.startswith("./"):
        text = text[2:]
    parts: list[str] = []
    for part in Path(text).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            raise ValueError(f"path traversal detected: {path!r}")
        parts.append(part)
    return "/".join(parts)


def classify_path(path: str, *, default_zone: PathZone = PathZone.WORKSPACE) -> PathClassification:
    original = str(path or "")
    norm = normalize_workspace_path(original)
    is_dir_hint = original.replace("\\", "/").rstrip().endswith("/") or norm in {"", "_env", "_helpers_shared", "_shared"}
    if norm == "_env":
        return PathClassification(
            original=original,
            normalized=norm,
            zone=PathZone.STAGED_ROOT,
            workspace_path=norm,
            is_directory_hint=True,
            message="`_env/` is the staged project-root directory, not a file.",
        )
    if norm.startswith("_env/"):
        return PathClassification(
            original=original,
            normalized=norm,
            zone=PathZone.STAGED_FILE,
            project_path=normalize_project_path(norm),
            workspace_path=norm,
            is_directory_hint=is_dir_hint,
            message="This is a staged workspace copy of a project path.",
        )
    if norm in {"_helpers_shared", "_shared"} or norm.startswith("_helpers_shared/") or norm.startswith("_shared/"):
        return PathClassification(
            original=original,
            normalized=norm,
            zone=PathZone.HELPER_SHARED,
            workspace_path=norm,
            is_directory_hint=is_dir_hint,
            message="This path belongs to the helper shared workspace area.",
        )
    if norm.startswith(("deliverables/", "artifacts/", "analysis_outputs/")):
        return PathClassification(
            original=original,
            normalized=norm,
            zone=PathZone.DELIVERABLE,
            workspace_path=norm,
            is_directory_hint=is_dir_hint,
            message="This path is a user-facing workspace artifact or deliverable candidate.",
        )
    if default_zone == PathZone.PROJECT:
        return PathClassification(
            original=original,
            normalized=norm,
            zone=PathZone.PROJECT,
            project_path=normalize_project_path(norm),
            is_directory_hint=is_dir_hint,
            message="This path is interpreted as project-relative.",
        )
    return PathClassification(
        original=original,
        normalized=norm,
        zone=default_zone,
        workspace_path=norm,
        is_directory_hint=is_dir_hint,
        message="This path is interpreted as chat-workspace-relative.",
    )


class PathResolver:
    def __init__(self, *, project_root: str | Path | None = None, workspace_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root).resolve() if project_root else None
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None

    def project_to_staged_path(self, project_path: str) -> str:
        rel = normalize_project_path(project_path)
        return f"_env/{rel}" if rel else "_env"

    def classify(self, path: str, *, default_zone: PathZone = PathZone.WORKSPACE) -> PathClassification:
        return classify_path(path, default_zone=default_zone)

    def staged_to_project_path(self, staged_path: str) -> str:
        text = str(staged_path or "").replace("\\", "/").strip()
        if text == "_env":
            return ""
        if not text.startswith("_env/"):
            raise ValueError(f"not an environment staged path: {staged_path!r}")
        return normalize_project_path(text[5:])

    def safe_project_path(self, project_path: str, *, must_exist: bool = False) -> Path:
        if self.project_root is None:
            raise ValueError("project_root is not configured")
        rel = normalize_project_path(project_path)
        target = (self.project_root / rel).resolve()
        try:
            target.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError(f"project path escapes root: {project_path!r}") from exc
        if must_exist and not target.exists():
            raise FileNotFoundError(rel)
        return target

    def safe_workspace_path(self, workspace_path: str, *, must_exist: bool = False) -> Path:
        if self.workspace_root is None:
            raise ValueError("workspace_root is not configured")
        text = str(workspace_path or "").replace("\\", "/").strip()
        if Path(text).is_absolute():
            target = Path(text).resolve()
        else:
            target = (self.workspace_root / text).resolve()
        try:
            target.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"workspace path escapes root: {workspace_path!r}") from exc
        if must_exist and not target.exists():
            raise FileNotFoundError(text)
        return target
