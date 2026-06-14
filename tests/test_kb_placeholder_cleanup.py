import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeConn:
    def __init__(self):
        self.deleted_ids = []
        self.rows = [
            {
                "id": "placeholder_same_file",
                "headline": "包涵 上传了 hello.c",
                "content": "正在后台下载和分析中，摘要稍后就会出现在这里。",
                "file_metadata": json.dumps({"filename": "hello.c", "uploader_name": "包涵"}, ensure_ascii=False),
            },
            {
                "id": "fresh_same_file",
                "headline": "C 语言 Hello World 示例",
                "content": "这个文件展示基础 main 函数与 printf 输出。",
                "file_metadata": json.dumps({"filename": "hello.c", "uploader_name": "包涵", "workspace_path": "group_files/hello.c"}, ensure_ascii=False),
            },
            {
                "id": "placeholder_without_fresh",
                "headline": "包涵 上传了 pending.txt",
                "content": "正在后台下载和分析中，摘要稍后就会出现在这里。",
                "file_metadata": json.dumps({"filename": "pending.txt", "uploader_name": "包涵"}, ensure_ascii=False),
            },
            {
                "id": "failed_same_file",
                "headline": "old.docx 下载失败",
                "content": "文件下载失败，暂时无法分析。",
                "file_metadata": json.dumps({
                    "filename": "old.docx",
                    "uploader_name": "包涵",
                    "file_size": 1024,
                    "download_status": "failed",
                }, ensure_ascii=False),
            },
            {
                "id": "done_same_file",
                "headline": "文档内容摘要",
                "content": "这个文档包含实验报告正文和数据表。",
                "file_metadata": json.dumps({
                    "filename": "old.docx",
                    "uploader_name": "包涵",
                    "file_size": 1024,
                    "workspace_path": "group_files/old.docx",
                    "download_status": "done",
                }, ensure_ascii=False),
            },
        ]

    async def fetch(self, sql, *args):
        return self.rows

    async def execute(self, sql, *args):
        self.deleted_ids = args[2]
        return "DELETE 1"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


async def test_cleanup_stale_file_placeholders_deletes_only_covered_placeholders(monkeypatch):
    from app.memory import kb

    conn = FakeConn()
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    deleted = await kb.cleanup_stale_file_placeholders("archive", "group")

    assert deleted == 2
    assert conn.deleted_ids == ["placeholder_same_file", "failed_same_file"]


async def test_load_file_index_keeps_same_name_files_from_same_nickname_different_users(monkeypatch):
    from app.memory import kb

    def row(node_id: str, uploader_uin: str, headline: str) -> dict:
        return {
            "id": node_id,
            "node_type": "file",
            "headline": headline,
            "content": f"summary for {node_id}",
            "salience": 0.5,
            "avoid_mention": False,
            "eff_salience": 1.0,
            "file_metadata": json.dumps({
                "filename": "task.docx",
                "uploader_name": "SameNick",
                "uploader_uin": uploader_uin,
                "file_size": 1024,
                "workspace_path": f"group_files/{node_id}.docx",
                "download_status": "done",
                "upload_time": 2_000_000_000,
            }, ensure_ascii=False),
        }

    conn = FakeConn()
    conn.rows = [
        row("bob_latest", "u2", "Bob current file"),
        row("alice_latest", "u1", "Alice file with colliding nickname"),
        row("bob_old_duplicate", "u2", "Bob older duplicate"),
    ]
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    items = await kb.load_file_index("archive", "group", viewer_user_id="u2")

    ids = [item["id"] for item in items]
    assert ids == ["bob_latest", "alice_latest", "bob_old_duplicate"]
    assert [item["uploader_uin"] for item in items] == ["u2", "u1", "u2"]
    by_id = {item["id"]: item for item in items}
    assert by_id["bob_latest"]["same_name_version_rank"] == 1
    assert by_id["bob_old_duplicate"]["same_name_version_rank"] == 2
    assert by_id["bob_latest"]["same_name_version_count"] == 2
    assert by_id["bob_old_duplicate"]["same_name_version_count"] == 2


async def test_load_file_index_caps_same_uploader_same_name_versions(monkeypatch):
    from app.memory import kb

    def row(idx: int) -> dict:
        return {
            "id": f"alice_v{idx}",
            "node_type": "file",
            "headline": f"Alice task version {idx}",
            "content": f"summary for version {idx}",
            "salience": 0.5,
            "avoid_mention": False,
            "eff_salience": 1.0,
            "file_metadata": json.dumps({
                "filename": "task.docx",
                "uploader_name": "Alice",
                "uploader_uin": "u1",
                "file_size": 1024,
                "workspace_path": f"group_files/alice_v{idx}.docx",
                "download_status": "done",
                "upload_time": 2_000_000_000 - idx,
            }, ensure_ascii=False),
        }

    conn = FakeConn()
    conn.rows = [row(1), row(2), row(3), row(4)]
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    items = await kb.load_file_index("archive", "group", viewer_user_id="u1")

    ids = [item["id"] for item in items]
    assert ids == ["alice_v1", "alice_v2", "alice_v3"]
    assert [item["same_name_version_rank"] for item in items] == [1, 2, 3]
    assert [item["same_name_version_count"] for item in items] == [4, 4, 4]


async def test_search_files_returns_group_file_uploader_metadata(monkeypatch):
    from app.memory import kb

    conn = FakeConn()
    conn.rows = [{
        "id": "file_node",
        "headline": "task document",
        "content": "summary for task document",
        "salience": 1.0,
        "created_at": None,
        "file_metadata": json.dumps({
            "filename": "task.docx",
            "workspace_path": "group_files/task.docx",
            "archive_id": "archive",
            "group_id": "group",
            "uploader_name": "SameNick",
            "uploader_uin": "u2",
            "upload_time": 2_000_000_000,
            "download_status": "done",
        }, ensure_ascii=False),
    }]
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    items = await kb.search_files("archive", "group", "task", limit=5)

    assert items[0]["uploader_name"] == "SameNick"
    assert items[0]["uploader_uin"] == "u2"
    assert items[0]["download_status"] == "done"


async def test_search_files_matches_historical_file_content_summary(monkeypatch):
    from app.memory import kb

    class CapturingConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.sql = ""
            self.args = ()
            self.rows = [{
                "id": "historical_file",
                "headline": "old project note",
                "content": "Summary mentions calibration tables, error analysis, and final conclusion.",
                "salience": 1.0,
                "created_at": None,
                "file_metadata": json.dumps({
                    "filename": "historical_report.docx",
                    "workspace_path": "group_files/historical_report.docx",
                    "archive_id": "archive",
                    "group_id": "group",
                    "uploader_name": "Bob",
                    "uploader_uin": "u2",
                    "upload_time": 1_900_000_000,
                    "download_status": "done",
                }, ensure_ascii=False),
            }]

        async def fetch(self, sql, *args):
            self.sql = sql
            self.args = args
            return self.rows

    conn = CapturingConn()
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    items = await kb.search_files("archive", "group", "calibration", limit=5)

    assert "(headline ILIKE" in conn.sql
    assert "OR content ILIKE" in conn.sql
    assert "%calibration%" in conn.args
    assert items[0]["id"] == "historical_file"
    assert items[0]["content"] == "Summary mentions calibration tables, error analysis, and final conclusion."
    assert items[0]["filename"] == "historical_report.docx"


async def test_search_files_matches_historical_file_summary_content(monkeypatch):
    from app.memory import kb

    class SearchConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.sql = ""
            self.args = ()

        async def fetch(self, sql, *args):
            self.sql = sql
            self.args = args
            return [{
                "id": "old_report",
                "headline": "历史实验报告",
                "content": "这个历史文件包含误差分析、数据表和最终结论。",
                "salience": 0.5,
                "created_at": None,
                "file_metadata": json.dumps({
                    "filename": "experiment_report.docx",
                    "workspace_path": "group_files/experiment_report.docx",
                    "archive_id": "archive",
                    "group_id": "group",
                    "uploader_name": "Bob",
                    "uploader_uin": "u2",
                    "upload_time": 1_700_000_000,
                    "download_status": "done",
                }, ensure_ascii=False),
            }]

    conn = SearchConn()
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    items = await kb.search_files("archive", "group", "误差分析", limit=5)

    assert "content ILIKE" in conn.sql
    assert "%误差分析%" in conn.args
    assert items[0]["id"] == "old_report"
    assert items[0]["content"] == "这个历史文件包含误差分析、数据表和最终结论。"
    assert items[0]["filename"] == "experiment_report.docx"
    assert items[0]["upload_time"] == 1_700_000_000


async def test_search_files_keeps_same_filename_same_nickname_different_users(monkeypatch):
    from app.memory import kb

    def row(node_id: str, uploader_uin: str) -> dict:
        return {
            "id": node_id,
            "headline": "task document",
            "content": f"summary for {node_id} task document",
            "salience": 1.0,
            "created_at": None,
            "file_metadata": json.dumps({
                "filename": "task.docx",
                "workspace_path": f"group_files/{node_id}.docx",
                "archive_id": "archive",
                "group_id": "group",
                "uploader_name": "SameNick",
                "uploader_uin": uploader_uin,
                "upload_time": 2_000_000_000,
                "download_status": "done",
            }, ensure_ascii=False),
        }

    conn = FakeConn()
    conn.rows = [row("same_name_current", "u2"), row("same_name_other", "u1")]
    monkeypatch.setattr(kb, "pool", lambda: FakePool(conn))

    items = await kb.search_files("archive", "group", "task", limit=5)

    assert [item["id"] for item in items] == ["same_name_current", "same_name_other"]
    assert [item["filename"] for item in items] == ["task.docx", "task.docx"]
    assert [item["uploader_name"] for item in items] == ["SameNick", "SameNick"]
    assert [item["uploader_uin"] for item in items] == ["u2", "u1"]
