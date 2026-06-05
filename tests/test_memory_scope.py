import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "SELECT id FROM warm_memories" in sql:
            archive_id, group_id, ids = args
            return [
                {"id": row["id"]}
                for row in self.rows
                if row.get("archive_id") == archive_id
                and row.get("group_id") == group_id
                and row.get("id") in ids
            ]
        if "SELECT id FROM cold_nodes" in sql:
            archive_id, group_id, ids = args
            return [
                {"id": row["id"]}
                for row in self.rows
                if row.get("archive_id") == archive_id
                and row.get("group_id") == group_id
                and row.get("id") in ids
            ]
        return self.rows

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        if "DELETE FROM warm_memories" in sql or "DELETE FROM cold_nodes" in sql:
            archive_id, group_id, ids = args
            self.rows[:] = [
                row for row in self.rows
                if not (
                    row.get("archive_id") == archive_id
                    and row.get("group_id") == group_id
                    and row.get("id") in ids
                )
            ]
        return "DELETE 999"


async def test_delete_warm_is_scoped_to_group_and_returns_selected_count(monkeypatch):
    from app.memory import warm

    conn = _Conn([
        {"id": "w1", "archive_id": "archive", "group_id": "group-a"},
        {"id": "w2", "archive_id": "archive", "group_id": "group-a"},
        {"id": "w3", "archive_id": "archive", "group_id": "group-b"},
        {"id": "w4", "archive_id": "other", "group_id": "group-a"},
    ])
    monkeypatch.setattr(warm, "pool", lambda: _Pool(conn))

    deleted = await warm.delete_warm("archive", "group-a", ["w1", "w2", "w3"])

    assert deleted == 2
    assert "group_id = $2" in conn.fetch_calls[0][0]
    assert conn.fetch_calls[0][1] == ("archive", "group-a", ["w1", "w2", "w3"])
    assert conn.execute_calls[0][1] == ("archive", "group-a", ["w1", "w2"])
    assert {row["id"] for row in conn.rows} == {"w3", "w4"}


async def test_delete_cold_is_scoped_to_group_and_returns_selected_count(monkeypatch):
    from app.memory import cold

    conn = _Conn([
        {"id": "c1", "archive_id": "archive", "group_id": "group-a"},
        {"id": "c2", "archive_id": "archive", "group_id": "group-b"},
        {"id": "c3", "archive_id": "other", "group_id": "group-a"},
    ])
    monkeypatch.setattr(cold, "pool", lambda: _Pool(conn))

    deleted = await cold.delete_cold("archive", "group-a", ["c1", "c2"])

    assert deleted == 1
    assert "group_id = $2" in conn.fetch_calls[0][0]
    assert conn.fetch_calls[0][1] == ("archive", "group-a", ["c1", "c2"])
    assert conn.execute_calls[0][1] == ("archive", "group-a", ["c1"])
    assert {row["id"] for row in conn.rows} == {"c2", "c3"}


async def test_delete_warm_and_cold_empty_ids_do_not_open_pool(monkeypatch):
    from app.memory import cold, warm

    def fail_pool():
        raise AssertionError("pool should not be opened for empty delete ids")

    monkeypatch.setattr(warm, "pool", fail_pool)
    monkeypatch.setattr(cold, "pool", fail_pool)

    assert await warm.delete_warm("archive", "group", []) == 0
    assert await cold.delete_cold("archive", "group", []) == 0
