import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def transaction(self):
        return FakeTransaction()

    async def fetch(self, sql, archive_id, group_id, limit):
        available = [row for row in self.rows if not row.get("kb_processed") and not row.get("kb_processing")]
        return [dict(row) for row in available[:limit]]

    async def execute(self, sql, archive_id, group_id, ids):
        if "kb_processed = TRUE" in sql:
            for row in self.rows:
                if row["id"] in ids:
                    row["kb_processed"] = True
                    row["kb_processing"] = False
        elif "kb_processing = TRUE" in sql:
            for row in self.rows:
                if row["id"] in ids:
                    row["kb_processing"] = True
        elif "kb_processing = FALSE" in sql:
            for row in self.rows:
                if row["id"] in ids and not row.get("kb_processed"):
                    row["kb_processing"] = False
        return "UPDATE 0"


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


async def test_claim_unprocessed_marks_rows_as_processing(monkeypatch):
    from app.memory import group_messages as gm

    rows = [
        {"id": 1, "user_id": "u", "user_name": "A", "content": "one", "addressed_bot": False, "created_at": "t", "kb_processed": False, "kb_processing": False},
        {"id": 2, "user_id": "u", "user_name": "A", "content": "two", "addressed_bot": False, "created_at": "t", "kb_processed": False, "kb_processing": False},
    ]
    monkeypatch.setattr(gm, "pool", lambda: FakePool(FakeConn(rows)))

    first = await gm.claim_unprocessed("a", "g", limit=1)
    second = await gm.claim_unprocessed("a", "g", limit=10)

    assert [row["id"] for row in first] == [1]
    assert [row["id"] for row in second] == [2]
    assert all(row["kb_processing"] for row in rows)




async def test_claim_unprocessed_uses_sqlite_transaction_to_avoid_duplicate_claims(tmp_path, monkeypatch):
    from app.db.pool import SqlitePool
    from app.memory import group_messages as gm

    db_path = tmp_path / "claim.sqlite"
    sqlite_pool = SqlitePool(str(db_path), max_size=4)
    monkeypatch.setattr(gm, "pool", lambda: sqlite_pool)
    try:
        async with sqlite_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE group_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT,
                    user_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    addressed_bot INTEGER NOT NULL DEFAULT 0,
                    kb_processed INTEGER NOT NULL DEFAULT 0,
                    kb_processing INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            for idx in range(6):
                await conn.execute(
                    """
                    INSERT INTO group_messages
                        (archive_id, group_id, user_id, user_name, content, addressed_bot)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    "a", "g", "u", "A", f"msg {idx}", False,
                )

        first, second = await asyncio.gather(
            gm.claim_unprocessed("a", "g", limit=2),
            gm.claim_unprocessed("a", "g", limit=2),
        )

        first_ids = {row["id"] for row in first}
        second_ids = {row["id"] for row in second}
        assert len(first_ids) == 2
        assert len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)

        async with sqlite_pool.acquire() as conn:
            processing = await conn.fetch(
                """
                SELECT id FROM group_messages
                WHERE archive_id = $1 AND group_id = $2 AND kb_processing = TRUE
                """,
                "a", "g",
            )
        assert {row["id"] for row in processing} == first_ids | second_ids
    finally:
        await sqlite_pool.close()


async def test_release_processing_and_mark_processed(monkeypatch):
    from app.memory import group_messages as gm

    rows = [
        {"id": 1, "kb_processed": False, "kb_processing": True},
        {"id": 2, "kb_processed": False, "kb_processing": True},
    ]
    conn = FakeConn(rows)
    monkeypatch.setattr(gm, "pool", lambda: FakePool(conn))

    await gm.release_processing("a", "g", [1])
    await gm.mark_processed("a", "g", [2])

    assert rows[0]["kb_processed"] is False
    assert rows[0]["kb_processing"] is False
    assert rows[1]["kb_processed"] is True
    assert rows[1]["kb_processing"] is False
