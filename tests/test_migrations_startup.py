import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_asyncio_noise_filter_suppresses_windows_proactor_disconnect(monkeypatch):
    from app import main

    loop = asyncio.new_event_loop()
    calls = []
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    loop.default_exception_handler = lambda context: calls.append(context)

    try:
        main._install_asyncio_noise_filter()
        handler = loop.get_exception_handler()
        assert handler is not None
        handler(
            loop,
            {
                "exception": ConnectionResetError(10054, "connection reset"),
                "handle": "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>",
            },
        )
        handler(loop, {"exception": RuntimeError("real failure"), "handle": "<Handle other>"})
    finally:
        loop.close()

    assert len(calls) == 1
    assert isinstance(calls[0]["exception"], RuntimeError)


def test_debug_report_prompt_uses_evidence_before_file_generation_wording():
    from app.core.debug import DEBUG_REPORT_SYSTEM

    assert "Use file-generation wording only when the events show" in DEBUG_REPORT_SYSTEM
    assert "For read-only analysis" in DEBUG_REPORT_SYSTEM
    assert "只在确有文件或产物动作时说生成文件" in DEBUG_REPORT_SYSTEM


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
    def __init__(self, *, applied=0, existing_db=False, fail_script=False, pre_applied=None, table_columns=None, tables=None):
        self.applied = applied
        self.existing_db = existing_db
        self.fail_script = fail_script
        self.executed = []
        self.scripts = []
        self.applied_filenames = set(pre_applied or [])
        self.table_columns = {table: list(cols) for table, cols in (table_columns or {}).items()}
        if isinstance(table_columns, list):
            self.table_columns = {"group_messages": list(table_columns)}
        self.tables = set(tables or self.table_columns)
        if existing_db:
            self.tables.add("archives")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "INSERT OR IGNORE INTO schema_migrations" in sql and args:
            self.applied_filenames.add(args[0])
        if sql.lstrip().upper().startswith("CREATE TABLE IF NOT EXISTS"):
            for table in ("bot_group_config", "bot_group_personas", "bot_settings", "synced_files"):
                if table in sql:
                    self.tables.add(table)
                    self.table_columns.setdefault(table, [])
        if "ALTER TABLE" in sql and "ADD COLUMN" in sql:
            parts = sql.split()
            table = parts[2]
            column = parts[5]
            self.tables.add(table)
            self.table_columns.setdefault(table, []).append(column)
        return "OK 1"

    async def fetch(self, sql, *args):
        if "PRAGMA table_info(" in sql:
            table = sql.split("PRAGMA table_info(", 1)[1].split(")", 1)[0]
            return [{"name": name} for name in self.table_columns.get(table, [])]
        return []

    async def fetchval(self, sql, *args):
        if "COUNT(*) FROM schema_migrations" in sql:
            return self.applied
        if "sqlite_master" in sql:
            if args:
                return 1 if args[0] in self.tables else None
            for table in self.tables:
                if table in sql:
                    return 1
            return 1 if self.existing_db else None
        if "schema_migrations WHERE filename" in sql:
            return 1 if args and args[0] in self.applied_filenames else None
        return None

    async def executescript(self, script):
        self.scripts.append(script)
        if self.fail_script:
            raise RuntimeError("bad migration")
        return "OK 0"


async def test_run_migrations_baselines_existing_database(monkeypatch):
    from app import main

    conn = _Conn(applied=0, existing_db=True)
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    await main._run_migrations()

    assert conn.scripts == []
    assert len(conn.applied_filenames) >= 1
    assert "001_init.sql" in conn.applied_filenames


async def test_run_migrations_repairs_missing_kb_processing_on_baselined_database(monkeypatch):
    from app import main

    conn = _Conn(
        applied=0,
        existing_db=True,
        table_columns={"group_messages": ["id", "archive_id", "group_id", "kb_processed"]},
    )
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    await main._run_migrations()

    executed_sql = "\n".join(sql for sql, _args in conn.executed)
    assert "ALTER TABLE group_messages ADD COLUMN kb_processing" in executed_sql
    assert "CREATE INDEX IF NOT EXISTS idx_group_msgs_kb_claim" in executed_sql


async def test_run_migrations_repairs_all_known_post_init_schema_on_baselined_database(monkeypatch):
    from app import main

    conn = _Conn(
        applied=0,
        existing_db=True,
        tables={"archives", "bot_group_personas", "cold_nodes", "group_events", "group_messages"},
        table_columns={
            "bot_group_personas": ["group_id", "archive_id", "persona_label", "created_at"],
            "cold_nodes": ["id", "archive_id", "group_id", "node_type"],
            "group_events": ["id", "archive_id", "group_id", "narration"],
            "group_messages": ["id", "archive_id", "group_id", "kb_processed"],
        },
    )
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    await main._run_migrations()

    executed_sql = "\n".join(sql for sql, _args in conn.executed)
    assert "CREATE TABLE IF NOT EXISTS bot_group_config" in executed_sql
    assert "CREATE TABLE IF NOT EXISTS bot_settings" in executed_sql
    assert "CREATE TABLE IF NOT EXISTS synced_files" in executed_sql
    assert "ALTER TABLE bot_group_personas ADD COLUMN last_summary" in executed_sql
    assert "ALTER TABLE bot_group_personas ADD COLUMN last_summary_at" in executed_sql
    assert "ALTER TABLE cold_nodes ADD COLUMN file_metadata" in executed_sql
    assert "ALTER TABLE group_events ADD COLUMN kind" in executed_sql
    assert "ALTER TABLE group_messages ADD COLUMN kb_processing" in executed_sql
    assert "DROP INDEX IF EXISTS idx_synced_files_dedup" in executed_sql
    assert "CREATE INDEX IF NOT EXISTS idx_group_events_kind" in executed_sql
    assert "CREATE INDEX IF NOT EXISTS idx_group_msgs_kb_claim" in executed_sql


async def test_run_migrations_does_not_readd_existing_kb_processing(monkeypatch):
    from app import main

    migration_names = [f.name for f in sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))]
    conn = _Conn(
        applied=len(migration_names),
        existing_db=True,
        pre_applied=migration_names,
        table_columns={"group_messages": ["id", "archive_id", "group_id", "kb_processed", "kb_processing"]},
    )
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    await main._run_migrations()

    executed_sql = "\n".join(sql for sql, _args in conn.executed)
    assert "ALTER TABLE group_messages ADD COLUMN kb_processing" not in executed_sql
    assert "CREATE INDEX IF NOT EXISTS idx_group_msgs_kb_claim" in executed_sql


async def test_run_migrations_records_new_migration_in_same_script(monkeypatch):
    from app import main

    conn = _Conn(applied=0, existing_db=False)
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    await main._run_migrations()

    assert conn.scripts
    assert "INSERT INTO schema_migrations (filename) VALUES ('001_init.sql')" in conn.scripts[0]
    assert conn.scripts[0].startswith("BEGIN;")
    assert conn.scripts[0].rstrip().endswith("COMMIT;")


async def test_run_migrations_raises_on_failed_new_migration(monkeypatch):
    import pytest
    from app import main

    conn = _Conn(applied=0, existing_db=False, fail_script=True)
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    with pytest.raises(RuntimeError):
        await main._run_migrations()


async def test_run_migrations_skips_already_applied_files(monkeypatch):
    from app import main

    migration_names = [f.name for f in sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))]
    conn = _Conn(applied=len(migration_names), existing_db=False, pre_applied=migration_names)
    monkeypatch.setattr(main, "pool", lambda: _Pool(conn))

    await main._run_migrations()

    assert conn.scripts == []
    assert conn.applied_filenames == set(migration_names)


async def test_run_migrations_returns_when_migrations_dir_missing(monkeypatch, tmp_path):
    from app import main

    missing_app_dir = tmp_path / "missing_app"
    fake_main_file = missing_app_dir / "main.py"
    monkeypatch.setattr(main, "__file__", str(fake_main_file))
    monkeypatch.setattr(main, "pool", lambda: (_ for _ in ()).throw(AssertionError("pool should not be opened")))

    await main._run_migrations()
