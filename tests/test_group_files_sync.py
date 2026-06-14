import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeConn:
    def __init__(self):
        self.inserts = []
        self.fetchrow_results = []
        self.executes = []

    async def fetch(self, sql, *args):
        if "SELECT file_name, file_size FROM synced_files" in sql:
            return []
        return []

    async def fetchrow(self, sql, *args):
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return None

    async def execute(self, sql, *args):
        self.inserts.append((sql, args))
        self.executes.append((sql, args))
        return "INSERT 0 1"

    def transaction(self):
        return FakeTransaction()


class FakeTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


async def test_sync_group_files_schedules_only_limited_new_downloads(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    conn = FakeConn()
    scheduled = []
    items = [
        gf.GroupFileItem(
            file_id=f"file-{idx}",
            file_name=f"doc-{idx}.txt",
            file_size=idx + 1,
            upload_time=2_000_000_000,
            uploader_name="Alice",
        )
        for idx in range(gf.MAX_BG_DOWNLOADS + 2)
    ]

    async def fake_heal(_archive_id, _group_id):
        return 0

    async def fake_fetch(_group_id):
        return items

    async def fake_create_node(archive_id, group_id, item):
        return f"kb-{item.file_id}"

    async def fake_bg(**_kwargs):
        return None

    def fake_schedule(coro, *, name=None):
        scheduled.append(name)
        coro.close()
        return None

    monkeypatch.setattr(gf, "_last_sync_time", {})
    monkeypatch.setattr(gf, "_in_flight_downloads", {})
    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(gf, "_heal_pending_nodes", fake_heal)
    monkeypatch.setattr(gf, "fetch_group_files", fake_fetch)
    monkeypatch.setattr(gf, "_create_pending_kb_node", fake_create_node)
    monkeypatch.setattr(gf, "_bg_download_and_index", fake_bg)
    monkeypatch.setattr(gf, "schedule", fake_schedule)

    synced = await gf.sync_group_files("archive", "group")

    assert synced == gf.MAX_BG_DOWNLOADS + 2
    assert scheduled == [
        f"group_file_download:archive:group:file-{idx}"
        for idx in range(gf.MAX_BG_DOWNLOADS)
    ]
    assert set(gf._in_flight_downloads) == {
        ("archive", f"file-{idx}") for idx in range(gf.MAX_BG_DOWNLOADS)
    }
    assert len(conn.inserts) == gf.MAX_BG_DOWNLOADS + 2


async def test_sync_group_files_sets_cooldown_for_empty_success_but_not_unavailable(monkeypatch):
    from app.memory import group_files as gf

    fetch_calls = 0

    async def fake_heal(_archive_id, _group_id):
        return 0

    async def empty_fetch(_group_id):
        nonlocal fetch_calls
        fetch_calls += 1
        return []

    async def unavailable_fetch(_group_id):
        nonlocal fetch_calls
        fetch_calls += 1
        raise gf.NapCatUnavailable("down")

    monkeypatch.setattr(gf, "_heal_pending_nodes", fake_heal)
    monkeypatch.setattr(gf, "_last_sync_time", {})
    monkeypatch.setattr(gf, "fetch_group_files", empty_fetch)

    assert await gf.sync_group_files("archive", "group") == 0
    assert await gf.sync_group_files("archive", "group") == 0
    assert fetch_calls == 1

    fetch_calls = 0
    monkeypatch.setattr(gf, "_last_sync_time", {})
    monkeypatch.setattr(gf, "fetch_group_files", unavailable_fetch)

    assert await gf.sync_group_files("archive", "group") == 0
    assert await gf.sync_group_files("archive", "group") == 0
    assert fetch_calls == 2


async def test_pending_group_file_metadata_keeps_uploader_id(monkeypatch):
    from app.memory import group_files as gf

    conn = FakeConn()
    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))

    node_id = await gf._create_pending_kb_node(
        archive_id="archive",
        group_id="group",
        item=gf.GroupFileItem(
            file_id="file-1",
            file_name="task.docx",
            file_size=1024,
            upload_time=2_000_000_000,
            uploader_uin=12345,
            uploader_name="SameNick",
        ),
    )

    assert node_id
    metadata = json.loads(conn.executes[-1][1][-1])
    assert metadata["uploader_uin"] == 12345
    assert metadata["uploader_name"] == "SameNick"


async def test_done_group_file_metadata_keeps_uploader_id(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    archive_dir = tmp_path / "archive" / "group" / "group_files"
    archive_dir.mkdir(parents=True)
    local_file = archive_dir / "photo.png"
    local_file.write_bytes(b"png")
    conn = FakeConn()

    async def fake_download(_item, _group_id, _ws_dir):
        return str(local_file)

    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(gf, "_download_file", fake_download)

    await gf._bg_download_and_index(
        archive_id="archive",
        group_id="group",
        kb_node_id="kb1",
        item=gf.GroupFileItem(
            file_id="file-1",
            file_name="photo.png",
            file_size=3,
            upload_time=2_000_000_000,
            uploader_uin=12345,
            uploader_name="SameNick",
        ),
        workspace_rel="group_files/photo.png",
        ws_dir=str(archive_dir),
    )

    metadata = json.loads(conn.executes[-1][1][2])
    assert metadata["download_status"] == "done"
    assert metadata["uploader_uin"] == 12345
    assert metadata["uploader_name"] == "SameNick"


async def test_fetch_group_file_reports_pending_when_file_not_downloaded(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    meta = {
        "filename": "doc.txt",
        "file_size": 5,
        "download_status": "pending",
        "uploader_uin": "u1",
        "uploader_name": "Alice",
        "upload_time": 2_000_000_000,
    }
    conn = FakeConn()
    conn.fetchrow_results = [
        {"file_metadata": json.dumps(meta, ensure_ascii=False)},
        {
            "workspace_path": "group_files/doc.txt",
            "file_id": "f1",
            "busid": 0,
            "file_name": "doc.txt",
            "file_size": 5,
            "upload_time": 2_000_000_000,
            "uploader_uin": "u1",
            "uploader_name": "Alice",
        },
    ]
    workspace = tmp_path / "temp"
    workspace.mkdir()

    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    result = await gf.fetch_group_file(
        "kb1",
        "archive",
        "group",
        str(workspace),
        current_user_id="u1",
        current_user_name="Alice",
    )

    assert result["ok"] is False
    assert "尚未下载完成" in result["error"]
    assert result["source_attribution"]["scope"] == "shared_group_file"
    assert result["source_attribution"]["kb_node_id"] == "kb1"
    assert result["source_attribution"]["current_user_relation"] == "same_speaker_upload"
    assert result["source_attribution"]["upload_time"] == 2_000_000_000


async def test_fetch_group_file_reuses_existing_workspace_copy(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    source_dir = tmp_path / "archive" / "group" / "group_files"
    source_dir.mkdir(parents=True)
    source = source_dir / "doc.txt"
    source.write_text("hello", encoding="utf-8")
    workspace = tmp_path / "temp"
    workspace.mkdir()
    existing = workspace / "doc.txt"
    existing.write_text("hello", encoding="utf-8")
    meta = {
        "filename": "doc.txt",
        "file_size": 5,
        "download_status": "done",
    }
    conn = FakeConn()
    conn.fetchrow_results = [
        {"file_metadata": json.dumps(meta, ensure_ascii=False)},
        {"workspace_path": "group_files/doc.txt", "file_id": "f1", "busid": 0, "file_name": "doc.txt"},
    ]

    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    result = await gf.fetch_group_file("kb1", "archive", "group", str(workspace))

    assert result["ok"] is True
    assert result["path"] == "doc.txt"
    assert result["filename"] == "doc.txt"
    assert result["note"] == "already in workspace, reused existing copy"
    assert result["source_attribution"]["scope"] == "shared_group_file"
    assert result["source_attribution"]["kb_node_id"] == "kb1"


async def test_fetch_group_file_copies_with_suffix_on_conflicting_workspace_file(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    source_dir = tmp_path / "archive" / "group" / "group_files"
    source_dir.mkdir(parents=True)
    source = source_dir / "doc.txt"
    source.write_text("new content", encoding="utf-8")
    workspace = tmp_path / "temp"
    workspace.mkdir()
    (workspace / "doc.txt").write_text("old", encoding="utf-8")
    meta = {
        "filename": "doc.txt",
        "file_size": 11,
        "download_status": "done",
    }
    conn = FakeConn()
    conn.fetchrow_results = [
        {"file_metadata": json.dumps(meta, ensure_ascii=False)},
        {"workspace_path": "group_files/doc.txt", "file_id": "f1", "busid": 0, "file_name": "doc.txt"},
    ]

    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    result = await gf.fetch_group_file("kb1", "archive", "group", str(workspace))

    assert result["ok"] is True
    assert result["path"] == "doc_from_group.txt"
    assert result["filename"] == "doc.txt"
    assert result["source_attribution"]["scope"] == "shared_group_file"
    assert (workspace / "doc_from_group.txt").read_text(encoding="utf-8") == "new content"


async def test_fetch_group_file_does_not_reuse_same_size_different_content(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    source_dir = tmp_path / "archive" / "group" / "group_files"
    source_dir.mkdir(parents=True)
    source = source_dir / "task.txt"
    source.write_text("BBBBB", encoding="utf-8")
    workspace = tmp_path / "temp"
    workspace.mkdir()
    (workspace / "task.txt").write_text("AAAAA", encoding="utf-8")
    meta = {
        "filename": "task.txt",
        "file_size": 5,
        "download_status": "done",
        "uploader_uin": "u2",
        "uploader_name": "SameNick",
    }
    conn = FakeConn()
    conn.fetchrow_results = [
        {"file_metadata": json.dumps(meta, ensure_ascii=False)},
        {
            "workspace_path": "group_files/task.txt",
            "file_id": "f2",
            "busid": 0,
            "file_name": "task.txt",
            "file_size": 5,
            "uploader_uin": "u2",
            "uploader_name": "SameNick",
        },
    ]

    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    result = await gf.fetch_group_file(
        "kb2",
        "archive",
        "group",
        str(workspace),
        current_user_id="u2",
        current_user_name="SameNick",
    )

    assert result["ok"] is True
    assert result["path"] == "task_from_group.txt"
    assert result["source_attribution"]["current_user_match"] is True
    assert (workspace / "task.txt").read_text(encoding="utf-8") == "AAAAA"
    assert (workspace / "task_from_group.txt").read_text(encoding="utf-8") == "BBBBB"


async def test_fetch_group_file_returns_current_user_source_attribution(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    source_dir = tmp_path / "archive" / "group" / "group_files"
    source_dir.mkdir(parents=True)
    (source_dir / "source.txt").write_text("source", encoding="utf-8")
    workspace = tmp_path / "temp"
    workspace.mkdir()
    meta = {
        "filename": "source.txt",
        "file_size": 6,
        "download_status": "done",
        "uploader_uin": "u2",
        "uploader_name": "Bob",
        "upload_time": 2_000_000_000,
    }

    def make_conn():
        conn = FakeConn()
        conn.fetchrow_results = [
            {"file_metadata": json.dumps(meta, ensure_ascii=False)},
            {
                "workspace_path": "group_files/source.txt",
                "file_id": "f1",
                "busid": 0,
                "file_name": "source.txt",
                "file_size": 6,
                "upload_time": 2_000_000_000,
                "uploader_uin": "u2",
                "uploader_name": "Bob",
            },
        ]
        return conn

    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    monkeypatch.setattr(gf, "pool", lambda: FakePool(make_conn()))
    same_user = await gf.fetch_group_file(
        "kb1",
        "archive",
        "group",
        str(workspace),
        current_user_id="u2",
    )
    assert same_user["source_attribution"]["current_user_match"] is True
    assert same_user["source_attribution"]["current_user_relation"] == "same_speaker_upload"

    monkeypatch.setattr(gf, "pool", lambda: FakePool(make_conn()))
    other_user = await gf.fetch_group_file(
        "kb1",
        "archive",
        "group",
        str(workspace),
        current_user_id="u3",
    )
    assert other_user["source_attribution"]["current_user_match"] is False
    assert other_user["source_attribution"]["current_user_relation"] == "other_user_upload"


async def test_fetch_group_file_same_nickname_missing_uploader_id_is_unknown(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    source_dir = tmp_path / "archive" / "group" / "group_files"
    source_dir.mkdir(parents=True)
    (source_dir / "source.txt").write_text("source", encoding="utf-8")
    workspace = tmp_path / "temp"
    workspace.mkdir()
    meta = {
        "filename": "source.txt",
        "file_size": 6,
        "download_status": "done",
        "uploader_name": "SameNick",
        "upload_time": 2_000_000_000,
    }

    def make_conn():
        conn = FakeConn()
        conn.fetchrow_results = [
            {"file_metadata": json.dumps(meta, ensure_ascii=False)},
            {
                "workspace_path": "group_files/source.txt",
                "file_id": "f1",
                "busid": 0,
                "file_name": "source.txt",
                "file_size": 6,
                "upload_time": 2_000_000_000,
                "uploader_uin": "",
                "uploader_name": "SameNick",
            },
        ]
        return conn

    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(gf, "pool", lambda: FakePool(make_conn()))

    result = await gf.fetch_group_file(
        "kb1",
        "archive",
        "group",
        str(workspace),
        current_user_id="u2",
        current_user_name="SameNick",
    )

    assert result["source_attribution"]["current_user_match"] is None
    assert result["source_attribution"]["current_user_relation"] == "unknown_uploader_relation"


async def test_fetch_group_file_keeps_same_basename_different_uploaders_distinct(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    source_dir = tmp_path / "archive" / "group" / "group_files"
    source_dir.mkdir(parents=True)
    (source_dir / "alice_task.txt").write_text("AAAAA", encoding="utf-8")
    (source_dir / "bob_task.txt").write_text("BBBBB", encoding="utf-8")
    workspace = tmp_path / "temp"
    workspace.mkdir()

    def make_conn(*, node_id: str, workspace_path: str, uploader_uin: str, uploader_name: str):
        meta = {
            "filename": "task.txt",
            "file_size": 5,
            "download_status": "done",
            "uploader_uin": uploader_uin,
            "uploader_name": uploader_name,
            "upload_time": 2_000_000_000,
        }
        conn = FakeConn()
        conn.fetchrow_results = [
            {"file_metadata": json.dumps(meta, ensure_ascii=False)},
            {
                "workspace_path": workspace_path,
                "file_id": f"file-{node_id}",
                "busid": 0,
                "file_name": "task.txt",
                "file_size": 5,
                "upload_time": 2_000_000_000,
                "uploader_uin": uploader_uin,
                "uploader_name": uploader_name,
            },
        ]
        return conn

    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    monkeypatch.setattr(
        gf,
        "pool",
        lambda: FakePool(make_conn(
            node_id="alice",
            workspace_path="group_files/alice_task.txt",
            uploader_uin="u1",
            uploader_name="SameNick",
        )),
    )
    first = await gf.fetch_group_file(
        "kb_alice",
        "archive",
        "group",
        str(workspace),
        current_user_id="u2",
        current_user_name="SameNick",
    )

    monkeypatch.setattr(
        gf,
        "pool",
        lambda: FakePool(make_conn(
            node_id="bob",
            workspace_path="group_files/bob_task.txt",
            uploader_uin="u2",
            uploader_name="SameNick",
        )),
    )
    second = await gf.fetch_group_file(
        "kb_bob",
        "archive",
        "group",
        str(workspace),
        current_user_id="u2",
        current_user_name="SameNick",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["path"] == "task.txt"
    assert second["path"] == "task_from_group.txt"
    assert (workspace / "task.txt").read_text(encoding="utf-8") == "AAAAA"
    assert (workspace / "task_from_group.txt").read_text(encoding="utf-8") == "BBBBB"
    assert first["source_attribution"]["kb_node_id"] == "kb_alice"
    assert first["source_attribution"]["current_user_relation"] == "other_user_upload"
    assert second["source_attribution"]["kb_node_id"] == "kb_bob"
    assert second["source_attribution"]["current_user_relation"] == "same_speaker_upload"


async def test_fetch_group_file_handler_passes_current_user_id(monkeypatch):
    from app.llm.tools import registry

    captured = {}

    async def fake_fetch_group_file(
        kb_node_id,
        archive_id,
        group_id,
        workspace_dir,
        *,
        current_user_id="",
        current_user_name="",
    ):
        captured.update({
            "kb_node_id": kb_node_id,
            "archive_id": archive_id,
            "group_id": group_id,
            "workspace_dir": workspace_dir,
            "current_user_id": current_user_id,
            "current_user_name": current_user_name,
        })
        return {
            "ok": True,
            "path": "doc.txt",
            "source_attribution": {"current_user_relation": "same_speaker_upload"},
        }

    monkeypatch.setattr(registry.gf_mem, "fetch_group_file", fake_fetch_group_file)

    raw = await registry._handle_fetch_group_file(
        "archive",
        "group",
        "u2",
        "",
        "workspace",
        {"kb_node_id": "kb1"},
    )
    result = json.loads(raw)

    assert result["ok"] is True
    assert captured == {
        "kb_node_id": "kb1",
        "archive_id": "archive",
        "group_id": "group",
        "workspace_dir": "workspace",
        "current_user_id": "u2",
        "current_user_name": "",
    }


async def test_fetch_group_file_dispatch_passes_current_user_name(monkeypatch):
    from app.llm.tools import registry

    captured = {}

    async def fake_fetch_group_file(
        kb_node_id,
        archive_id,
        group_id,
        workspace_dir,
        *,
        current_user_id="",
        current_user_name="",
    ):
        captured.update({
            "kb_node_id": kb_node_id,
            "archive_id": archive_id,
            "group_id": group_id,
            "workspace_dir": workspace_dir,
            "current_user_id": current_user_id,
            "current_user_name": current_user_name,
        })
        return {
            "ok": True,
            "path": "doc.txt",
            "source_attribution": {"current_user_relation": "same_speaker_upload"},
        }

    monkeypatch.setattr(registry.gf_mem, "fetch_group_file", fake_fetch_group_file)

    raw = await registry.dispatch(
        "fetch_group_file",
        {"kb_node_id": "kb1"},
        archive_id="archive",
        group_id="group",
        user_id="u2",
        user_name="Bob",
        workspace_dir="workspace",
    )
    result = json.loads(raw)

    assert result["ok"] is True
    assert captured == {
        "kb_node_id": "kb1",
        "archive_id": "archive",
        "group_id": "group",
        "workspace_dir": "workspace",
        "current_user_id": "u2",
        "current_user_name": "Bob",
    }


async def test_search_files_handler_adds_current_user_source_relation(monkeypatch, tmp_path):
    from app.llm.tools import registry

    async def fake_search_files(_archive_id, _group_id, _query, *, limit=10):
        return [{
            "id": "kb1",
            "filename": "task.docx",
            "headline": "历史任务文档",
            "content": "这个历史文件摘要包含实验报告、误差分析和最终结论。",
            "archive_id": "archive",
            "group_id": "group",
            "uploader_name": "SameNick",
            "uploader_uin": "u2",
        }]

    monkeypatch.setattr(registry.kb_mem, "search_files", fake_search_files)

    raw = await registry._handle_search_files(
        "archive",
        "group",
        str(tmp_path),
        {"query": "task", "limit": 5},
        current_user_id="u2",
        current_user_name="SameNick",
    )
    result = json.loads(raw)

    attr = result["items"][0]["source_attribution"]
    assert attr["scope"] == "shared_group_file_index"
    assert attr["current_user_match"] is True
    assert attr["current_user_relation"] == "same_speaker_upload"
    assert result["items"][0]["content"] == "这个历史文件摘要包含实验报告、误差分析和最终结论。"
    assert result["items"][0]["headline"] == "历史任务文档"


async def test_search_files_handler_distinguishes_same_nickname_same_filename_users(monkeypatch, tmp_path):
    from app.llm.tools import registry

    async def fake_search_files(_archive_id, _group_id, _query, *, limit=10):
        return [
            {
                "id": "current_user_file",
                "filename": "task.docx",
                "archive_id": "archive",
                "group_id": "group",
                "uploader_name": "SameNick",
                "uploader_uin": "u2",
            },
            {
                "id": "other_user_file",
                "filename": "task.docx",
                "archive_id": "archive",
                "group_id": "group",
                "uploader_name": "SameNick",
                "uploader_uin": "u1",
            },
        ]

    monkeypatch.setattr(registry.kb_mem, "search_files", fake_search_files)

    raw = await registry._handle_search_files(
        "archive",
        "group",
        str(tmp_path),
        {"query": "task.docx", "limit": 5},
        current_user_id="u2",
        current_user_name="SameNick",
    )
    result = json.loads(raw)
    by_id = {item["id"]: item["source_attribution"] for item in result["items"]}

    assert by_id["current_user_file"]["current_user_match"] is True
    assert by_id["current_user_file"]["current_user_relation"] == "same_speaker_upload"
    assert by_id["other_user_file"]["current_user_match"] is False
    assert by_id["other_user_file"]["current_user_relation"] == "other_user_upload"


async def test_search_files_handler_same_nickname_missing_uploader_id_is_unknown(monkeypatch, tmp_path):
    from app.llm.tools import registry

    async def fake_search_files(_archive_id, _group_id, _query, *, limit=10):
        return [{
            "id": "unknown_owner_file",
            "filename": "task.docx",
            "archive_id": "archive",
            "group_id": "group",
            "uploader_name": "SameNick",
        }]

    monkeypatch.setattr(registry.kb_mem, "search_files", fake_search_files)

    raw = await registry._handle_search_files(
        "archive",
        "group",
        str(tmp_path),
        {"query": "task.docx", "limit": 5},
        current_user_id="u2",
        current_user_name="SameNick",
    )
    result = json.loads(raw)
    attr = result["items"][0]["source_attribution"]

    assert attr["current_user_match"] is None
    assert attr["current_user_relation"] == "unknown_uploader_relation"
