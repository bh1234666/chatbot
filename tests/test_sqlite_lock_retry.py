import sqlite3


async def test_sqlite_execute_retries_database_locked(monkeypatch):
    from app.db import pool as db_pool

    class FakeCursor:
        rowcount = 1
        description = [("value",)]

        async def fetchone(self):
            return ("ok",)

    class FakeConn:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql, args=()):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return FakeCursor()

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    fake = FakeConn()
    monkeypatch.setattr(db_pool.asyncio, "sleep", fake_sleep)

    cursor = await db_pool._execute_with_locked_retry(fake, "SELECT 1")
    row = await cursor.fetchone()

    assert row == ("ok",)
    assert fake.calls == 2
    assert sleeps == [0.05]
