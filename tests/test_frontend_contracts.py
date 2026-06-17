from pathlib import Path


def test_frontend_workflow_fallback_defines_background_event_before_use():
    src = Path("agent_frontend/src/app.js").read_text(encoding="utf-8")
    normal_branch = src.split('} else if (event === "error") {', 1)[1].split("function finishRun", 1)[0]

    assert "const backgroundEvent = isBackgroundTaskEvent(data, event);" in normal_branch
    assert normal_branch.index("const backgroundEvent = isBackgroundTaskEvent(data, event);") < normal_branch.index(
        'helperKind: backgroundEvent ? "background"'
    )


def test_frontend_insert_mode_label_matches_backend_judged_continue():
    src = Path("agent_frontend/src/app.js").read_text(encoding="utf-8")

    assert "插入后由后端判定续跑" in src
    assert "插入后排队下一轮" not in src
