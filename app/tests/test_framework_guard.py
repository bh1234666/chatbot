"""Regression tests for framework-first delegate guard behavior."""

from app.llm.tools.delegate_framework import (
    broad_framework_guard_warnings,
    format_helper_request_envelope,
    helper_prompt_with_framework,
    high_priority_framework_warnings,
    normalize_framework_contract,
    normalize_string_list,
    task_has_framework,
)
from app.llm.tools.delegate import _deterministic_kind_recommendations, _sanitize_and_validate_tasks
from app.llm.tools.delegate_state import _record_task_contracts
from app.llm.tools.environment import _handle_apply_create
from app.llm.tools.registry import _handle_workspace


def test_framework_contract_normalizes_dict():
    text = normalize_framework_contract({
        "goal": "compare index maintenance strategies",
        "schema": {"algorithm": "string", "insert_ms": "float"},
        "validation": ["same seed", "same operation mix"],
    })
    assert "goal: compare index maintenance strategies" in text
    assert "schema:" in text
    assert "same seed" in text


def test_framework_contract_sorts_nested_dict_keys():
    left = normalize_framework_contract({
        "schema": {"z": "last", "a": "first"},
        "goal": "stable",
    })
    right = normalize_framework_contract({
        "goal": "stable",
        "schema": {"a": "first", "z": "last"},
    })
    assert left == right
    assert left.index("a: first") < left.index("z: last")


def test_helper_prompt_receives_framework_block():
    prompt = helper_prompt_with_framework(
        "Implement only the red-black tree slice.",
        "Interface: class IndexStrategy with insert/delete/search/range_scan.",
    )
    assert "## Shared Framework Contract" in prompt
    assert "## Assigned Helper Task" in prompt
    assert "red-black tree slice" in prompt


def test_helper_request_envelope_formats_task_metadata():
    prompt = format_helper_request_envelope({
        "task_id": "impl_rbtree",
        "kind": "code",
        "mode": "hard",
        "framework": "Interface: IndexStructure; schema: benchmark_results.csv.",
        "input_files": ["_env/src/interfaces.py", "_env/FRAMEWORK.md"],
        "expected_outputs": ["_env/src/rb_tree.py", "_env/results/rb_tree.csv"],
        "write_scopes": ["_env/src"],
        "acceptance_checks": ["python -m pytest tests/test_rb_tree.py", "CSV columns match schema"],
        "prompt": "Implement only the red-black tree slice.",
    })
    assert "## Helper Request Envelope" in prompt
    assert "helper_kind: code" in prompt
    assert "helper_mode: hard" in prompt
    assert "Interface: IndexStructure" in prompt
    assert "_env/src/interfaces.py" in prompt
    assert "_env/src/rb_tree.py" in prompt
    assert "### Writable Project Scopes" in prompt
    assert "_env/src" in prompt
    assert "python -m pytest tests/test_rb_tree.py" in prompt
    assert "Implement only the red-black tree slice." in prompt


def test_helper_request_envelope_sorts_mapping_metadata():
    left = format_helper_request_envelope({
        "task_id": "read_sources",
        "kind": "read",
        "framework": {"schema": {"z": "last", "a": "first"}, "goal": "stable"},
        "input_files": {"z": "_env/z.docx", "a": "_env/a.pdf"},
        "write_scopes": {"reports": "_helpers_shared/reports", "evidence": "_helpers_shared/evidence"},
        "checks": {"z": "verify z", "a": "verify a"},
        "prompt": "Extract evidence.",
    })
    right = format_helper_request_envelope({
        "task_id": "read_sources",
        "kind": "read",
        "framework": {"goal": "stable", "schema": {"a": "first", "z": "last"}},
        "input_files": {"a": "_env/a.pdf", "z": "_env/z.docx"},
        "write_scopes": {"evidence": "_helpers_shared/evidence", "reports": "_helpers_shared/reports"},
        "checks": {"a": "verify a", "z": "verify z"},
        "prompt": "Extract evidence.",
    })

    assert left == right
    assert left.index("a: _env/a.pdf") < left.index("z: _env/z.docx")
    assert left.index("a: verify a") < left.index("z: verify z")


def test_normalize_string_list_sorts_sets_for_prompt_stability():
    assert normalize_string_list({"b", "a", "c"}) == ["a", "b", "c"]


def test_task_has_framework_requires_field():
    assert task_has_framework({"framework": "shared framework contract"})
    assert not task_has_framework({"prompt": "Follow the benchmark spec and CSV schema."})
    assert not task_has_framework({"prompt": "Implement everything."})


def test_overconcentrated_single_task_blocks_without_framework():
    tasks = [{
        "task_id": "all_db_algorithms",
        "kind": "code",
        "prompt": (
            "Compare Red-Black Tree, Skip List, B-Tree, B+Tree, and Fractal Tree performance. "
            "Implement algorithms, run benchmarks, generate CSV, write charts, and draft a paper report. "
            "Use insert/delete/search/range_scan workloads and research a new algorithm."
        ),
        "expected_outputs": [
            "algorithms.py",
            "benchmarks.csv",
            "figures.png",
            "paper.docx",
            "new_algorithm.md",
        ],
    }]
    warnings = broad_framework_guard_warnings(tasks)
    issues = {w["issue"] for w in warnings}
    assert "overconcentrated_helper_task" in issues
    assert high_priority_framework_warnings(warnings)


def test_peer_tasks_with_framework_are_allowed():
    tasks = [
        {
            "task_id": "rbtree",
            "kind": "code",
            "prompt": "Implement red-black tree slice.",
            "framework": "Shared framework contract: IndexStrategy interface and benchmark CSV schema.",
            "expected_outputs": ["rbtree.py", "results_rbtree.csv"],
        },
        {
            "task_id": "skiplist",
            "kind": "code",
            "prompt": "Implement skip list slice.",
            "framework": "Shared framework contract: IndexStrategy interface and benchmark CSV schema.",
            "expected_outputs": ["skiplist.py", "results_skiplist.csv"],
        },
        {
            "task_id": "btree",
            "kind": "code",
            "prompt": "Implement B-tree slice.",
            "framework": "Shared framework contract: IndexStrategy interface and benchmark CSV schema.",
            "expected_outputs": ["btree.py", "results_btree.csv"],
        },
    ]
    assert broad_framework_guard_warnings(tasks) == []


def test_peer_tasks_without_framework_request_contract_first():
    tasks = [
        {
            "task_id": "rbtree",
            "kind": "code",
            "prompt": "Benchmark Red-Black Tree performance.",
            "expected_outputs": ["results_rbtree.csv"],
        },
        {
            "task_id": "skiplist",
            "kind": "code",
            "prompt": "Benchmark Skip List performance.",
            "expected_outputs": ["results_skiplist.csv"],
        },
        {
            "task_id": "bptree",
            "kind": "code",
            "prompt": "Benchmark B+Tree performance.",
            "expected_outputs": ["results_bptree.csv"],
        },
    ]
    warnings = broad_framework_guard_warnings(tasks)
    assert any(w["issue"] == "missing_framework_for_peer_fanout" for w in warnings)


def test_framework_producer_mixed_with_consumers_blocks():
    tasks = [
        {
            "task_id": "framework_contract",
            "kind": "code",
            "prompt": "Create the shared framework contract and benchmark schema.",
            "expected_outputs": ["contract.json"],
        },
        {
            "task_id": "impl_rbt",
            "kind": "code",
            "prompt": "Implement Red-Black Tree after reading the contract.",
            "expected_outputs": ["src/red_black_tree.py"],
        },
        {
            "task_id": "impl_skip",
            "kind": "code",
            "prompt": "Implement Skip List after reading the contract.",
            "expected_outputs": ["src/skip_list.py"],
        },
    ]
    warnings = broad_framework_guard_warnings(tasks)
    assert any(w["issue"] == "framework_producer_mixed_with_consumers" for w in warnings)
    assert high_priority_framework_warnings(warnings)


def test_peer_tasks_with_prompt_reference_but_no_framework_field_block():
    tasks = [
        {
            "task_id": "impl_rbt",
            "kind": "code",
            "prompt": "Follow the shared framework contract. Benchmark Red-Black Tree.",
            "expected_outputs": ["results_rbtree.csv"],
        },
        {
            "task_id": "impl_skip",
            "kind": "code",
            "prompt": "Follow the shared framework contract. Benchmark Skip List.",
            "expected_outputs": ["results_skiplist.csv"],
        },
        {
            "task_id": "impl_bplus",
            "kind": "code",
            "prompt": "Follow the shared framework contract. Benchmark B+Tree.",
            "expected_outputs": ["results_bplus.csv"],
        },
    ]
    warnings = broad_framework_guard_warnings(tasks)
    assert any(w["issue"] == "missing_framework_for_peer_fanout" for w in warnings)


def test_oversized_framework_helper_blocks():
    prompt = (
        "Create a framework contract.\n"
        "```python\nclass A: pass\n```\n"
        "```python\nclass B: pass\n```\n"
        "```python\nclass C: pass\n```\n"
        "```python\nclass D: pass\n```\n"
    ) + ("Implement detailed bodies. " * 250)
    tasks = [{
        "task_id": "framework_contract",
        "kind": "code",
        "prompt": prompt,
        "expected_outputs": [
            "contract.json",
            "src/interface.py",
            "src/a.py",
            "src/b.py",
            "benchmarks/harness.py",
            "paper.docx",
        ],
    }]
    warnings = broad_framework_guard_warnings(tasks)
    assert any(w["issue"] == "overconcentrated_framework_task" for w in warnings)


def test_framework_priority_cap_releases_after_two_hits():
    warnings = [{"issue": "overconcentrated_helper_task", "severity": "high"}]
    assert high_priority_framework_warnings(warnings, trace_total=0, cap=2)
    assert high_priority_framework_warnings(warnings, trace_total=1, cap=2)
    assert high_priority_framework_warnings(warnings, trace_total=2, cap=2) == []


def test_removed_general_scaffold_kind_is_not_silently_rewritten():
    tasks = [{
        "task_id": "framework-scaffold",
        "kind": "general",
        "prompt": "Create the shared framework scaffold and write clean importable project files.",
        "expected_outputs": [
            "_env/src/db_index/interfaces.py",
            "_env/src/db_index/__init__.py",
            "_env/src/db_index/benchmark_spec.py",
            "_env/FRAMEWORK_CONTRACT.txt",
        ],
    }]
    recs = _deterministic_kind_recommendations(tasks)
    assert recs == []
    assert tasks[0]["kind"] == "general"


async def test_explicit_code_hard_backup_preserves_helper_envelope(tmp_path, monkeypatch):
    from app.core.runtime_mode import EnvironmentContext, runtime_context

    env = EnvironmentContext(
        root_dir=str(tmp_path / "project"),
        archive_id="arch",
        group_id="env_user_test",
        user_id="user",
        project_key="project",
    )
    (tmp_path / "project").mkdir()
    monkeypatch.setattr("app.core.debug.current_trace_id", lambda: "trace_hard_pair_contract")
    with runtime_context("environment", env):
        cleaned = await _sanitize_and_validate_tasks(
            {
                "tasks": [
                    {
                        "task_id": "framework_infra",
                        "kind": "code",
                        "mode": "easy",
                        "framework": "Shared contract: IndexBase API and benchmark CSV schema.",
                        "input_files": ["_env/contract.json"],
                        "prompt": (
                            "Create the shared framework scaffold for a multi-file project. "
                            "Use _env/src/base.py, _env/benchmark/runner.py, _env/benchmark/workloads.py, "
                            "_env/analysis/plots.py, _env/tests/test_contract.py, and "
                            "_env/scripts/check_project.py. Validate with the check script."
                        ),
                        "expected_outputs": [
                            "_env/src/base.py",
                            "_env/benchmark/workloads.py",
                            "_env/benchmark/runner.py",
                            "_env/analysis/plots.py",
                            "_env/tests/test_contract.py",
                            "_env/scripts/check_project.py",
                        ],
                        "acceptance_checks": ["python scripts/check_project.py"],
                    },
                    {
                        "task_id": "framework_infra_hard",
                        "kind": "code",
                        "mode": "hard",
                        "framework": "Shared contract: IndexBase API and benchmark CSV schema.",
                        "input_files": ["_env/contract.json"],
                        "prompt": (
                            "Create the shared framework scaffold for a multi-file project. "
                            "Use _env/src/base.py, _env/benchmark/runner.py, _env/benchmark/workloads.py, "
                            "_env/analysis/plots.py, _env/tests/test_contract.py, and "
                            "_env/scripts/check_project.py. Validate with the check script."
                        ),
                        "expected_outputs": [
                            "_env/src/base.py",
                            "_env/benchmark/workloads.py",
                            "_env/benchmark/runner.py",
                            "_env/analysis/plots.py",
                            "_env/tests/test_contract.py",
                            "_env/scripts/check_project.py",
                        ],
                        "acceptance_checks": ["python scripts/check_project.py"],
                    },
                ]
            },
            main_workspace=str(tmp_path),
            archive_id="arch",
            group_id="env_user_test",
            user_id="user",
        )
    assert isinstance(cleaned, list)
    assert {task["task_id"] for task in cleaned} == {"framework_infra", "framework_infra_hard"}
    hard = next(task for task in cleaned if task["task_id"] == "framework_infra_hard")
    assert hard["paired_with"] == "framework_infra"
    assert hard["framework"].startswith("Shared contract")
    assert hard["input_files"] == ["_env/contract.json"]
    assert hard["expected_outputs"] == [
        "_env/src/base.py",
        "_env/benchmark/workloads.py",
        "_env/benchmark/runner.py",
        "_env/analysis/plots.py",
        "_env/tests/test_contract.py",
        "_env/scripts/check_project.py",
    ]
    assert hard["acceptance_checks"] == ["python scripts/check_project.py"]


def test_main_env_apply_create_medium_source_requires_helper(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    content = "\n".join(f"def func_{i}():\n    return {i}" for i in range(80))
    result = _handle_apply_create(str(tmp_path), {
        "path": "src/generated_module.py",
        "content": content,
    })
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_source_create_should_delegate"
    assert not (project / "src" / "generated_module.py").exists()


def test_main_env_apply_create_structured_source_requires_helper(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    content = (
        "from abc import ABC, abstractmethod\n"
        "from typing import Optional\n\n"
        "class Interface(ABC):\n"
        "    @abstractmethod\n"
        "    def insert(self, key: int, value: int) -> None:\n"
        "        ...\n"
        "    @abstractmethod\n"
        "    def search(self, key: int) -> Optional[int]:\n"
        "        ...\n"
    )
    result = _handle_apply_create(str(tmp_path), {
        "path": "framework/benchmark_interface.py",
        "content": content * 3,
    })
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_source_create_should_delegate"
    assert not (project / "framework" / "benchmark_interface.py").exists()


def test_main_env_apply_create_tiny_source_stub_allowed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    result = _handle_apply_create(str(tmp_path), {
        "path": "src/pkg/__init__.py",
        "content": '"""Package marker."""\n',
    })
    assert result["ok"] is True
    assert (project / "src" / "pkg" / "__init__.py").read_text(encoding="utf-8")


def test_main_env_apply_create_short_project_contract_requires_helper(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    result = _handle_apply_create(str(tmp_path), {
        "path": "_shared/paper_framework_contract.md",
        "content": "# Contract\n\n- Scope: database index paper\n",
    })
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_project_artifact_create_should_delegate"
    assert not (project / "_shared" / "paper_framework_contract.md").exists()


def test_main_env_apply_create_compact_design_summary_allowed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    content = (
        "# Design Thread Summary\n\n"
        "## Decisions\n"
        "- Final decision: option B.\n\n"
        "## Open Items\n"
        "- Mobile breakpoint remains open.\n\n"
        "## Commitments\n"
        "- You committed to the spec writeup by Friday.\n"
    )
    result = _handle_apply_create(str(tmp_path), {
        "path": "design_thread_summary.md",
        "content": content,
    })
    assert result["ok"] is True
    assert (project / "design_thread_summary.md").read_text(encoding="utf-8") == content


def test_main_env_apply_create_tiny_private_note_allowed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    result = _handle_apply_create(str(tmp_path), {
        "path": "notes.txt",
        "content": "temporary coordination note\n",
    })
    assert result["ok"] is True
    assert (project / "notes.txt").read_text(encoding="utf-8") == "temporary coordination note\n"


def test_main_env_apply_create_short_source_implementation_requires_helper(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    result = _handle_apply_create(str(tmp_path), {
        "path": "ui/app.js",
        "content": "const state = {};\nfunction boot(){ return state; }\n",
    })
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_source_create_should_delegate"
    assert not (project / "ui" / "app.js").exists()


def test_main_env_apply_create_substantial_direct_source_requires_helper(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.llm.tools.environment.current_environment", lambda: type(
        "Env", (), {"root_dir": str(project)}
    )())
    content = "\n".join(f"def func_{i}():\n    return {i}" for i in range(180))
    result = _handle_apply_create(str(tmp_path), {
        "path": "src/generated_module.py",
        "content": content,
    })
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_source_create_should_delegate"
    assert result["content_chars"] == len(content)
    assert not (project / "src" / "generated_module.py").exists()


async def test_code_helper_large_project_source_write_requests_segmentation(tmp_path):
    from app.core.core_processes import reset_current_helper_kind, set_current_helper_kind

    token = set_current_helper_kind("code")
    try:
        content = "\n".join(f"def generated_{i}():\n    return {i}" for i in range(220))
        result_text = await _handle_workspace(str(tmp_path), {
            "action": "write",
            "path": "_env/src/generated_big_module.py",
            "content": content,
        })
    finally:
        reset_current_helper_kind(token)

    import json

    result = json.loads(result_text)
    assert result["ok"] is False
    assert result["error_kind"] == "helper_monolithic_project_write_should_segment"
    assert result["recovery_facts"]["same_task"] is True
    assert "small skeleton or interface" in result["recovery_facts"]["available_authoring_shapes"]
    assert "focused edit_file/multi_edit/insert_in_file steps" in result["recovery_facts"]["available_authoring_shapes"]
    assert "isolated python execution is not a workspace file IO substitute" in result["recovery_facts"]["workspace_io_fact"]
    assert not (tmp_path / "_env" / "src" / "generated_big_module.py").exists()


async def test_code_helper_small_project_source_write_allowed(tmp_path):
    from app.core.core_processes import reset_current_helper_kind, set_current_helper_kind

    token = set_current_helper_kind("code")
    try:
        result_text = await _handle_workspace(str(tmp_path), {
            "action": "write",
            "path": "_env/src/small_stub.py",
            "content": "def ok():\n    return True\n",
        })
    finally:
        reset_current_helper_kind(token)

    import json

    result = json.loads(result_text)
    assert result["ok"] is True
    assert (tmp_path / "_env" / "src" / "small_stub.py").read_text(encoding="utf-8")


async def test_main_thread_existing_staged_project_copy_write_returns_apply_fact(tmp_path):
    import json

    staged = tmp_path / "_env" / "pricing.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("def price():\n    return 1\n", encoding="utf-8")

    result_text = await _handle_workspace(str(tmp_path), {
        "action": "write",
        "path": "_env/pricing.py",
        "content": "def price():\n    return 2\n",
    })

    result = json.loads(result_text)
    assert result["ok"] is True
    assert result["staged_project_copy"] is True
    assert result["staged_project_path"] == "_env/pricing.py"
    assert "env_apply_replace" in result["pending_project_apply_fact"]
    assert "env_diff" in result["suggested_next_tools"]
    assert staged.read_text(encoding="utf-8") == "def price():\n    return 2\n"


async def test_main_thread_project_framework_write_requests_delegate(tmp_path):
    import json

    result_text = await _handle_workspace(str(tmp_path), {
        "action": "write",
        "path": "db_index_project/_shared/CONTRACT.md",
        "content": "# Shared Framework Contract\n\nInterface and benchmark ownership.\n",
    })
    result = json.loads(result_text)
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_project_artifact_should_delegate"
    assert result["recovery_action"] == "switch_tool"
    assert result["retry_same_tool"] is False
    assert result["recommended_tools"] == ["delegate"]
    assert result["recovery_facts"]["same_goal"] is True
    assert "helper-owned staged output" in result["recovery_facts"]["helper_prompt_fact"]
    assert "internal handoff" in result["recovery_facts"]["helper_prompt_fact"]
    assert not (tmp_path / "db_index_project" / "_shared" / "CONTRACT.md").exists()


async def test_main_thread_project_source_write_requests_delegate(tmp_path):
    import json

    result_text = await _handle_workspace(str(tmp_path), {
        "action": "write",
        "path": "db_index_project/_shared/index_base.py",
        "content": "class IndexBase:\n    pass\n",
    })
    result = json.loads(result_text)
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_project_artifact_should_delegate"
    assert result["recovery_facts"]["matching_helper_kind"] == "code"
    assert not (tmp_path / "db_index_project" / "_shared" / "index_base.py").exists()


async def test_main_thread_root_project_script_write_requests_delegate(tmp_path):
    import json

    result_text = await _handle_workspace(str(tmp_path), {
        "action": "write",
        "path": "run_all_benchmarks.py",
        "content": "print('benchmark')\n",
    })
    result = json.loads(result_text)
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_project_artifact_should_delegate"
    assert result["recovery_facts"]["matching_helper_kind"] == "code"
    assert not (tmp_path / "run_all_benchmarks.py").exists()


async def test_main_thread_project_benchmark_run_requests_delegate(tmp_path):
    import json

    (tmp_path / "run_all_benchmarks.py").write_text("print('benchmark')\n", encoding="utf-8")
    result_text = await _handle_workspace(str(tmp_path), {
        "action": "run",
        "command": "python run_all_benchmarks.py",
        "timeout_sec": 600,
    })
    result = json.loads(result_text)
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_project_run_should_delegate"
    assert result["recovery_action"] == "switch_tool"
    assert result["retry_same_tool"] is False
    assert result["recommended_tools"] == ["delegate"]
    assert result["recovery_facts"]["matching_helper_kind"] == "code"
    assert result["blocked_scripts"] == ["run_all_benchmarks.py"]


async def test_main_thread_pytest_verification_run_requests_delegate(tmp_path, monkeypatch):
    import json

    from app.llm.tools import registry

    async def fake_run(workspace_dir, command, timeout_sec=None, abort_event=None):
        return {"ok": True, "command": command, "timeout_sec": timeout_sec}

    monkeypatch.setattr(registry.ws_tool, "handle_run", fake_run)
    result_text = await _handle_workspace(str(tmp_path), {
        "action": "run",
        "command": "python -m pytest tests/test_benchmark.py",
        "timeout_sec": 60,
    })
    result = json.loads(result_text)
    assert result["ok"] is False
    assert result["error_kind"] == "main_thread_workspace_run_should_delegate"
    assert result["recovery_facts"]["matching_helper_kind"] == "code"


async def test_main_thread_scratch_text_write_still_allowed(tmp_path):
    import json

    result_text = await _handle_workspace(str(tmp_path), {
        "action": "write",
        "path": "notes.txt",
        "content": "small note\n",
    })
    result = json.loads(result_text)
    assert result["ok"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "small note\n"


async def test_helper_progress_update_returns_true_on_event_success(monkeypatch, tmp_path):
    from app.core import core_processes

    seen: list[dict] = []
    monkeypatch.setattr("app.core.environment_events.publish_workflow_event", seen.append)
    proc_id = await core_processes.registry().register_helper(
        owner="owner",
        task=None,
        helper_task_id="task_progress",
        helper_workspace=str(tmp_path),
        abort_event=__import__("asyncio").Event(),
        helper_kind="code",
    )
    try:
        ok = await core_processes.registry().update_helper_progress(
            proc_id,
            iter_num=3,
            note="streaming iter 3",
        )
    finally:
        await core_processes.registry().unregister(proc_id)

    assert ok is True
    assert seen and seen[-1]["kind"] == "helper_progress"
    assert seen[-1]["iter"] == 3


async def test_resume_inherits_missing_helper_contract_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.debug.current_trace_id", lambda: "trace_contract_test")
    await _record_task_contracts("trace_contract_test", [{
        "task_id": "impl_slice",
        "kind": "code",
        "mode": "easy",
        "framework": "Interface: IndexStrategy; CSV schema: benchmark_results.csv",
        "input_files": ["_env/FRAMEWORK.md"],
        "expected_outputs": ["_env/src/db_index/red_black_tree.py"],
        "acceptance_checks": ["python -m pytest tests/test_red_black_tree.py"],
    }])
    cleaned = await _sanitize_and_validate_tasks(
        {
            "tasks": [{
                "task_id": "impl_slice",
                "resume": True,
                "prompt": "Continue from the previous useful work and fix the failing test.",
            }]
        },
        main_workspace=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
    )
    assert isinstance(cleaned, list)
    task = cleaned[0]
    assert task["kind"] == "code"
    assert task["framework"].startswith("Interface: IndexStrategy")
    assert task["input_files"] == ["_env/FRAMEWORK.md"]
    assert task["expected_outputs"] == ["_env/src/db_index/red_black_tree.py"]
    assert task["acceptance_checks"] == ["python -m pytest tests/test_red_black_tree.py"]
