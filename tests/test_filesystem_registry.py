from pathlib import Path

from app.core.filesystem import FileRegistry, PathResolver, PathZone, classify_path, index_project
from app.core.filesystem.models import FileKind, FileStatus, Visibility
from app.core.filesystem.artifacts import list_deliverable_records
from app.core.filesystem.planner import build_read_plan
from app.core.filesystem.transfers import promote_deliverable, stage_project_file
from app.llm.tools.environment_resources import build_project_resource_manifest


def test_path_resolver_round_trips_staged_project_path(tmp_path):
    resolver = PathResolver(project_root=tmp_path / "project", workspace_root=tmp_path / "workspace")

    staged = resolver.project_to_staged_path("资料/写作 sample.docx")
    project = resolver.staged_to_project_path(staged)

    assert staged == "_env/资料/写作 sample.docx"
    assert project == "资料/写作 sample.docx"


def test_path_classification_separates_project_workspace_and_staged_zones():
    staged_root = classify_path("_env/")
    staged_file = classify_path("_env/src/main.py")
    helper_shared_root = classify_path("_helpers_shared/")
    helper_shared_file = classify_path("_helpers_shared/task/evidence.txt")
    workspace_file = classify_path("analysis_outputs/report.md")
    workspace_dir = classify_path("notes/")
    project_file = classify_path("src/main.py", default_zone=PathZone.PROJECT)

    assert staged_root.zone == PathZone.STAGED_ROOT
    assert staged_root.is_directory_hint is True
    assert staged_file.zone == PathZone.STAGED_FILE
    assert staged_file.project_path == "src/main.py"
    assert helper_shared_root.zone == PathZone.HELPER_SHARED
    assert helper_shared_root.is_directory_hint is True
    assert helper_shared_file.zone == PathZone.HELPER_SHARED
    assert workspace_file.zone == PathZone.DELIVERABLE
    assert workspace_dir.zone == PathZone.WORKSPACE
    assert workspace_dir.is_directory_hint is True
    assert project_file.zone == PathZone.PROJECT
    assert project_file.project_path == "src/main.py"


def test_file_registry_indexes_unicode_project_files(tmp_path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    target = project / "五月雅思" / "口语素材.txt"
    target.parent.mkdir(parents=True)
    target.write_text("part 2 topic\n", encoding="utf-8")
    workspace.mkdir()

    registry = index_project(project, workspace, scope_id="test-scope")
    record = registry.find_by_project_path("五月雅思/口语素材.txt")

    assert record is not None
    assert record.display_name == "口语素材.txt"
    assert record.category == "text"
    assert record.sha256
    assert (workspace / ".file_registry.json").is_file()


def test_file_registry_reindex_removes_stale_project_index_records(tmp_path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    kept = project / "README.md"
    stale = project / "data" / "workspaces" / "arch" / "old.txt"
    kept.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    kept.write_text("# Demo\n", encoding="utf-8")
    stale.write_text("old runtime output\n", encoding="utf-8")
    workspace.mkdir()

    registry = index_project(project, workspace, scope_id="stale-scope")
    assert registry.find_by_project_path("README.md") is not None
    assert registry.find_by_project_path("data/workspaces/arch/old.txt") is None

    extra = registry.upsert_project_file(
        stale,
        kind=FileKind.PROJECT_SOURCE,
        status=FileStatus.INDEXED,
        visibility=Visibility.PROJECT,
        origin="project_index",
    )
    registry.save()
    assert extra.project_path == "data/workspaces/arch/old.txt"

    refreshed = index_project(project, workspace, scope_id="stale-scope")
    paths = {record.project_path for record in refreshed.list_records() if record.project_path}

    assert "README.md" in paths
    assert "data/workspaces/arch/old.txt" not in paths


def test_read_plan_splits_large_source_material_by_category(tmp_path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    for idx in range(25):
        path = project / "materials" / f"file_{idx}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content {idx}\n", encoding="utf-8")
    workspace.mkdir()

    registry = index_project(project, workspace, scope_id="read-plan")
    shards = build_read_plan(registry, max_files_per_shard=10)

    assert len(shards) == 3
    assert all(shard.category == "text" for shard in shards)
    assert sum(len(shard.project_paths) for shard in shards) == 25


def test_environment_manifest_is_registry_compatibility_view(tmp_path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    doc = project / "电子231工程管理" / "小组报告.docx"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"fake-docx")
    workspace.mkdir()

    manifest = build_project_resource_manifest(project, workspace, [{"kind": "read", "task_id": "scan"}])

    assert manifest["version"] == 2
    assert manifest["source"] == "file_registry"
    assert manifest["registry_path"].endswith(".file_registry.json")
    assert manifest["resources"][0]["project_path"] == "电子231工程管理/小组报告.docx"
    assert manifest["resources"][0]["staged_path"] == "_env/电子231工程管理/小组报告.docx"
    assert manifest["resources"][0]["category"] == "office_pdf"


def test_deliverable_registry_only_lists_explicit_deliverables(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    internal = workspace / "scratch.txt"
    deliverable = workspace / "report.docx"
    internal.write_text("internal\n", encoding="utf-8")
    deliverable.write_bytes(b"docx")
    registry = FileRegistry.load(scope_id="artifacts", workspace_root=workspace)

    registry.register_deliverable("report.docx", display_name="最终报告.docx")
    registry.save()

    listed = registry.list_records(visibility=Visibility.DELIVERABLE)
    assert [item.display_name for item in listed] == ["最终报告.docx"]


def test_stage_project_file_uses_registry_path_contract(tmp_path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    source = project / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('ok')\n", encoding="utf-8")
    workspace.mkdir()
    registry = FileRegistry.load(scope_id="stage", workspace_root=workspace, project_root=project)

    record = stage_project_file(registry, "src/main.py")

    assert record.project_path == "src/main.py"
    assert record.workspace_path == "_env/src/main.py"
    assert (workspace / "_env" / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_deliverable_view_is_registry_backed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "internal.txt").write_text("hidden\n", encoding="utf-8")
    (workspace / "final.docx").write_bytes(b"docx")
    registry = FileRegistry.load(scope_id="deliverable-view", workspace_root=workspace)

    promote_deliverable(registry, "final.docx", display_name="研究论文.docx", owner_task_id="paper")
    items = list_deliverable_records(registry)

    assert len(items) == 1
    assert items[0]["name"] == "研究论文.docx"
    assert items[0]["rel_path"] == "final.docx"


def test_env_fetch_and_apply_update_file_registry(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools import environment

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    source = project / "hello.txt"
    project.mkdir()
    workspace.mkdir()
    source.write_text("old\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        fetched = environment._handle_fetch(str(workspace), {"path": "hello.txt"})
        (workspace / fetched["workspace_path"]).write_text("new\n", encoding="utf-8")
        applied = environment._handle_apply_replace(
            str(workspace),
            {"path": "hello.txt", "expected_hash": fetched["sha256"]},
        )

    assert applied["ok"] is True
    registry = FileRegistry.load(scope_id=f"env:{project.resolve()}", workspace_root=workspace, project_root=project)
    record = registry.find_by_project_path("hello.txt")
    assert record is not None
    assert record.status == FileStatus.APPLIED
    assert record.apply_state == "replaced"
    assert record.workspace_path == "_env/hello.txt"


def test_env_apply_guard_accepts_ready_registry_without_legacy_provenance(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools import environment

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    source = project / "report.md"
    project.mkdir()
    workspace.mkdir()
    source.write_text("old\n", encoding="utf-8")
    staged = workspace / "_env" / "report.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("new\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        fetched = environment._handle_fetch(str(workspace), {"path": "report.md"})
        (workspace / "_env" / "report.md").write_text("new\n", encoding="utf-8")
        registry = FileRegistry.load(scope_id=f"env:{project.resolve()}", workspace_root=workspace, project_root=project)
        record = registry.find_by_workspace_path("_env/report.md")
        assert record is not None
        record.kind = FileKind.HELPER_OUTPUT
        record.status = FileStatus.READY
        registry.add_or_update(record)
        registry.save()
        provenance = workspace / "_env" / ".provenance.json"
        if provenance.exists():
            provenance.unlink()
        applied = environment._handle_apply_replace(
            str(workspace),
            {"path": "report.md", "expected_hash": fetched["sha256"]},
        )

    assert applied["ok"] is True
    assert source.read_text(encoding="utf-8") == "new\n"


def test_env_apply_guard_blocks_unready_registry_record(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools import environment

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    source = project / "report.md"
    project.mkdir()
    workspace.mkdir()
    source.write_text("old\n", encoding="utf-8")
    staged = workspace / "_env" / "report.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("draft\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        fetched = environment._handle_fetch(str(workspace), {"path": "report.md"})
        (workspace / "_env" / "report.md").write_text("draft\n", encoding="utf-8")
        registry = FileRegistry.load(scope_id=f"env:{project.resolve()}", workspace_root=workspace, project_root=project)
        record = registry.find_by_workspace_path("_env/report.md")
        assert record is not None
        record.kind = FileKind.HELPER_OUTPUT
        record.status = FileStatus.FAILED
        record.owner_task_id = "report_task"
        record.metadata["terminal_reason"] = "failed"
        registry.add_or_update(record)
        registry.save()
        provenance = workspace / "_env" / ".provenance.json"
        if provenance.exists():
            provenance.unlink()
        blocked = environment._handle_apply_replace(
            str(workspace),
            {"path": "report.md", "expected_hash": fetched["sha256"]},
        )

    assert blocked["ok"] is False
    assert blocked["error_kind"] == "staged_environment_file_not_ready"
    assert blocked["recovery_facts"]["same_task_id"] == "report_task"
    assert source.read_text(encoding="utf-8") == "old\n"


def test_env_apply_allows_ready_provenance_when_registry_record_is_stale(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools import environment

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    source = project / "config_loader.py"
    project.mkdir()
    workspace.mkdir()
    source.write_text("old\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        fetched = environment._handle_fetch(str(workspace), {"path": "config_loader.py"})
        staged = workspace / "_env" / "config_loader.py"
        staged.write_text("new\n", encoding="utf-8")
        registry = FileRegistry.load(scope_id=f"env:{project.resolve()}", workspace_root=workspace, project_root=project)
        record = registry.find_by_workspace_path("_env/config_loader.py")
        assert record is not None
        record.status = FileStatus.INDEXED
        record.verified = False
        registry.add_or_update(record)
        registry.save()
        environment.record_env_helper_outputs(
            str(workspace),
            task_id="fix_config",
            files=["_env/config_loader.py"],
            ok=True,
            terminal_reason="completed",
            outputs_complete=True,
            kind="code",
            mode="easy",
        )
        result = environment._handle_apply_replace(
            str(workspace),
            {
                "path": "config_loader.py",
                "workspace_path": "_env/config_loader.py",
                "expected_hash": fetched["sha256"],
            },
        )

    assert result["ok"] is True
    assert source.read_text(encoding="utf-8") == "new\n"
