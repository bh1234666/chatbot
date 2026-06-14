import json
from io import BytesIO
from pathlib import Path

import pytest

from app.schemas.api import ChatRequest


def test_chat_request_attached_file_ids_is_backward_compatible():
    req = ChatRequest(archive_id="a", group_id="g", user_id="u", message="hello")
    assert req.attached_file_ids == []

    req2 = ChatRequest(
        archive_id="a",
        group_id="g",
        user_id="u",
        message="hello",
        attached_file_ids=["f1"],
    )
    assert req2.attached_file_ids == ["f1"]


def test_bot_file_filename_and_attachment_prefix():
    from app.memory import bot_files

    assert bot_files.safe_upload_filename("../bad:name?.txt") == "bad_name_.txt"
    prefix = bot_files.attachment_prefix([
        {
            "id": "botfile_1",
            "kb_node_id": "c_1",
            "name": "需求.txt",
            "workspace_path": "uploaded_files/需求.txt",
        }
    ])
    assert "[BOT_FILE_ATTACHMENTS]" in prefix
    assert "bot 文件区" in prefix
    assert "kb_node_id=c_1" in prefix


@pytest.mark.asyncio
async def test_save_uploaded_file_registers_workspace_and_indexes(monkeypatch, tmp_path):
    from app.memory import bot_files

    executed = []

    class Tx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Conn:
        def transaction(self):
            return Tx()

        async def execute(self, sql, *args):
            executed.append((sql, args))
            return "OK 1"

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    monkeypatch.setattr(bot_files, "pool", lambda: Pool())
    monkeypatch.setattr(bot_files, "_get_workspace_root", lambda: tmp_path)

    item = await bot_files.save_uploaded_file(
        archive_id="arch",
        group_id="group",
        user_id="u",
        user_name="用户",
        filename="hello.txt",
        content_type="text/plain",
        source=BytesIO("你好".encode("utf-8")),
    )

    assert item["name"] == "hello.txt"
    assert item["workspace_path"].startswith("uploaded_files/")
    assert (tmp_path / "arch" / "group" / item["workspace_path"]).is_file()
    assert any("INSERT INTO cold_nodes" in sql for sql, _ in executed)
    assert any("INSERT INTO synced_files" in sql for sql, _ in executed)

    meta_args = [
        args for sql, args in executed
        if "INSERT INTO cold_nodes" in sql
    ][0]
    meta = json.loads(meta_args[-1])
    assert meta["source"] == "bot_file_area"
    assert meta["download_status"] == "done"


@pytest.mark.asyncio
async def test_delivered_artifact_registry_uses_done_files_only(monkeypatch, tmp_path):
    from app.memory import bot_artifacts

    rows = []

    class Conn:
        async def execute(self, sql, *args):
            if "INSERT INTO bot_delivered_artifacts" in sql:
                rows[:] = [r for r in rows if not (
                    r["archive_id"] == args[0]
                    and r["group_id"] == args[1]
                    and r["artifact_id"] == args[2]
                )]
                rows.append({
                    "archive_id": args[0],
                    "group_id": args[1],
                    "artifact_id": args[2],
                    "file_name": args[3],
                    "file_size": args[4],
                    "delivered_at": args[5],
                    "workspace_path": args[6],
                })
            return "OK 1"

        async def fetch(self, sql, *args):
            return [
                {
                    "artifact_id": r["artifact_id"],
                    "file_name": r["file_name"],
                    "file_size": r["file_size"],
                    "delivered_at": r["delivered_at"],
                    "workspace_path": r["workspace_path"],
                }
                for r in rows
                if r["archive_id"] == args[0]
                and r["group_id"] == args[1]
            ]

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    workspace = tmp_path / "arch" / "group"
    workspace.mkdir(parents=True)
    (workspace / "delivered.txt").write_text("ok", encoding="utf-8")
    (workspace / "internal.txt").write_text("hidden", encoding="utf-8")

    monkeypatch.setattr(bot_artifacts, "pool", lambda: Pool())
    monkeypatch.setattr(bot_artifacts.ws_tool, "get_persistent_workspace_path", lambda _a, _g: str(workspace))

    assert await bot_artifacts.record_delivered_files(
        "arch",
        "group",
        [{"name": "delivered.txt", "rel_path": "delivered.txt"}],
    ) == 1

    items = await bot_artifacts.list_delivered_files("arch", "group")
    assert [item["rel_path"] for item in items] == ["delivered.txt"]
    assert "internal.txt" not in {item["rel_path"] for item in items}
    assert items[0]["download_url"].endswith("/v1/chat/files/arch/group/delivered.txt")
    assert (workspace / ".file_registry.json").is_file()


@pytest.mark.asyncio
async def test_artifacts_endpoint_returns_delivered_registry_not_workspace_scan(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"id": archive_id}

    async def fake_list_delivered_files(archive_id, group_id):
        return [{
            "id": "artifact_1",
            "name": "pushed.txt",
            "rel_path": "pushed.txt",
            "workspace_path": "pushed.txt",
            "download_url": "/v1/chat/files/arch/group/pushed.txt",
        }]

    def fail_workspace_scan(*args, **kwargs):
        raise AssertionError("artifact endpoint must not scan workspace files")

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_artifacts, "list_delivered_files", fake_list_delivered_files)
    monkeypatch.setattr(chat.ws_tool, "list_generated_files", fail_workspace_scan)

    result = await chat.list_workspace_artifacts("arch", "group")

    assert result["items"][0]["rel_path"] == "pushed.txt"


def test_delivered_artifact_url_query_is_not_part_of_rel_path():
    from app.memory import bot_artifacts

    item = bot_artifacts._normalize_done_file(
        "arch",
        "group",
        {
            "name": "delivered.txt",
            "url": "/v1/chat/files/arch/group/delivered.txt?workspace_token=abc123",
        },
    )

    assert item is not None
    assert item["rel_path"] == "delivered.txt"
    assert item["workspace_path"] == "delivered.txt"
