from pathlib import Path


def test_environment_maintenance_creates_diverse_project_types(tmp_path: Path):
    from stress_tools.run_environment_maintenance import (
        create_project,
        detect_project_kind,
        validate_project,
    )

    projects = [create_project(tmp_path, i) for i in range(4)]
    kinds = [detect_project_kind(p) for p in projects]

    assert kinds == ["taskboard", "snake", "dataset", "multilang"]
    for project, kind in zip(projects, kinds):
        result = validate_project(project, 0)
        assert result["kind"] == kind
        assert result["compile_result"]["returncode"] == 0
        assert result["import_result"]["returncode"] == 0
        assert result["checks"]["has_complex_tree"] is True


def test_environment_maintenance_project_specific_required_hits(tmp_path: Path):
    from stress_tools.run_environment_maintenance import create_project, validate_project

    snake = create_project(tmp_path, 1)
    dataset = create_project(tmp_path, 2)

    snake_result = validate_project(snake, 0)
    dataset_result = validate_project(dataset, 0)

    assert "collision" in snake_result["checks"]["required_hits"]
    assert "load_rows" not in snake_result["checks"]["required_hits"]
    assert "load_rows" in dataset_result["checks"]["required_hits"]
    assert "collision" not in dataset_result["checks"]["required_hits"]


def test_environment_maintenance_compileall_hit_uses_real_compile_result(tmp_path: Path):
    from stress_tools.run_environment_maintenance import create_project, validate_project

    snake = create_project(tmp_path, 1)
    result = validate_project(snake, 0)

    assert result["compile_result"]["returncode"] == 0
    assert result["checks"]["required_hits"]["compileall"] is True


def test_complex_stress_summary_includes_environment_result_files(tmp_path: Path):
    from stress_tools.run_complex_long_stress import summarize_direct

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "kind": "direct_result",
        "turn": 0,
        "task": "env_task",
        "result": {
            "ok": True,
            "latency_sec": 12.5,
            "event_counts": {"workflow": 3},
            "text": "done",
            "errors": [],
        },
        "artifacts": {"ok": True, "items": []},
        "result_files": [
            {"name": "deliverables_x.zip", "url": "/v1/chat/files/a/g/deliverables_x.zip"}
        ],
    }
    (run_dir / "direct_events.jsonl").write_text(
        __import__("json").dumps(event, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary = summarize_direct(run_dir)

    assert summary["tasks"][0]["artifact_count"] == 0
    assert summary["tasks"][0]["result_files"][0]["name"] == "deliverables_x.zip"


def test_complex_stress_http_5xx_counter_ignores_trace_ids():
    from stress_tools.run_complex_long_stress import count_explicit_http_5xx

    text = (
        "[502a801be89b4942] trace id only\n"
        "llm retry 1/3: HTTP 502\n"
        "status=503 body='upstream unavailable'\n"
        "status_code=502\n"
    )

    assert count_explicit_http_5xx(text) == {"502": 2, "503": 1}
