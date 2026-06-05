"""Unified file-system core for project, helper, and deliverable files."""

from .indexer import index_project
from .models import FileKind, FileRecord, FileStatus, Visibility
from .path_resolver import PathClassification, PathResolver, PathZone, classify_path, normalize_project_path, normalize_workspace_path
from .registry import FileRegistry
from .transfers import intake_workspace_file, promote_deliverable, stage_project_file

__all__ = [
    "FileKind",
    "FileRecord",
    "FileRegistry",
    "FileStatus",
    "PathResolver",
    "PathClassification",
    "PathZone",
    "Visibility",
    "classify_path",
    "index_project",
    "intake_workspace_file",
    "normalize_project_path",
    "normalize_workspace_path",
    "promote_deliverable",
    "stage_project_file",
]
