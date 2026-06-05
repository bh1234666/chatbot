import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_environment_maintenance_stage_three_accepts_real_cli_tests(tmp_path):
    from stress_tools.run_environment_maintenance import create_project, validate_project

    project = create_project(tmp_path, 0)
    (project / "tests" / "test_cli.py").write_text(
        "from taskboard.cli import build_parser, main\n\n"
        "def test_version_command(capsys):\n"
        "    assert main(['--version']) == 0\n\n"
        "def test_parser_exists():\n"
        "    assert build_parser().prog == 'taskboard'\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_storage.py").write_text(
        "from taskboard.storage import load_tasks, save_tasks\n\n"
        "def test_storage_round_trip(tmp_path):\n"
        "    path = tmp_path / 'tasks.json'\n"
        "    save_tasks([], path)\n"
        "    assert load_tasks(path) == []\n",
        encoding="utf-8",
    )
    (project / "scripts" / "check_project.py").write_text(
        "from pathlib import Path\n\n"
        "def main():\n"
        "    assert Path('src/taskboard/cli.py').exists()\n"
        "    print('taskboard check ok')\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (project / "README.md").write_text(
        "# TaskBoard\n\nUsage: run commands with pytest during maintenance.\n",
        encoding="utf-8",
    )
    result = validate_project(project, 2)

    assert result["checks"]["required_hits"]["test_cli"] is True
    assert result["checks"]["required_hits"]["test_storage"] is True
    assert result["checks"]["required_hits"]["check_project"] is True
    assert result["ok"] is True


def test_multilang_stage_three_accepts_real_test_files(tmp_path):
    from stress_tools.run_environment_maintenance import create_project, validate_project

    project = create_project(tmp_path, 0, ("multilang",))
    (project / "tests" / "test_metrics.py").write_text(
        "from polybench.metrics import mean\n\n"
        "def test_mean_empty():\n"
        "    assert mean([]) == 0\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_datasets.py").write_text(
        "from pathlib import Path\n"
        "from polybench.datasets import load_numbers\n\n"
        "def test_load_numbers(tmp_path):\n"
        "    p = tmp_path / 'numbers.txt'\n"
        "    p.write_text('1 2 3', encoding='utf-8')\n"
        "    assert load_numbers(p) == [1, 2, 3]\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_reports.py").write_text(
        "from polybench.reports import render_summary\n\n"
        "def test_render_summary_nonempty():\n"
        "    assert render_summary({'rows': 1}).strip()\n",
        encoding="utf-8",
    )

    result = validate_project(project, 2)

    assert result["checks"]["required_hits"]["test_metrics"] is True
    assert result["checks"]["required_hits"]["test_datasets"] is True
    assert result["checks"]["required_hits"]["test_reports"] is True


def test_multilang_build_probe_accepts_environment_skips(tmp_path):
    from textwrap import dedent

    from stress_tools.run_environment_maintenance import create_project, validate_project

    project = create_project(tmp_path, 0, ("multilang",))
    (project / "scripts" / "check_project.py").write_text(
        dedent(
            """
            import compileall

            def main():
                print('gcc SKIP: not installed')
                print('g++ SKIP: not installed')
                print('node FAIL: permission denied (installed but not executable)')
                compileall.compile_dir('src', quiet=1)
                print('python OK: compileall passed')
                print('build SKIP: make not installed')
                print('check_project: FAILURES: node')
                return 1

            if __name__ == '__main__':
                raise SystemExit(main())
            """
        ).lstrip(),
        encoding="utf-8",
    )
    result = validate_project(project, 0)

    assert result["project_check_result"]["returncode"] == 1
    assert result["checks"]["multilang_check_ready"] is True
    assert result["checks"]["required_hits"]["check_project"] is True
    assert result["checks"]["required_hits"]["gcc"] is True
    assert result["checks"]["required_hits"]["g++"] is True
    assert result["checks"]["required_hits"]["node"] is True
    assert result["checks"]["required_hits"]["build"] is True
    assert result["ok"] is True


def test_multilang_build_probe_rejects_real_compile_failures(tmp_path):
    from textwrap import dedent

    from stress_tools.run_environment_maintenance import create_project, validate_project

    project = create_project(tmp_path, 0, ("multilang",))
    (project / "scripts" / "check_project.py").write_text(
        dedent(
            """
            import compileall

            def main():
                print('gcc FAIL: compile failed: undefined reference to sortbench')
                print('g++ SKIP: not installed')
                print('node SKIP: not installed')
                compileall.compile_dir('src', quiet=1)
                print('python OK: compileall passed')
                print('build SKIP: make not installed')
                print('check_project: FAILURES: gcc')
                return 1

            if __name__ == '__main__':
                raise SystemExit(main())
            """
        ).lstrip(),
        encoding="utf-8",
    )
    result = validate_project(project, 0)

    assert result["checks"]["multilang_check_ready"] is True
    assert result["checks"]["required_hits"]["gcc"] is False
    assert result["ok"] is False
    assert "gcc fail" in result["project_check_result"]["blocking_failures"][0]


def test_environment_summary_treats_cancelled_passed_validation_as_non_failure(tmp_path):
    from stress_tools.run_environment_maintenance import ProjectState, create_project, write_summary

    project = create_project(tmp_path / "projects", 0)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "ts": "2026-05-27T20:00:00",
        "kind": "environment_call",
        "project": str(project),
        "stage": 0,
        "turn": 0,
        "ok": False,
        "cancelled": True,
        "error": "cancelled while waiting for environment result",
        "validation_after": {"ok": True},
    }
    (run_dir / "events.jsonl").write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")

    asyncio.run(write_summary(run_dir, [ProjectState(0, project)]))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    assert summary["fail"] == 0
    assert summary["errors"] == 0


def test_round2_prompt_contains_complex_acceptance_closure():
    from app.core.context import ROUND2_SYSTEM_TEMPLATE

    assert "acceptance points" in ROUND2_SYSTEM_TEMPLATE
    assert "verified files" in ROUND2_SYSTEM_TEMPLATE
    assert "failure signals" in ROUND2_SYSTEM_TEMPLATE
    assert "验收闭环" in ROUND2_SYSTEM_TEMPLATE
    assert "失败续作" in ROUND2_SYSTEM_TEMPLATE
