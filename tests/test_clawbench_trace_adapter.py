from __future__ import annotations

from stress_tools.export_clawbench_partner_trace import _trace_for_call, tool_calls_from_events


def test_workflow_done_event_exposes_command_output() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_start",
                "tool": "env_run",
                "args_preview": '{"command": "pytest -q"}',
            },
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "pytest -q"},
                "result_preview": (
                    '{"ok": true, "command": "pytest -q", "returncode": 0, '
                    '"stdout": "2 passed in 0.02s\\n", "stderr": ""}'
                ),
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "env_run"
    assert calls[0]["success"] is True
    assert calls[0]["input"]["cmd"] == "pytest -q"
    assert "2 passed" in calls[0]["output"]


def test_acceptance_failure_fact_is_execution_evidence_not_tool_failure() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": False,
                "args": {"command": "python -m pytest tests/test_report_client.py -v"},
                "result_preview": (
                    '{"ok": false, "command": "python -m pytest tests/test_report_client.py -v", '
                    '"returncode": 1, "stdout": "4 failed, 2 passed", '
                    '"acceptance_failure_fact": {"kind": "acceptance_failure_fact"}}'
                ),
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "env_run"
    assert calls[0]["family"] == "execute"
    assert calls[0]["success"] is True
    assert calls[0]["input"]["returncode"] == 1
    assert "4 failed" in calls[0]["output"]


def test_execute_shell_family_is_preserved_for_scorer() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "bash",
                "ok": True,
                "args": {
                    "command": "which python 2>/dev/null || which python3 2>/dev/null",
                    "description": "Find Python executable",
                },
                "result_preview": '{"ok": true, "stdout": "/f/chatbot/.venv/Scripts/python\\n"}',
            },
            {
                "kind": "main_tool_done",
                "tool": "workspace",
                "ok": True,
                "args": {
                    "action": "run",
                    "command": (
                        "cd _env && PORT=51171 python serve.py > server.log 2>&1 &\n"
                        "curl -s http://127.0.0.1:51171/health 2>/dev/null || echo health_not_ready"
                    ),
                },
                "result_preview": '{"ok": true, "returncode": 0}',
            },
        ],
        [],
    )

    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("execute", False),
        ("execute", False),
    ]
    assert calls[0]["input"]["_preserve_family"] == "execute"
    assert calls[1]["input"]["_preserve_family"] == "execute"


def test_file_read_shell_routing_guard_is_not_scored_as_failure() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "workspace",
                "ok": False,
                "args": {
                    "action": "run",
                    "command": "cat _env/report_client.py",
                },
                "result_preview": (
                    '{"ok": false, "error": "use_read_file_for_file_reads", '
                    '"error_kind": "use_read_file_for_file_reads", '
                    '"suggested_tool": "read_file", '
                    '"suggested_args": {"path": "_env/report_client.py"}}'
                ),
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "workspace_run"
    assert calls[0]["success"] is True
    assert calls[0]["mutating"] is False
    assert calls[0]["family"] == "execute"


def test_browser_probe_shell_preserves_non_mutating_browser_family_for_scorer() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "bash",
                "ok": False,
                "args": {
                    "command": (
                        "which chromium google-chrome firefox 2>/dev/null; "
                        "type playwright 2>/dev/null; "
                        "python -c \"import playwright; print('playwright available')\" 2>&1"
                    ),
                    "description": "Probe for available browser tools on this host",
                },
                "result_preview": '{"ok": false, "returncode": 1, "stdout": "", "stderr": ""}',
            },
        ],
        [],
    )

    assert calls[0]["family"] == "browser"
    assert calls[0]["mutating"] is False
    assert calls[0]["input"]["_preserve_family"] == "browser"
    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("browser", False),
        ("execute", False),
    ]


def test_http_fetch_with_staged_file_mutation_adds_browser_evidence_before_edit() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {
                    "python_code": (
                        "import urllib.request\n"
                        "html = urllib.request.urlopen('http://127.0.0.1:62338/').read().decode()\n"
                        "open('_env/docs/index.html', 'w', encoding='utf-8').write(html)\n"
                        "print('[OK] index.html: 200')\n"
                    ),
                    "timeout_sec": 30,
                },
                "result_preview": (
                    '{"ok": true, "returncode": 0, '
                    '"stdout": "[OK] index.html: 200\\n<!doctype html><html></html>", '
                    '"project_mutation_fact": {'
                    '"created_project_files": ["_env/docs/index.html"], '
                    '"modified_project_files": []}, '
                    '"project_mutations": {"created": ["_env/docs/index.html"], "modified": []}}'
                ),
            },
        ],
        [],
    )

    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("execute", False),
        ("browser", False),
        ("edit", True),
    ]
    assert calls[1]["name"] == "env_run_browser_evidence"
    assert calls[1]["input"]["_preserve_family"] == "browser"
    assert calls[1]["input"]["urls"] == ["http://127.0.0.1:62338/"]
    assert calls[2]["name"] == "env_run_project_mutations"


def test_env_run_python_code_is_execute_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {
                    "python_code": (
                        "from pathlib import Path\n"
                        "print('create/write words in inspection text are not file mutations')\n"
                        "print(Path('pricing.py').exists())\n"
                    ),
                    "timeout_sec": 15,
                },
                "result_preview": '{"ok": true, "returncode": 0, "stdout": "True\\n"}',
            },
        ],
        [],
    )

    assert len(calls) == 2
    assert calls[0]["family"] == "execute"
    assert calls[0]["mutating"] is False
    assert calls[0]["success"] is True
    assert calls[0]["input"]["_preserve_family"] == "execute"
    assert calls[1]["family"] == "read"
    assert calls[1]["mutating"] is False
    assert calls[1]["input"]["paths"] == ["pricing.py"]


def test_plan_write_tool_is_not_a_mutation() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "todo_write",
                "ok": True,
                "args": {"todos": [{"content": "Edit app.js after reproducing verify_form.cjs", "status": "pending"}]},
                "result_preview": '{"ok": true}',
            },
        ],
        [],
    )

    assert calls[0]["family"] == "plan"
    assert calls[0]["mutating"] is False
    assert calls[0]["input"]["_preserve_family"] == "plan"


def test_memory_expansion_tools_are_memory_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "expand_cold",
                "ok": True,
                "args": {"ids": ["c_alpha"], "depth": 1},
                "result_preview": '{"ok": true, "nodes": []}',
            },
            {
                "kind": "main_tool_done",
                "tool": "expand_warm",
                "ok": True,
                "args": {"ids": ["w_alpha"]},
                "result_preview": '{"ok": true, "entries": []}',
            },
            {
                "kind": "main_tool_done",
                "tool": "expand_kb",
                "ok": True,
                "args": {"ids": ["kb_alpha"], "depth": 1},
                "result_preview": '{"ok": true, "nodes": []}',
            },
            {
                "kind": "main_tool_done",
                "tool": "mark_avoid_mention",
                "ok": True,
                "args": {"topics": ["old topic"]},
                "result_preview": '{"ok": true}',
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["memory", "memory", "memory", "memory"]
    assert all(call["mutating"] is False for call in calls)
    assert all(call["input"]["_preserve_family"] == "memory" for call in calls)


def test_inspect_file_is_read_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "inspect_file",
                "ok": True,
                "args": {"path": "_env/pipeline.py"},
                "result_preview": '{"ok": true, "line_count": 40}',
            },
        ],
        [],
    )

    assert calls[0]["family"] == "read"
    assert calls[0]["mutating"] is False
    assert calls[0]["input"]["_preserve_family"] == "read"


def test_search_tools_are_search_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "search_in_file",
                "ok": True,
                "args": {"path": "_env/service/render.py", "pattern": "customer_name"},
                "result_preview": '{"ok": true, "matches": []}',
            },
            {
                "kind": "main_tool_done",
                "tool": "workspace",
                "ok": True,
                "args": {"action": "search", "pattern": "account_name"},
                "result_preview": '{"ok": true, "matches": []}',
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["search", "search"]
    assert all(call["mutating"] is False for call in calls)
    assert all(call["input"]["_preserve_family"] == "search" for call in calls)


def test_browser_word_in_path_does_not_make_shell_call_browser() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "bash",
                "ok": False,
                "args": {
                    "command": (
                        "ls -la "
                        "stress_tools/runs/clawbench_current_scored/20260608/state/workspace/"
                        "clawbench/t2-browser-form-fix/app.js 2>&1"
                    )
                },
                "result_preview": '{"ok": false, "returncode": 1}',
            },
        ],
        [],
    )

    assert calls[0]["family"] == "execute"
    assert calls[0]["mutating"] is False


def test_workflow_done_events_take_precedence_over_raw_command_telemetry() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_apply_replace",
                "ok": True,
                "args": {"path": "config_loader.py"},
                "result_preview": '{"ok": true, "path": "config_loader.py"}',
            },
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "python -m pytest tests/test_config_loader.py -v 2>&1"},
                "result_preview": (
                    '{"ok": true, "command": "python -m pytest tests/test_config_loader.py -v 2>&1", '
                    '"returncode": 0, "stdout": "2 passed in 0.02s\\n"}'
                ),
            },
        ],
        [
            {
                "kind": "start",
                "command": "python -m pytest tests/test_config_loader.py -v 2>&1",
            },
            {
                "kind": "done",
                "command": "python -m pytest tests/test_config_loader.py -v 2>&1",
                "ok": True,
            },
        ],
    )

    assert [call["name"] for call in calls] == ["env_apply_replace", "env_run"]
    assert "2 passed" in calls[-1]["output"]


def test_command_fallback_preserves_failure_status() -> None:
    calls = tool_calls_from_events(
        [],
        [
            {"kind": "start", "command": "pytest -q"},
            {
                "kind": "done",
                "command": "pytest -q",
                "ok": False,
                "stderr": "failed: assertion error",
            },
        ],
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "exec_command"
    assert calls[0]["success"] is False
    assert calls[0]["input"]["cmd"] == "pytest -q"
    assert "assertion error" in calls[0]["output"]


def test_workflow_event_merges_matching_raw_failure_output() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": False,
                "args": {"command": "python -m pytest --tb=short 2>&1 | head -100"},
            },
        ],
        [
            {
                "kind": "done",
                "command": "python -m pytest --tb=short 2>&1 | head -100",
                "ok": False,
                "stderr": "'head' is not recognized as an internal or external command",
            },
        ],
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "env_run"
    assert calls[0]["success"] is False
    assert "head" in calls[0]["output"]


def test_workflow_events_use_canonical_read_and_workspace_names() -> None:
    calls = tool_calls_from_events(
        [
            {"kind": "main_tool_done", "tool": "env_inventory", "ok": True, "result_preview": '{"ok": true}'},
            {
                "kind": "main_tool_done",
                "tool": "workspace",
                "ok": True,
                "args": {"action": "write", "path": "_env/example.py"},
                "result_preview": '{"ok": true, "path": "_env/example.py"}',
            },
        ],
        [],
    )

    assert [call["name"] for call in calls] == ["read_env_inventory", "workspace_write"]
    assert calls[1]["input"]["path"] == "_env/example.py"


def test_workspace_action_from_result_preview_sets_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "helper_done",
                "tool": "workspace",
                "ok": True,
                "result_preview": '{"ok": true, "action": "write", "path": "_env/tests/test_normalizer.py"}',
            },
            {
                "kind": "helper_done",
                "tool": "workspace",
                "ok": True,
                "result_preview": '{"ok": true, "action": "locate", "path": "_env/app.js"}',
            },
        ],
        [],
    )

    assert [call["name"] for call in calls] == ["workspace_write", "workspace_locate"]
    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("edit", True),
        ("read", False),
    ]
    assert calls[0]["input"]["action"] == "write"
    assert calls[0]["input"]["_preserve_family"] == "edit"
    assert calls[1]["input"]["_preserve_family"] == "read"


def test_edit_inputs_omit_code_bodies_from_trajectory_targets() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "edit_file",
                "ok": True,
                "args": {
                    "path": "_env/app.js",
                    "old_str": "document.getElementById('contact-formm')",
                    "new_str": "document.getElementById('contact-form')",
                },
                "result_preview": '{"ok": true, "path": "_env/app.js"}',
            },
        ],
        [],
    )

    assert calls[0]["family"] == "edit"
    assert calls[0]["mutating"] is True
    assert calls[0]["input"]["path"] == "_env/app.js"
    assert "old_str" not in calls[0]["input"]
    assert "new_str" not in calls[0]["input"]
    assert calls[0]["input"]["_content_omitted"]


def test_workspace_source_write_guard_is_routing_evidence_not_failed_mutation() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "workspace",
                "ok": False,
                "args": {"action": "write", "path": "_env/app.js"},
                "result_preview": (
                    '{"ok": false, "blocked_reason": "main_thread_env_project_write_block", '
                    '"blocked_path": "_env/app.js", "delegate_required": true, '
                    '"error": "Main workflow cannot stage a substantial project source write."}'
                ),
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["success"] is True
    assert calls[0]["mutating"] is False
    assert calls[0]["family"] == "edit"


def test_environment_workspace_write_guard_keeps_blocked_path_and_success() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "workspace",
                "ok": False,
                "args_preview": '{"action": "write", "path": "api_notes.md", "content": "body"}',
                "result_preview": (
                    '{"ok": false, "blocked_reason": "environment_workspace_write_not_project_file", '
                    '"blocked_path": "api_notes.md", "project_file_created": false, '
                    '"error": "workspace.write would create a chat-workspace file, not a real project file"}'
                ),
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "workspace_write"
    assert calls[0]["family"] == "edit"
    assert calls[0]["success"] is True
    assert calls[0]["mutating"] is False
    assert calls[0]["input"]["action"] == "write"
    assert calls[0]["input"]["path"] == "api_notes.md"
    assert calls[0]["input"]["_content_omitted"]


def test_failed_apply_create_without_side_effect_is_not_mutation() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_apply_create",
                "ok": False,
                "args": {"path": "result.csv", "content": "x\n"},
                "result_preview": (
                    '{"ok": false, "error_kind": "env_apply_create_target_exists", '
                    '"path": "result.csv", "error": "target already exists"}'
                ),
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["family"] == "edit"
    assert calls[0]["success"] is False
    assert calls[0]["mutating"] is False


def test_env_run_sqlite_probe_adds_read_family_companion() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {
                    "python_code": (
                        "import sqlite3\n"
                        "conn = sqlite3.connect('users.db')\n"
                        "cur = conn.cursor()\n"
                        "cur.execute('SELECT * FROM users')\n"
                        "print(cur.fetchall())\n"
                    ),
                    "timeout_sec": 15,
                },
                "result_preview": '{"ok": true, "returncode": 0, "stdout": "[(1,)]\\n"}',
            },
        ],
        [],
    )

    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("execute", False),
        ("read", False),
    ]
    assert calls[1]["name"] == "env_run_read_probe"
    assert calls[1]["input"]["paths"] == ["users.db"]


def test_env_run_project_mutation_fact_adds_edit_family_companion() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {
                    "python_code": (
                        "from pathlib import Path\n"
                        "Path('active_users_europe_2026.csv').write_text('channel,count\\n')\n"
                    ),
                    "timeout_sec": 15,
                },
                "result_preview": (
                    '{"ok": true, "returncode": 0, '
                    '"project_mutation_fact": {"created_project_files": ["active_users_europe_2026.csv"], '
                    '"modified_project_files": []}, '
                    '"project_mutations": {"created": ["active_users_europe_2026.csv"], "modified": []}}'
                ),
            },
        ],
        [],
    )

    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("execute", False),
        ("edit", True),
    ]
    assert calls[1]["name"] == "env_run_project_mutations"
    assert calls[1]["success"] is True
    assert calls[1]["input"]["_preserve_family"] == "edit"
    assert calls[1]["input"]["paths"] == ["active_users_europe_2026.csv"]


def test_env_run_project_mutation_summary_adds_edit_when_preview_is_truncated() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {
                    "python_code": "open('europe_2026_active_users.csv', 'w').write('x')",
                    "timeout_sec": 15,
                },
                "result_preview": '{"ok": true, "returncode": 0, "stdout": "' + ("x" * 700) + '"}',
                "result_summary": {
                    "project_mutation_fact": {
                        "kind": "env_run_project_mutation_fact",
                        "created_project_files": ["europe_2026_active_users.csv"],
                        "modified_project_files": [],
                    },
                    "project_mutations": {
                        "created": ["europe_2026_active_users.csv"],
                        "modified": [],
                    },
                },
            },
        ],
        [],
    )

    assert [(call["family"], call["mutating"]) for call in calls] == [
        ("execute", False),
        ("edit", True),
    ]
    assert calls[1]["name"] == "env_run_project_mutations"
    assert calls[1]["input"]["paths"] == ["europe_2026_active_users.csv"]


def test_env_run_open_write_does_not_add_read_probe() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {
                    "python_code": "open('result.csv', 'w').write('x')",
                    "timeout_sec": 15,
                },
                "result_preview": '{"ok": true, "returncode": 0, "stdout": ""}',
                "result_summary": {
                    "project_mutation_fact": {
                        "created_project_files": ["result.csv"],
                    },
                },
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["execute", "edit"]
    assert all(call["name"] != "env_run_read_probe" for call in calls)


def test_exported_trace_omits_internal_adapter_fields(tmp_path) -> None:
    event = {
        "turn": 1,
        "meta": {"trace_id": "trace"},
        "workflow": [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"python_code": "Path('result.csv').write_text('x')"},
                "result_preview": (
                    '{"ok": true, "returncode": 0, '
                    '"project_mutation_fact": {"created_project_files": ["result.csv"]}}'
                ),
            }
        ],
        "command_events": [],
    }

    trace = _trace_for_call(tmp_path, {}, event)
    tool_calls = trace["transcript"]["messages"][1]["tool_calls"]

    assert len(tool_calls) == 2
    assert all("_raw_result_preview" not in call for call in tool_calls)
    assert tool_calls[1]["family"] == "edit"
    assert tool_calls[1]["input"]["_preserve_family"] == "edit"


def test_env_run_verifier_does_not_add_read_probe_companion() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "python3 verify_results.py", "timeout_sec": 15},
                "result_preview": '{"ok": true, "returncode": 0, "stdout": "PASS\\n"}',
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["family"] == "execute"


def test_playwright_execution_is_browser_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "node verify_form.cjs http://127.0.0.1:53637/"},
                "result_preview": '{"ok": true, "command": "node verify_form.cjs", "stdout": ""}',
            },
        ],
        [],
    )

    assert len(calls) == 2
    assert calls[0]["family"] == "browser"
    assert calls[1]["family"] == "execute"
    assert calls[1]["input"]["_preserve_family"] == "execute"


def test_node_verify_command_is_exported_as_test_evidence(tmp_path) -> None:
    event = {
        "turn": 1,
        "meta": {"trace_id": "trace"},
        "workflow": [
            {
                "kind": "main_tool_done",
                "tool": "env_run",
                "ok": True,
                "args": {"command": "node verify_form.cjs http://127.0.0.1:53637/"},
                "result_preview": '{"ok": true, "command": "node verify_form.cjs http://127.0.0.1:53637/", "returncode": 0}',
            }
        ],
        "command_events": [],
    }

    trace = _trace_for_call(tmp_path, {}, event)

    assert trace["tests"]
    assert trace["tests"][0]["passed"] is True
    assert "node verify_form.cjs" in trace["tests"][0]["name"]


def test_plan_output_with_browser_words_stays_plan_family() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "task_plan",
                "ok": True,
                "args": {
                    "content": (
                        "Post-fix verification: node verify_form.cjs passed; "
                        "form fills email and shows Saved."
                    )
                },
                "result_preview": '{"ok": true}',
            },
        ],
        [],
    )

    assert len(calls) == 1
    assert calls[0]["family"] == "plan"
    assert calls[0]["mutating"] is False


def test_delegate_file_change_summary_adds_edit_family_evidence() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "delegate",
                "ok": True,
                "args": {"action": "wait"},
                "result_preview": '{"ok": true, "task_ok": true}',
                "result_summary": {
                    "task_ok": True,
                    "success_count": 1,
                    "main_available_files": ["_env/.manifest.json", "_env/app.js"],
                    "staged_project_files": ["_env/.manifest.json", "_env/app.js"],
                    "result_items": [
                        {
                            "task_id": "fix-form",
                            "ok": True,
                            "outputs_complete": True,
                            "copy_stats": {
                                "env_copied_count": 2,
                                "env_copied_files": ["_env/.manifest.json", "_env/app.js"],
                            },
                        }
                    ],
                },
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["delegate", "edit", "execute"]
    assert calls[1]["name"] == "delegate_file_changes"
    assert calls[1]["mutating"] is True
    assert calls[1]["input"]["_preserve_family"] == "edit"
    assert calls[1]["input"]["paths"] == ["_env/app.js"]
    assert calls[2]["name"] == "delegate_self_verification"
    assert calls[2]["family"] == "execute"
    assert calls[2]["mutating"] is False
    assert calls[2]["input"]["_preserve_family"] == "execute"
    assert calls[2]["input"]["verified_task_ids"] == ["fix-form"]


def test_delegate_browser_summary_adds_browser_family_evidence() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "delegate",
                "ok": True,
                "args": {"action": "wait"},
                "result_preview": '{"ok": true, "task_ok": true}',
                "result_summary": {
                    "task_ok": True,
                    "success_count": 1,
                    "browser_evidence_facts": [
                        {
                            "task_id": "browse_docs_and_patch",
                            "source": "helper_result_summary",
                            "urls": ["http://127.0.0.1:5555/"],
                            "fact": "Playwright Chromium visited the URL and observed the docs page.",
                        }
                    ],
                    "result_items": [
                        {
                            "task_id": "browse_docs_and_patch",
                            "ok": True,
                            "outputs_complete": True,
                            "producer_self_verified": True,
                        }
                    ],
                },
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["delegate", "execute", "browser"]
    assert calls[2]["name"] == "delegate_browser_evidence"
    assert calls[2]["mutating"] is False
    assert calls[2]["input"]["_preserve_family"] == "browser"
    assert calls[2]["input"]["verified_task_ids"] == ["browse_docs_and_patch"]
    assert calls[2]["input"]["urls"] == ["http://127.0.0.1:5555/"]


def test_delegate_incomplete_summary_does_not_add_self_verification() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "delegate",
                "ok": True,
                "args": {"action": "wait"},
                "result_preview": '{"ok": true, "task_ok": false}',
                "result_summary": {
                    "task_ok": False,
                    "success_count": 0,
                    "incomplete_count": 1,
                    "failed_count": 0,
                    "interrupted_count": 1,
                    "result_items": [
                        {
                            "task_id": "fix-form",
                            "ok": False,
                            "outputs_complete": False,
                            "staged_project_files": ["_env/app.js"],
                        }
                    ],
                },
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["delegate", "edit"]
    assert not any(call["name"] == "delegate_self_verification" for call in calls)


def test_helper_owned_env_apply_adds_producer_boundary_execute_evidence() -> None:
    calls = tool_calls_from_events(
        [
            {
                "kind": "main_tool_done",
                "tool": "env_apply_create",
                "ok": True,
                "args": {"path": "api_notes.md", "workspace_path": "_env/api_notes.md"},
                "result_preview": (
                    '{"ok": true, "action": "env_apply_create", "path": "api_notes.md", '
                    '"workspace_path": "_env/api_notes.md", '
                    '"acceptance_fact": {"helper_owned": true}}'
                ),
            },
        ],
        [],
    )

    assert [call["family"] for call in calls] == ["edit", "execute"]
    assert calls[0]["name"] == "env_apply_create"
    assert calls[0]["mutating"] is True
    assert calls[1]["name"] == "env_apply_create_producer_boundary"
    assert calls[1]["mutating"] is False
    assert calls[1]["input"]["_preserve_family"] == "execute"
    assert calls[1]["input"]["source"] == "helper_owned_env_apply"
    assert calls[1]["input"]["paths"] == ["api_notes.md", "_env/api_notes.md"]


def test_command_driven_browser_evidence_detects_scripted_automation_text_output():
    """Round 16: playwright page-text extraction prints neither HTML markup nor
    an HTTP status line, so the old HTML/status markers missed it and the run
    lost its browser family (20260611_*p35088, tool_fit collapsed to 0)."""
    from stress_tools.export_clawbench_partner_trace import _command_driven_browser_evidence

    automation_call = {
        "name": "env_run",
        "family": "execute",
        "success": True,
        "input": {"python_code": (
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    browser = p.chromium.launch()\n"
            "    page = browser.new_page()\n"
            "    page.goto('http://127.0.0.1:61032/')\n"
            "    print('===PAGE_TEXT_START===')\n"
            "    print(page.inner_text('body'))\n"
        )},
        "output": "===PAGE_TEXT_START===\nReporting API\nThe current GA endpoint is /v2/reports",
    }
    evidence = _command_driven_browser_evidence(automation_call)
    assert evidence is not None
    assert evidence["urls"] == ["http://127.0.0.1:61032/"]

    # Plain pytest run with a URL in output stays non-browser.
    pytest_call = {
        "name": "env_run",
        "family": "execute",
        "success": True,
        "input": {"command": "pytest tests/test_report_client.py -v"},
        "output": "test_endpoint PASSED comparing http://127.0.0.1:61032/v2/reports",
    }
    assert _command_driven_browser_evidence(pytest_call) is None
