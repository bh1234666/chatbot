from __future__ import annotations

from pathlib import Path

from app.core.filesystem import FileRegistry
from app.core.filesystem.models import FileKind, Visibility
from app.llm.tools.delegate_copyback import _copy_results_to_main
from app.llm.tools import workspace as ws_tool


def test_environment_copyback_skips_unexpected_new_env_files(tmp_path):
    helper_ws = tmp_path / "helper"
    main_ws = tmp_path / "main"
    helper_env = helper_ws / "_env" / "app"
    main_env = main_ws / "_env" / "app"
    helper_env.mkdir(parents=True)
    main_env.mkdir(parents=True)
    (helper_env / "existing.py").write_text("x = 2\n", encoding="utf-8")
    (helper_env / "fake_dependency.py").write_text("stub = True\n", encoding="utf-8")
    (main_env / "existing.py").write_text("x = 1\n", encoding="utf-8")
    snapshot = {"_env/app/existing.py": (0.0, 6)}

    copied, stats, _file_map = _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "env_guard",
        fork_snapshot=snapshot,
        expected_outputs=["_env/app/existing.py"],
    )

    assert "_env/app/existing.py" in stats["env_copied_files"]
    assert (main_env / "existing.py").read_text(encoding="utf-8") == "x = 2\n"
    assert not (main_env / "fake_dependency.py").exists()
    assert "_env/app/fake_dependency.py" in stats["env_skipped_unexpected_new"]


def test_environment_copyback_allows_declared_new_env_files(tmp_path):
    helper_ws = tmp_path / "helper"
    main_ws = tmp_path / "main"
    helper_env = helper_ws / "_env" / "docs"
    helper_env.mkdir(parents=True)
    (helper_env / "algorithm_report.md").write_text("# Report\n", encoding="utf-8")

    copied, stats, _file_map = _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "env_report",
        fork_snapshot={},
        expected_outputs=["_env/docs/algorithm_report.md"],
    )

    assert "_env/docs/algorithm_report.md" in stats["env_copied_files"]
    assert (main_ws / "_env" / "docs" / "algorithm_report.md").is_file()
    assert stats["env_skipped_unexpected_new"] == []


def test_environment_copyback_does_not_overwrite_sibling_output_with_unowned_env_input(tmp_path):
    helper_ws = tmp_path / "helper"
    main_ws = tmp_path / "main"
    helper_contracts = helper_ws / "_env" / "contracts"
    helper_service = helper_ws / "_env" / "service"
    main_contracts = main_ws / "_env" / "contracts"
    main_service = main_ws / "_env" / "service"
    helper_contracts.mkdir(parents=True)
    helper_service.mkdir(parents=True)
    main_contracts.mkdir(parents=True)
    main_service.mkdir(parents=True)

    old_contract = "def validate_event(payload):\n    return payload['customer_name']\n"
    new_contract = "def validate_event(payload):\n    return payload['account_name']\n"
    old_service = "def render_account(event):\n    return event['customer_name']\n"
    new_service = "def render_account(event):\n    return event['account_name']\n"

    (helper_contracts / "customer_event.py").write_text(old_contract, encoding="utf-8")
    (helper_service / "render.py").write_text(new_service, encoding="utf-8")
    (main_contracts / "customer_event.py").write_text(new_contract, encoding="utf-8")
    (main_service / "render.py").write_text(old_service, encoding="utf-8")
    snapshot = {
        "_env/contracts/customer_event.py": (0.0, len(old_contract.encode("utf-8"))),
        "_env/service/render.py": (0.0, len(old_service.encode("utf-8"))),
    }

    copied, stats, _file_map = _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "migrate-service",
        fork_snapshot=snapshot,
        expected_outputs=["_env/service/render.py"],
        helper_kind="code",
    )

    assert "_env/service/render.py" in copied
    assert "_env/contracts/customer_event.py" not in copied
    assert (main_contracts / "customer_event.py").read_text(encoding="utf-8") == new_contract
    assert (main_service / "render.py").read_text(encoding="utf-8") == new_service
    assert "_env/contracts/customer_event.py" in stats["env_skipped_unowned"]


def test_environment_copyback_registers_staged_outputs(tmp_path):
    helper_ws = tmp_path / "helper"
    main_ws = tmp_path / "main"
    helper_env = helper_ws / "_env" / "docs"
    helper_env.mkdir(parents=True)
    main_ws.mkdir()
    (helper_env / "algorithm_report.md").write_text("# Report\n", encoding="utf-8")

    _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "env_report",
        fork_snapshot={},
        expected_outputs=["_env/docs/algorithm_report.md"],
        helper_kind="edit",
    )

    registry = FileRegistry.load(scope_id=f"workspace:{main_ws.resolve()}", workspace_root=main_ws)
    records = registry.list_records(kind=FileKind.HELPER_OUTPUT, visibility=Visibility.PROJECT)
    assert [record.workspace_path for record in records] == ["_env/docs/algorithm_report.md"]
    assert records[0].owner_task_id == "env_report"


def test_read_helper_internal_evidence_is_copied_without_project_visibility(tmp_path):
    helper_ws = tmp_path / "helper"
    main_ws = tmp_path / "main"
    helper_ws.mkdir()
    main_ws.mkdir()
    (helper_ws / "read_evidence_docx.txt").write_text(
        "VERDICT: PASS\ncoverage_summary: all staged docx files covered\n",
        encoding="utf-8",
    )

    copied, stats, file_map = _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "read_docx",
        fork_snapshot={},
        expected_outputs=["read_evidence_docx.txt"],
        helper_kind="read",
    )

    assert copied == ["read_evidence_docx.txt"]
    assert (main_ws / "read_evidence_docx.txt").is_file()
    assert stats.get("env_copied_files", []) == []
    assert file_map[0]["main_name"] == "read_evidence_docx.txt"

    registry = FileRegistry.load(scope_id=f"workspace:{main_ws.resolve()}", workspace_root=main_ws)
    records = registry.list_records(kind=FileKind.HELPER_OUTPUT, visibility=Visibility.INTERNAL)
    assert [record.workspace_path for record in records] == ["read_evidence_docx.txt"]


def test_session_temp_copyback_mirrors_outputs_to_persistent_root(tmp_path):
    persistent_ws = tmp_path / "main"
    persistent_ws.mkdir()
    session_ws = ws_tool.ensure_temp_workspace(
        str(persistent_ws),
        session_tag="arch:group:user:trace",
        isolate_session=True,
    )
    helper_ws = tmp_path / "helper"
    helper_ws.mkdir()
    (helper_ws / "answer.txt").write_text("session answer\n", encoding="utf-8")

    copied, _stats, _file_map = _copy_results_to_main(
        str(helper_ws),
        session_ws,
        "writer",
        fork_snapshot={},
        expected_outputs=["answer.txt"],
        helper_kind="edit",
    )

    assert copied == ["answer.txt"]
    assert (Path(session_ws) / "answer.txt").read_text(encoding="utf-8") == "session answer\n"
    assert (persistent_ws / "answer.txt").read_text(encoding="utf-8") == "session answer\n"


def test_copyback_scrubs_internal_env_prefix_in_text_outputs(tmp_path):
    helper_ws = tmp_path / "helper"
    main_ws = tmp_path / "main"
    helper_env = helper_ws / "_env" / "analysis"
    helper_env.mkdir(parents=True)
    main_ws.mkdir()
    body = (
        "# Red-Black Tree Analysis\n\n"
        "Reference implementation: `_env/src/rbtree.c` (353 lines).\n"
        "All claims trace back to `_env/src/rbtree.c` and `_env/bench_results/rbt.csv`.\n"
        "Unrelated mention of `train_env/foo` and `_env_other` should not change.\n"
    )
    (helper_env / "rbt_analysis.md").write_text(body, encoding="utf-8")

    copied, stats, _file_map = _copy_results_to_main(
        str(helper_ws),
        str(main_ws),
        "env_report",
        fork_snapshot={},
        expected_outputs=["_env/analysis/rbt_analysis.md"],
        helper_kind="edit",
    )

    written = (main_ws / "_env" / "analysis" / "rbt_analysis.md").read_text(encoding="utf-8")
    assert "_env/src/rbtree.c" not in written
    assert "_env/bench_results/rbt.csv" not in written
    assert "src/rbtree.c" in written
    assert "bench_results/rbt.csv" in written
    # Adjacent identifiers must not be scrubbed.
    assert "train_env/foo" in written
    assert "_env_other" in written
    scrubbed = stats.get("internal_paths_scrubbed", [])
    assert scrubbed and scrubbed[0]["occurrences"] >= 3
