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
