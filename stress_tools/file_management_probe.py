from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.filesystem import FileRegistry
from app.core.filesystem.models import FileKind, FileStatus, Visibility
from app.core.runtime_mode import EnvironmentContext, runtime_context
from app.llm.tools import environment
from app.llm.tools.delegate_actions import (
    _annotate_source_count_hints_from_manifest,
    _auto_fetch_environment_workspace_refs,
    _normalize_environment_output_paths_from_manifest,
)
from app.llm.tools.delegate_copyback import _copy_results_to_main


RUN_ROOT = ROOT / "stress_tools" / "runs" / ("file_probe_" + time.strftime("%Y%m%d_%H%M%S"))


def _tree_stats(path: Path) -> dict:
    suffix_counts: dict[str, int] = {}
    total_files = 0
    total_bytes = 0
    max_depth = 0
    for item in path.rglob("*"):
        if any(part in {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"} for part in item.parts):
            continue
        try:
            rel = item.relative_to(path)
        except ValueError:
            continue
        max_depth = max(max_depth, len(rel.parts))
        if item.is_file():
            total_files += 1
            try:
                total_bytes += item.stat().st_size
            except OSError:
                pass
            suffix = item.suffix.lower() or "(no suffix)"
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "max_depth": max_depth,
        "top_suffixes": dict(sorted(suffix_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]),
    }


def _probe_environment(project: Path, name: str, prompt: str, kind: str = "code") -> dict:
    workspace = RUN_ROOT / name / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env = EnvironmentContext(
        root_dir=str(project.resolve()),
        archive_id=f"probe-{name}",
        group_id="probe",
        user_id="probe",
        project_key=name,
    )
    tasks = [{
        "kind": kind,
        "task_id": f"{name}_task",
        "prompt": prompt,
        "expected_outputs": ["report.md"] if kind in {"code", "edit"} else [],
    }]
    with runtime_context("environment", env):
        fetch = _auto_fetch_environment_workspace_refs(str(workspace), tasks)
        _normalize_environment_output_paths_from_manifest(str(workspace), tasks)
        _annotate_source_count_hints_from_manifest(str(workspace), tasks)
    inventory = workspace / "_env" / "project_inventory.md"
    manifest = workspace / "_env" / ".resource_manifest.json"
    manifest_data = {}
    if manifest.is_file():
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "name": name,
        "project": str(project),
        "tree_stats": _tree_stats(project),
        "fetch": fetch,
        "task_after": tasks[0],
        "inventory_exists": inventory.is_file(),
        "inventory_size": inventory.stat().st_size if inventory.is_file() else 0,
        "manifest_exists": manifest.is_file(),
        "manifest_summary": manifest_data.get("summary", {}),
    }


def _probe_copyback_many_files() -> dict:
    main_ws = RUN_ROOT / "copyback" / "main"
    helper_ws = RUN_ROOT / "copyback" / "helper"
    shutil.rmtree(main_ws, ignore_errors=True)
    shutil.rmtree(helper_ws, ignore_errors=True)
    main_ws.mkdir(parents=True)
    helper_ws.mkdir(parents=True)
    declared = set()
    expected = []
    for idx in range(72):
        rel = f"generated_project/pkg/mod_{idx:02d}.py"
        path = helper_ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE = {idx}\n", encoding="utf-8")
        declared.add(rel)
        expected.append(rel)
    copied_declared, stats_declared, file_map_declared = _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "large_project",
        declared_files=declared,
        expected_outputs=expected,
        helper_kind="code",
    )

    main_ws2 = RUN_ROOT / "copyback" / "main_no_declared"
    helper_ws2 = RUN_ROOT / "copyback" / "helper_no_declared"
    shutil.rmtree(main_ws2, ignore_errors=True)
    shutil.rmtree(helper_ws2, ignore_errors=True)
    main_ws2.mkdir(parents=True)
    helper_ws2.mkdir(parents=True)
    for idx in range(72):
        (helper_ws2 / f"loose_{idx:02d}.txt").write_text("x\n", encoding="utf-8")
    copied_loose, stats_loose, file_map_loose = _copy_results_to_main(
        str(helper_ws2),
        str(main_ws2),
        "loose_project",
        declared_files=set(),
        expected_outputs=[],
        helper_kind="code",
    )
    return {
        "declared": {
            "copied_count": len(copied_declared),
            "capped": stats_declared.get("capped"),
            "env_copied_count": stats_declared.get("env_copied_count"),
            "file_map_count": len(file_map_declared),
        },
        "undeclared": {
            "copied_count": len(copied_loose),
            "capped": stats_loose.get("capped"),
            "rejected_count": stats_loose.get("rejected_count"),
            "file_map_count": len(file_map_loose),
        },
    }


def _probe_apply_errors() -> dict:
    project = RUN_ROOT / "apply" / "project"
    workspace = RUN_ROOT / "apply" / "workspace"
    project.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env = EnvironmentContext(
        root_dir=str(project.resolve()),
        archive_id="probe-apply",
        group_id="probe",
        user_id="probe",
        project_key="apply",
    )
    with runtime_context("environment", env):
        directory_target = environment._handle_apply_create(str(workspace), {"path": "contracts"})
        staged = workspace / "_env" / "src" / "main.py"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("print('hello')\n", encoding="utf-8")
        unknown_staged = environment._handle_apply_create(
            str(workspace),
            {"path": "src/main.py", "workspace_path": "_env/src/main.py"},
        )
        source = project / "existing.txt"
        source.write_text("old\n", encoding="utf-8")
        fetched = environment._handle_fetch(str(workspace), {"path": "existing.txt"})
        ready_staged = workspace / "_env" / "existing.txt"
        ready_staged.write_text("new\n", encoding="utf-8")
        registry = FileRegistry.load(scope_id=f"env:{project.resolve()}", workspace_root=workspace, project_root=project)
        ready_record = registry.find_by_workspace_path("_env/existing.txt")
        if ready_record is not None:
            ready_record.kind = FileKind.HELPER_OUTPUT
            ready_record.status = FileStatus.READY
            ready_record.visibility = Visibility.PROJECT
            ready_record.owner_task_id = "apply_ready"
            registry.add_or_update(ready_record)
            registry.save()
        provenance = workspace / "_env" / ".provenance.json"
        if provenance.exists():
            provenance.unlink()
        registry_ready_apply = environment._handle_apply_replace(
            str(workspace),
            {"path": "existing.txt", "expected_hash": fetched["sha256"]},
        )
    return {
        "directory_target": directory_target,
        "unknown_staged": unknown_staged,
        "registry_ready_apply": registry_ready_apply,
    }


def main() -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    targets = [
        (ROOT / "app", "app", "Inspect all files in this current directory and summarize the project structure, modules, and risks."),
        (ROOT / "5月雅思", "ielts", "Read all files in the current directory and organize all IELTS source material by section."),
        (ROOT / "电子231工程管理", "engineering_management", "Read all files in the current directory and organize engineering-management source material by group."),
    ]
    report = {
        "run_root": str(RUN_ROOT),
        "targets": [
            _probe_environment(project, name, prompt)
            for project, name, prompt in targets
            if project.exists()
        ],
        "copyback": _probe_copyback_many_files(),
        "apply_errors": _probe_apply_errors(),
    }
    out = RUN_ROOT / "file_management_probe_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:12000])


if __name__ == "__main__":
    main()
