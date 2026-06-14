import asyncio
from pathlib import Path

import pytest

from app.llm.tools import workspace as ws_tool
from app.llm.tools.workspace_file_ops import handle_read_file


def test_isolated_temp_workspaces_do_not_share_current_files(tmp_path):
    main_ws = tmp_path / "main"
    main_ws.mkdir()
    (main_ws / "durable.txt").write_text("shared durable", encoding="utf-8")

    temp_a = Path(ws_tool.ensure_temp_workspace(
        str(main_ws),
        session_tag="arch:group:user-a:trace-a",
        isolate_session=True,
    ))
    temp_b = Path(ws_tool.ensure_temp_workspace(
        str(main_ws),
        session_tag="arch:group:user-b:trace-b",
        isolate_session=True,
    ))

    assert temp_a != temp_b
    assert temp_a.parent.name == "_sessions"
    assert temp_a.parent.parent == main_ws / ".temp"
    assert (temp_a / "durable.txt").read_text(encoding="utf-8") == "shared durable"
    assert (temp_b / "durable.txt").read_text(encoding="utf-8") == "shared durable"

    (temp_a / "only_a.txt").write_text("a current file", encoding="utf-8")
    assert not (temp_b / "only_a.txt").exists()
    assert "only_a.txt" in ws_tool.list_generated_files(str(temp_a))
    assert "only_a.txt" not in ws_tool.list_generated_files(str(temp_b))


def test_main_workspace_listing_excludes_session_temp_files(tmp_path):
    main_ws = tmp_path / "main"
    main_ws.mkdir()
    (main_ws / "durable.txt").write_text("durable", encoding="utf-8")
    temp_a = Path(ws_tool.ensure_temp_workspace(
        str(main_ws),
        session_tag="arch:group:user-a:trace-a",
        isolate_session=True,
    ))
    (temp_a / "_voice_trace_a.wav").write_bytes(b"RIFF....WAVE")

    files = ws_tool.list_generated_files(str(main_ws))

    assert "durable.txt" in files
    assert all("_voice_trace_a.wav" not in f for f in files)
    assert all(not f.startswith(".temp/") for f in files)
    temp_root_files = ws_tool.list_generated_files(str(main_ws / ".temp"))
    assert all("_voice_trace_a.wav" not in f for f in temp_root_files)
    assert all(not f.startswith("_sessions/") for f in temp_root_files)


def test_workspace_registry_unregisters_only_matching_workspace():
    ws_tool._workspace_registry.clear()
    group_key = "arch:group"
    ws_tool.register_workspace(group_key, "A")
    ws_tool.register_workspace(group_key, "B")

    assert ws_tool.get_workspace(group_key) == "B"
    assert ws_tool.get_registered_workspaces(group_key) == ["B", "A"]

    ws_tool.unregister_workspace(group_key, "A")
    assert ws_tool.get_workspace(group_key) == "B"
    assert ws_tool.get_registered_workspaces(group_key) == ["B"]

    ws_tool.unregister_workspace(group_key, "B")
    assert ws_tool.get_workspace(group_key) is None
    assert ws_tool.get_registered_workspaces(group_key) == []


def test_session_temp_read_file_falls_back_to_persistent_root(tmp_path):
    main_ws = tmp_path / "main"
    main_ws.mkdir()
    session_ws = ws_tool.ensure_temp_workspace(
        str(main_ws),
        session_tag="arch:group:user:trace",
        isolate_session=True,
    )
    (main_ws / "persisted.txt").write_text("from persistent root", encoding="utf-8")

    result = asyncio.run(handle_read_file(session_ws, "persisted.txt"))

    assert result["ok"] is True
    assert result.get("_p47_main_fallback") is True
    assert "from persistent root" in result["content"]


def test_orchestrator_file_urls_use_workspace_token_helper():
    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "def _workspace_file_url" in src
    assert "?workspace_token=" in src
    assert src.count("/v1/chat/files/{req.archive_id}/{req.group_id}/") == 1
    assert src.count("_workspace_file_url(") >= 5


@pytest.mark.asyncio
async def test_download_file_scans_all_registered_workspaces(monkeypatch, tmp_path):
    from app.api import chat

    ws_tool._workspace_registry.clear()
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    ws_a = tmp_path / "session_a"
    ws_b = tmp_path / "session_b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / "artifact.txt").write_text("from a", encoding="utf-8")

    monkeypatch.setattr(
        chat.ws_tool,
        "get_persistent_workspace_path",
        lambda archive_id, group_id: str(persistent),
    )
    ws_tool.register_workspace("arch:group", str(ws_a))
    ws_tool.register_workspace("arch:group", str(ws_b))

    response = await chat.download_file("arch", "group", "artifact.txt")

    assert Path(response.path) == ws_a / "artifact.txt"


@pytest.mark.asyncio
async def test_download_file_workspace_token_selects_matching_session(monkeypatch, tmp_path):
    from app.api import chat

    ws_tool._workspace_registry.clear()
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    (persistent / "artifact.txt").write_text("from persistent", encoding="utf-8")
    ws_a = tmp_path / "session_a"
    ws_b = tmp_path / "session_b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / "artifact.txt").write_text("from a", encoding="utf-8")
    (ws_b / "artifact.txt").write_text("from b", encoding="utf-8")

    monkeypatch.setattr(
        chat.ws_tool,
        "get_persistent_workspace_path",
        lambda archive_id, group_id: str(persistent),
    )
    ws_tool.register_workspace("arch:group", str(ws_a))
    ws_tool.register_workspace("arch:group", str(ws_b))

    response = await chat.download_file(
        "arch",
        "group",
        "artifact.txt",
        workspace_token=ws_tool.workspace_token(str(ws_a)),
    )

    assert Path(response.path) == ws_a / "artifact.txt"


@pytest.mark.asyncio
async def test_preview_file_scans_all_registered_workspaces(monkeypatch, tmp_path):
    from app.api import chat

    ws_tool._workspace_registry.clear()
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    ws_a = tmp_path / "session_a"
    ws_b = tmp_path / "session_b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / "artifact.txt").write_text("from a", encoding="utf-8")

    monkeypatch.setattr(
        chat.ws_tool,
        "get_persistent_workspace_path",
        lambda archive_id, group_id: str(persistent),
    )
    ws_tool.register_workspace("arch:group", str(ws_a))
    ws_tool.register_workspace("arch:group", str(ws_b))

    preview = await chat.preview_workspace_file("arch", "group", "artifact.txt")

    assert preview["ok"] is True
    assert preview["content"] == "from a"

    (persistent / "artifact.txt").write_text("from persistent", encoding="utf-8")
    preview = await chat.preview_workspace_file("arch", "group", "artifact.txt")

    assert preview["ok"] is True
    assert preview["content"] == "from persistent"


@pytest.mark.asyncio
async def test_preview_file_workspace_token_selects_matching_session(monkeypatch, tmp_path):
    from app.api import chat

    ws_tool._workspace_registry.clear()
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    ws_a = tmp_path / "session_a"
    ws_b = tmp_path / "session_b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / "artifact.txt").write_text("from a", encoding="utf-8")
    (ws_b / "artifact.txt").write_text("from b", encoding="utf-8")

    monkeypatch.setattr(
        chat.ws_tool,
        "get_persistent_workspace_path",
        lambda archive_id, group_id: str(persistent),
    )
    ws_tool.register_workspace("arch:group", str(ws_a))
    ws_tool.register_workspace("arch:group", str(ws_b))

    preview = await chat.preview_workspace_file(
        "arch",
        "group",
        "artifact.txt",
        workspace_token=ws_tool.workspace_token(str(ws_a)),
    )

    assert preview["ok"] is True
    assert preview["content"] == "from a"
