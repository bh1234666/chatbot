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


async def test_fetch_group_file_reports_pending_when_file_not_downloaded(tmp_path, monkeypatch):
    from app.memory import group_files as gf

    meta = {
        "filename": "doc.txt",
        "file_size": 5,
        "download_status": "pending",
    }
    conn = FakeConn()
    conn.fetchrow_results = [
        {"file_metadata": json.dumps(meta, ensure_ascii=False)},
        {"workspace_path": "group_files/doc.txt", "file_id": "f1", "busid": 0, "file_name": "doc.txt"},
    ]
    workspace = tmp_path / "temp"
    workspace.mkdir()

    monkeypatch.setattr(gf, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(gf, "_get_workspace_root", lambda: tmp_path)

    result = await gf.fetch_group_file("kb1", "archive", "group", str(workspace))

    assert result["ok"] is False
    assert "尚未下载完成" in result["error"]


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

    assert result == {
        "ok": True,
        "path": "doc.txt",
        "filename": "doc.txt",
        "note": "already in workspace, reused existing copy",
    }


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

    assert result == {"ok": True, "path": "doc_from_group.txt", "filename": "doc.txt"}
    assert (workspace / "doc_from_group.txt").read_text(encoding="utf-8") == "new content"
