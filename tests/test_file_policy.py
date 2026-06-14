import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.core.file_policy import classify_file_for_delivery


class FakeClient:
    def __init__(self):
        self.posts = []

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse()


class FakeResponse:
    status_code = 200
    text = ""


def test_file_policy_blocks_executables_and_scripts():
    for name in ("a.exe", "run.bat", "script.ps1", "tool.sh"):
        decision = classify_file_for_delivery(name)
        assert not decision.allowed
        assert decision.status_code == 403


def test_file_policy_allows_safe_documents_and_source():
    for name in ("report.pdf", "notes.md", "data.csv", "main.py", "source.cpp"):
        decision = classify_file_for_delivery(name)
        assert decision.allowed
        assert decision.delivery_kind == "file"


def test_file_policy_classifies_images_and_voice():
    assert classify_file_for_delivery("plot.png").delivery_kind == "image"
    assert classify_file_for_delivery("reply.wav").delivery_kind == "voice"


def test_file_policy_rejects_unknown_extension_by_default():
    decision = classify_file_for_delivery("payload.unknown")
    assert not decision.allowed
    assert decision.status_code == 400


async def test_bridge_detect_media_type_uses_central_policy():
    import napcat_bridge

    assert napcat_bridge._detect_media_type("plot.png") == "image"
    assert napcat_bridge._detect_media_type("reply.wav") == "record"
    assert napcat_bridge._detect_media_type("archive.zip") == "file"
    assert napcat_bridge._detect_media_type("payload.exe") == "blocked"


async def test_bridge_skips_blocked_generated_files_without_link_fallback(monkeypatch):
    import napcat_bridge

    client = FakeClient()
    fallback_calls = []

    async def fake_fallback(*args):
        fallback_calls.append(args)

    monkeypatch.setattr(napcat_bridge, "_send_file_link_fallback", fake_fallback)

    voice_sent = await napcat_bridge._send_generated_files(
        client,
        "123",
        [{"name": "payload.exe", "url": "/files/a/g/payload.exe", "local_path": ""}],
    )

    assert voice_sent is False
    assert len(client.posts) == 1
    assert client.posts[0][0].endswith("/send_group_msg")
    assert "payload.exe" in client.posts[0][1]["json"]["message"]
    assert fallback_calls == []


async def test_chat_download_file_rejects_blocked_extension_from_workspace(tmp_path, monkeypatch):
    from app.api import chat

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    blocked = workspace / "payload.exe"
    blocked.write_bytes(b"MZ")

    monkeypatch.setattr(chat.ws_tool, "get_workspace", lambda _group_key: "")
    monkeypatch.setattr(chat.ws_tool, "get_persistent_workspace_path", lambda _archive_id, _group_id: str(workspace))

    with pytest.raises(chat.HTTPException) as exc:
        await chat.download_file("archive", "group", "payload.exe")

    assert exc.value.status_code == 403
    assert "executable" in exc.value.detail


async def test_chat_download_file_rejects_path_traversal_even_if_file_exists(tmp_path, monkeypatch):
    from app.api import chat

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    monkeypatch.setattr(chat.ws_tool, "get_workspace", lambda _group_key: "")
    monkeypatch.setattr(chat.ws_tool, "get_persistent_workspace_path", lambda _archive_id, _group_id: str(workspace))

    with pytest.raises(chat.HTTPException) as exc:
        await chat.download_file("archive", "group", "../secret.txt")

    assert exc.value.status_code == 404
