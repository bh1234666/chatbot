"""Environment resource manifest and staging helpers for delegation."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from app.core.filesystem import index_project
from app.core.filesystem.indexer import DEFAULT_SKIP_DIRS, should_skip_rel, summarize_registry_for_manifest


CODE_TEXT_EXTS = (
    "py", "pyi", "js", "ts", "tsx", "jsx", "json", "toml", "yaml",
    "yml", "md", "txt", "csv", "tsv", "html", "htm", "css", "scss", "c",
    "h", "cpp", "hpp", "cc", "rs", "go", "java", "sql", "sh", "bat",
    "ps1", "ini", "cfg",
)
VISUAL_DOC_EXTS = (
    "png", "jpg", "jpeg", "webp", "bmp", "gif",
    "pdf", "docx", "pptx", "xlsx", "xls", "doc", "ppt",
)
MEDIA_ARCHIVE_EXTS = ("mp3", "wav", "m4a", "mp4", "mov", "webm", "flac", "zip", "rar", "7z")
KNOWN_EXTS = tuple(dict.fromkeys(CODE_TEXT_EXTS + VISUAL_DOC_EXTS + MEDIA_ARCHIVE_EXTS))
SOURCE_MATERIAL_EXTS = set(VISUAL_DOC_EXTS) | set(CODE_TEXT_EXTS)
SKIP_DIRS = {
    *DEFAULT_SKIP_DIRS,
}


def _skip_rel(path: str) -> str | None:
    norm = str(path or "").replace("\\", "/")
    if Path(norm).name.startswith("~$"):
        return "office_lock_file"
    return should_skip_rel(norm, skip_dirs=SKIP_DIRS)


def _category(rel_path: str) -> str:
    ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
    if ext in {"md", "txt", "csv", "tsv", "json", "yaml", "yml", "toml", "ini", "html", "htm", "css"}:
        return "text"
    if ext in set(CODE_TEXT_EXTS) - {"md", "txt", "csv", "tsv", "json", "toml", "yaml", "yml", "html", "htm", "css"}:
        return "code"
    if ext in {"docx", "doc", "pdf", "xlsx", "xls", "pptx", "ppt"}:
        return "office_pdf"
    if ext in {"png", "jpg", "jpeg", "webp", "bmp", "gif"}:
        return "image"
    if ext in {"mp3", "wav", "m4a", "mp4", "mov", "webm", "flac"}:
        return "audio_video"
    if ext in {"zip", "rar", "7z", "tar", "gz"}:
        return "archive"
    return "other"


def _task_blob(tasks: list[dict]) -> str:
    parts: list[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        parts.extend([
            str(task.get("kind") or ""),
            str(task.get("mode") or ""),
            str(task.get("task_id") or ""),
            str(task.get("prompt") or ""),
            " ".join(str(x) for x in (task.get("input_files") or [])),
            " ".join(str(x) for x in (task.get("expected_outputs") or [])),
        ])
    return "\n".join(parts)


def _needs_inventory(tasks: list[dict]) -> bool:
    kinds = {str(task.get("kind") or "").strip().lower() for task in tasks or [] if isinstance(task, dict)}
    if kinds & {"inventory", "summarize", "project_map", "file_summary", "read"}:
        return True
    text = _task_blob(tasks).lower()
    broad_terms = (
        "all files", "all source", "all materials", "directory", "project", "inventory",
        "read all", "summarize all", "scan", "coverage", "source map",
        "所有文件", "全部文件", "所有材料", "全部材料", "当前目录", "目录", "工程", "项目",
        "文件清单", "整理", "分四科", "逐个", "每个文件",
    )
    return any(term in text for term in broad_terms)


def _workspace_env_path(workspace_root: Path, rel_path: str) -> Path:
    normalized = Path(str(rel_path).replace("\\", "/"))
    parts = [part for part in normalized.parts if part not in ("", ".", "..")]
    return workspace_root / "_env" / Path(*parts)


def _manifest_path(workspace_root: Path) -> Path:
    return workspace_root / "_env" / ".resource_manifest.json"


def load_resource_manifest(workspace_dir: str) -> dict:
    path = _manifest_path(Path(workspace_dir).resolve())
    if not path.exists():
        return {"version": 1, "resources": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "resources": []}
    if not isinstance(data, dict):
        return {"version": 1, "resources": []}
    data.setdefault("version", 1)
    data.setdefault("resources", [])
    return data


def _write_resource_manifest(workspace_root: Path, manifest: dict) -> None:
    path = _manifest_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_project_resource_manifest(project_root: Path, workspace_root: Path, tasks: list[dict]) -> dict:
    """Build a compatibility manifest from the new unified file registry."""
    project_root = project_root.resolve()
    workspace_root = workspace_root.resolve()
    scope_id = f"env:{project_root}"
    registry = index_project(project_root, workspace_root, scope_id=scope_id, max_entries=3000, max_depth=8)
    summary = summarize_registry_for_manifest(registry)
    records = [
        record for record in registry.list_records()
        if record.file_id != "__index_summary__" and record.project_path
    ]
    total_project_files = 0
    skipped_resources: list[dict[str, str]] = []
    for current, dirs, files in os.walk(project_root):
        current_path = Path(current)
        dirs[:] = [
            name for name in dirs
            if not _skip_rel((current_path / name).relative_to(project_root).as_posix() + "/")
        ]
        for filename in files:
            rel_path = (current_path / filename).relative_to(project_root).as_posix()
            reason = _skip_rel(rel_path)
            if reason == "runtime_state":
                continue
            total_project_files += 1
            if reason:
                skipped_resources.append({"project_path": rel_path, "reason": reason})
    effective_records = [record for record in records if not _skip_rel(record.project_path)]

    resources: list[dict[str, Any]] = []
    key_paths = set(summary.get("key_candidate_paths") or [])
    for record in records[:3000]:
        ext = Path(record.project_path).suffix.lower() or "(no suffix)"
        entry = {
            "file_id": record.file_id,
            "project_path": record.project_path,
            "staged_path": f"_env/{record.project_path}",
            "category": record.category,
            "suffix": ext,
            "size": record.size,
            "sha256": record.sha256,
            "staged": _workspace_env_path(workspace_root, record.project_path).is_file(),
            "display_name": record.display_name,
        }
        if record.project_path in key_paths:
            entry["key_candidate"] = True
        resources.append(entry)

    dirs = sorted({
        parent.as_posix()
        for record in records
        for parent in [Path(record.project_path).parent]
        if parent.as_posix() not in {"", "."}
    })[:800]

    manifest = {
        "version": 2,
        "created_at": time.time(),
        "source": "file_registry",
        "project_root": str(project_root),
        "registry_path": str(registry.path),
        "resource_policy": {
            "project_path": "Path relative to the real environment root.",
            "staged_path": "Readable helper path after the main process stages the file.",
            "write_target": "Project edits and deliverables are tracked by the file registry before promotion or apply.",
        },
        "summary": {
            "listed_dirs": len(dirs),
            "listed_files": summary.get("listed_files", len(resources)),
            "total_project_files": total_project_files,
            "effective_material_files": len(effective_records),
            "skipped_low_value_files": len(skipped_resources),
            "omitted_entries": summary.get("omitted_entries", 0),
            "category_counts": summary.get("category_counts", {}),
            "suffix_counts": dict(list((summary.get("suffix_counts") or {}).items())[:80]),
            "key_candidate_paths": summary.get("key_candidate_paths", [])[:240],
        },
        "skipped_resources": skipped_resources[:300],
        "directories": dirs,
        "resources": resources,
        "task_scope": [
            {
                "task_id": str(task.get("task_id") or ""),
                "kind": str(task.get("kind") or ""),
                "input_files": list(task.get("input_files") or [])[:40] if isinstance(task, dict) else [],
            }
            for task in tasks or []
            if isinstance(task, dict)
        ],
    }
    return manifest

def write_project_inventory_markdown(workspace_root: Path, manifest: dict) -> None:
    workspace_root = workspace_root.resolve()
    inventory_path = workspace_root / "_env" / "project_inventory.md"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    summary = manifest.get("summary") or {}
    category_counts = summary.get("category_counts") or {}
    suffix_counts = summary.get("suffix_counts") or {}
    key_paths = summary.get("key_candidate_paths") or []
    dirs = manifest.get("directories") or []
    resources = manifest.get("resources") or []
    skipped_resources = manifest.get("skipped_resources") or []
    category_lines = [f"- {name}: {count}" for name, count in category_counts.items() if count]
    suffix_lines = [f"- `{suffix}`: {count}" for suffix, count in suffix_counts.items()]
    key_lines = [f"- `{path}`" for path in key_paths[:180]]
    dir_lines = [f"- `{path}/`" for path in dirs[:350]]
    file_lines = [
        f"- `{item.get('project_path')}` -> `{item.get('staged_path')}`"
        + (f" ({item.get('size')} bytes)" if item.get("size") is not None else "")
        + ("" if item.get("staged") else " [not staged]")
        for item in resources[:1800]
    ]
    content = "\n".join([
        "# Project Resource Manifest",
        "",
        "This file is an automatically supplied project inventory and resource manifest. Treat exact paths here as the source of truth before reading or requesting project files.",
        "",
        "This inventory is for locating files and planning coverage; content conclusions still require reading concrete files.",
        "该清单用于定位文件与规划覆盖，内容结论仍需读取具体文件。",
        "",
        "## Scope",
        f"- project_root: `{manifest.get('project_root', '')}`",
        f"- listed_dirs: {summary.get('listed_dirs', 0)}",
        f"- total_project_files: {summary.get('total_project_files', summary.get('listed_files', 0))}",
        f"- listed_files: {summary.get('listed_files', 0)}",
        f"- effective_material_files: {summary.get('effective_material_files', summary.get('listed_files', 0))}",
        f"- skipped_low_value_files: {summary.get('skipped_low_value_files', 0)}",
        f"- omitted_entries: {summary.get('omitted_entries', 0)}",
        "",
        "## Path Contract",
        "- `project_path` is the real project-relative path.",
        "- `staged_path` is the helper-readable path after staging.",
        "- If a needed file is marked `[not staged]`, request the exact `project_path` instead of guessing a path.",
        "",
        "Use project_path for real project paths and staged_path for helper-readable staged copies. Request exact project_path values when files are not staged.",
        "真实项目路径用 project_path，helper 读取暂存副本用 staged_path；未暂存文件按精确 project_path 请求。",
        "",
        "## Count Contract",
        "- `total_project_files` counts files in the indexed project surface after default internal/generated directory exclusions and before low-value file skips.",
        "- `listed_files` counts indexed resource files after low-value/internal skips.",
        "- `effective_material_files` excludes low-value technical files such as Office lock files.",
        "- When reporting coverage, state both counts if they differ.",
        "",
        "total_project_files 是排除默认内部/生成目录后的项目表面文件数；listed_files 是索引文件数；effective_material_files 是有效材料数；口径不一致时需同时说明。",
        "",
        "## Categories",
        *(category_lines or ["- (none)"]),
        "",
        "## Suffix Counts",
        *(suffix_lines or ["- (none)"]),
        "",
        "## Key Candidate Paths",
        *(key_lines or ["- (none detected)"]),
        "",
        "## Directories",
        *(dir_lines or ["- (none)"]),
        "",
        "## Files",
        *(file_lines or ["- (none)"]),
        "",
        "## Skipped Low-Value Files",
        *(
            [f"- `{item.get('project_path')}` ({item.get('reason')})" for item in skipped_resources[:120]]
            or ["- (none)"]
        ),
        "",
        "## Coverage Note",
        "Use this inventory for path truth and coverage planning. Read text, images, Office/PDF files, archives, media, and long materials with the matching helper before making content claims.",
        "",
        "路径与覆盖以该清单为准；具体内容需由对应 helper 读取后再下结论。",
    ])
    inventory_path.write_text(content, encoding="utf-8")


def write_resource_manifest_files(project_root: Path, workspace_root: Path, tasks: list[dict]) -> dict:
    manifest = build_project_resource_manifest(project_root, workspace_root, tasks)
    _write_resource_manifest(workspace_root, manifest)
    write_project_inventory_markdown(workspace_root, manifest)
    return manifest


def task_requested_inventory(tasks: list[dict]) -> bool:
    return _needs_inventory(tasks)


def explicit_project_refs(project_root: Path, tasks: list[dict]) -> set[str]:
    project_root = project_root.resolve()
    refs: set[str] = set()
    project_root_token = str(project_root).replace("\\", "/")
    known_exts = tuple(KNOWN_EXTS)

    def add_ref(raw_ref: str, *, from_env_prefix: bool = False) -> None:
        text = str(raw_ref or "").replace("\\", "/").strip().strip("`\"'")
        if not text:
            return
        text = text.rstrip(".,;:)]}")
        if text == "_env":
            return
        if text.startswith("_env/"):
            text = text[5:].lstrip("/")
        if project_root_token and text.lower().startswith(project_root_token.lower().rstrip("/") + "/"):
            text = text[len(project_root_token.rstrip("/")) + 1:]
        if (
            not text
            or text.startswith(("_helpers_shared/", "_delegate_", ".temp/", ".prev/"))
            or text.startswith("../")
            or "/../" in f"/{text}/"
            or (":" in text and not from_env_prefix)
        ):
            return
        source = (project_root / text).resolve()
        try:
            source.relative_to(project_root)
        except ValueError:
            return
        if source.is_file() or source.is_dir():
            refs.add(text)

    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        prompt = str(task.get("prompt") or "").replace("\\", "/")
        field_refs = []
        for key in ("input_files", "source_files", "transferred_files", "files", "expected_outputs"):
            raw = task.get(key)
            if isinstance(raw, str):
                field_refs.extend(raw.splitlines())
            elif isinstance(raw, (list, tuple, set)):
                field_refs.extend(str(x) for x in raw)
        for value in field_refs:
            add_ref(value, from_env_prefix=str(value).replace("\\", "/").startswith("_env/"))
        for match in re.finditer(
            r"_env/([^`\"'<>|\r\n]+?\.(?:" + "|".join(known_exts) + r"))",
            prompt,
            flags=re.IGNORECASE,
        ):
            add_ref("_env/" + match.group(1), from_env_prefix=True)
        for match in re.finditer(r"_env/([^\s`\"'<>|]+)", prompt):
            add_ref("_env/" + match.group(1), from_env_prefix=True)
        for match in re.finditer(
            r"(?<![\w.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_. -]+\.(?:"
            + "|".join(known_exts)
            + r"))",
            prompt,
            flags=re.IGNORECASE,
        ):
            add_ref(match.group(1))
        for match in re.finditer(r"_env/([^`\"'<>|\r\n]+?)(?:\s+(?:directory|folder|file)|[。；，、,.;:]|$)", prompt, flags=re.IGNORECASE):
            add_ref("_env/" + match.group(1).strip(), from_env_prefix=True)
        if project_root_token:
            escaped_root = re.escape(project_root_token.rstrip("/"))
            for match in re.finditer(
                escaped_root + r"/([^`\"'<>|]+?\.(?:" + "|".join(known_exts) + r"))",
                prompt,
                flags=re.IGNORECASE,
            ):
                add_ref(match.group(1), from_env_prefix=True)
        for match in re.finditer(
            r"['\"`]([^'\"`<>|]+?\.(?:" + "|".join(VISUAL_DOC_EXTS) + r"))['\"`]",
            prompt,
            flags=re.IGNORECASE,
        ):
            add_ref(match.group(1))

    blob_l = _task_blob(tasks).replace("\\", "/").lower()
    if blob_l:
        scanned = 0
        try:
            for child in project_root.rglob("*"):
                if scanned >= 8000:
                    break
                if not child.is_file():
                    continue
                scanned += 1
                rel = child.resolve().relative_to(project_root).as_posix()
                if _skip_rel(rel):
                    continue
                ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
                if ext not in known_exts:
                    continue
                rel_l = rel.lower()
                base_l = child.name.lower()
                if rel_l in blob_l or f"_env/{rel_l}" in blob_l or base_l in blob_l:
                    refs.add(rel)
        except OSError:
            pass
    return refs


def context_project_refs(project_root: Path, refs: set[str]) -> set[str]:
    project_root = project_root.resolve()
    context_refs: set[str] = set()
    for rel in (
        "pyproject.toml",
        "package.json",
        "requirements.txt",
        "README.md",
        "Makefile",
        "CMakeLists.txt",
    ):
        if (project_root / rel).is_file():
            context_refs.add(rel)
    for rel in tuple(refs):
        parent = Path(rel).parent
        if str(parent) in ("", "."):
            continue
        for name in ("__init__.py", "README.md", "conftest.py"):
            candidate = parent / name
            if (project_root / candidate).is_file():
                context_refs.add(candidate.as_posix())
    return context_refs

