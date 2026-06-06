"""
FastAPI 应用入口。
启动时初始化 DB 连接池并执行迁移；关闭时优雅清理。

Author: bh1234666
License: MIT
"""
import logging
import os
import sys
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.core import debug
from app.db.pool import init_pool, close_pool, pool, db_kind
from app.api import archives, personas, chat, observe, memory, bot as bot_api, group_files, environment, agent


log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure stdlib logging without risking blocked stderr pipes."""
    handlers: list[logging.Handler] = []
    if settings.debug_console:
        handlers.append(logging.StreamHandler())
    else:
        log_dir = settings.debug_log_dir or "logs"
        os.makedirs(log_dir, exist_ok=True)
        app_log = Path(log_dir) / f"app_{os.getpid()}.log"
        handlers.append(logging.FileHandler(app_log, encoding="utf-8"))
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        handlers=handlers,
        force=True,
    )


def _install_asyncio_noise_filter() -> None:
    """Suppress Windows Proactor disconnect noise while preserving real loop errors."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    previous_handler = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        handle = str(context.get("handle") or "")
        if (
            isinstance(exc, ConnectionResetError)
            and "_ProactorBasePipeTransport._call_connection_lost" in handle
        ):
            log.debug("suppressed client disconnect during Proactor connection_lost: %s", exc)
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


# 2026-05-15 PG support (Item 8): schema introspection 走方言分支。
# SQLite 用 sqlite_master / PRAGMA;PostgreSQL 用 information_schema。
# (这两个 helper 只在迁移启动时跑,不在热路径,分支开销可忽略。)

async def _table_exists(conn, table: str) -> bool:
    if db_kind() == "sqlite":
        return bool(await conn.fetchval(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = $1
            """,
            table,
        ))
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table,
    ))


async def _table_columns(conn, table: str) -> set[str]:
    if db_kind() == "sqlite":
        rows = await conn.fetch(f"PRAGMA table_info({table})")
        return {row.get("name") for row in rows}
    rows = await conn.fetch(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table,
    )
    return {row.get("column_name") for row in rows}


async def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    if not await _table_exists(conn, table):
        return
    if column not in await _table_columns(conn, table):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


async def _ensure_post_baseline_schema(conn) -> None:
    """补 baseline 之后的增量 schema。

    2026-05-15 PG support (Item 8):
      - CREATE TABLE DDL 用 SQLite 风格的 `DEFAULT (datetime('now'))`,
        PG parser 不接受 → 这一段仅在 sqlite 上跑。PG 部署的 schema 应该
        由 Alembic / psql 在部署前建好(用 `DEFAULT NOW()` 替换 datetime)。
      - `_ensure_column` / CREATE INDEX 已经走 dialect-aware introspection
        (information_schema vs sqlite_master),对两个方言都安全 → 都跑。
      - `UPDATE group_events ...` 是普通 SQL,两边都可执行。
    """
    if db_kind() == "sqlite":
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_group_config (
                group_id            TEXT PRIMARY KEY,
                active_archive_id   TEXT,
                participate         INTEGER NOT NULL DEFAULT 0,
                group_name          TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_group_personas (
                group_id            TEXT NOT NULL,
                archive_id          TEXT NOT NULL,
                persona_label       TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (group_id, archive_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synced_files (
                archive_id     TEXT NOT NULL,
                group_id       TEXT NOT NULL,
                file_id        TEXT NOT NULL,
                file_name      TEXT NOT NULL,
                file_size      BIGINT NOT NULL DEFAULT 0,
                upload_time    BIGINT NOT NULL,
                uploader_uin   BIGINT NOT NULL DEFAULT 0,
                uploader_name  TEXT NOT NULL DEFAULT '',
                busid          INTEGER NOT NULL DEFAULT 0,
                workspace_path TEXT NOT NULL,
                kb_node_id     TEXT,
                synced_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (archive_id, group_id, file_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_delivered_artifacts (
                archive_id     TEXT NOT NULL,
                group_id       TEXT NOT NULL,
                artifact_id    TEXT NOT NULL,
                file_name      TEXT NOT NULL,
                file_size      BIGINT NOT NULL DEFAULT 0,
                delivered_at   BIGINT NOT NULL,
                workspace_path TEXT NOT NULL,
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (archive_id, group_id, artifact_id)
            )
            """
        )

    # _ensure_column 走 dialect-aware introspection,两个方言都安全。
    await _ensure_column(conn, "bot_group_personas", "last_summary", "last_summary TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "bot_group_personas", "last_summary_at", "last_summary_at TEXT")
    await _ensure_column(conn, "cold_nodes", "file_metadata", "file_metadata TEXT")
    await _ensure_column(conn, "group_events", "kind", "kind TEXT NOT NULL DEFAULT 'narration'")
    await _ensure_column(conn, "group_messages", "kb_processing", "kb_processing INTEGER NOT NULL DEFAULT 0")

    if await _table_exists(conn, "synced_files"):
        # 2026-05-15 重设计:这个索引编码了"同 name+size 必然是同一文件"的错误前提。
        # QQ 允许群内文件**合法重名**(不同用户传同名文件,或同一用户先后传两份同名
        # 但内容不同的版本)。索引强制唯一会让第二次 INSERT 抛 IntegrityError,
        # 应用层只能要么吞错丢失新上传,要么 ON CONFLICT DO NOTHING 静默丢弃 — 都不
        # 符合用户期望(应该让新上传赢得文件名,旧的加时间戳后缀保留访问)。
        # 删掉索引,在应用层做"撞名 → 把老的 file_name 加时间戳"的处理。
        # DROP INDEX IF EXISTS 在 SQLite 和 PG 都幂等安全。
        await conn.execute("DROP INDEX IF EXISTS idx_synced_files_dedup")
    if await _table_exists(conn, "bot_delivered_artifacts"):
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_bot_delivered_artifacts_lookup
                ON bot_delivered_artifacts (archive_id, group_id, delivered_at)
            """
        )
    if await _table_exists(conn, "group_events"):
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_events_kind
                ON group_events (archive_id, group_id, kind, created_at)
            """
        )
        await conn.execute(
            """
            UPDATE group_events
            SET kind = 'progress'
            WHERE narration LIKE '（进度报告）%'
              AND kind = 'narration'
            """
        )
    if await _table_exists(conn, "group_messages"):
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_msgs_kb_claim
                ON group_messages (archive_id, group_id, created_at)
                WHERE kb_processed = 0 AND kb_processing = 0
            """
        )


async def _run_migrations() -> None:
    """启动时执行未记录的迁移；新失败必须阻止服务继续启动。

    2026-05-15 PG support (Item 8):
      - SQLite 分支保持原行为(执行 migrations/*.sql via executescript)。
      - PostgreSQL 分支不自动跑迁移 —— 迁移文件是 SQLite 方言写的
        (datetime('now') / INSERT OR IGNORE 等),直接喂给 PG 会炸。
        PG 部署要走另一套迁移工具(Alembic / psql -f),启动时只记一条 INFO。
        本函数下半截的 schema 增量(_ensure_post_baseline_schema)对两个方言
        都能跑(已经做了方言改造),保留调用。
    """
    if db_kind() == "sqlite":
        await _run_migrations_sqlite()
    else:
        log.info(
            "PostgreSQL 部署:跳过 SQLite 风格迁移文件。"
            "首次部署请用 psql 或 Alembic 提前导入 schema;"
            "本进程只补 _ensure_post_baseline_schema 里的增量 DDL。"
        )
        async with pool().acquire() as conn:
            await _ensure_post_baseline_schema(conn)


async def _run_migrations_sqlite() -> None:
    """原 _run_migrations 的 SQLite 实现(逻辑没动,只是改名挪到 sqlite 分支下)。"""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    if not migrations_dir.exists():
        return
    files = sorted(migrations_dir.glob("*.sql"))
    async with pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        applied_count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
        archives_exists = await conn.fetchval(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'archives'
            """
        )
        if applied_count == 0 and archives_exists:
            for f in files:
                await conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (filename) VALUES ($1)",
                    f.name,
                )
            log.info("baseline existing database with %d migrations", len(files))
            await _ensure_post_baseline_schema(conn)
            return

        for f in files:
            already_applied = await conn.fetchval(
                "SELECT 1 FROM schema_migrations WHERE filename = $1",
                f.name,
            )
            if already_applied:
                continue

            log.info("running migration: %s", f.name)
            sql = f.read_text(encoding="utf-8")
            filename_sql = f.name.replace("'", "''")
            script = (
                "BEGIN;\n"
                f"{sql}\n"
                f"INSERT INTO schema_migrations (filename) VALUES ('{filename_sql}');\n"
                "COMMIT;"
            )
            try:
                await conn.executescript(script)
            except Exception:
                try:
                    await conn.execute("ROLLBACK")
                except Exception:
                    pass
                log.exception("migration %s failed", f.name)
                raise

        await _ensure_post_baseline_schema(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    for noisy in ("httpx", "httpcore", "openai", "aiosqlite", "asyncpg", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _install_asyncio_noise_filter()
    from app.core import pause_state as _ps

    await init_pool()
    debug.init_log()  # 服务启动时立即创建 debug 日志文件
    # 2026-05-07 Bug 7: sweep stale pause states on startup
    try:
        _removed = await _ps.sweep_stale_pause_states()
        if _removed:
            log.info("pause_state sweep: removed %d stale files on startup", _removed)
    except Exception:
        log.exception("pause_state sweep failed (non-fatal)")
    if settings.debug_mode:
        debug.status(f"DEBUG MODE ON (verbose={settings.debug_verbose}, log_dir={settings.debug_log_dir})")
    else:
        import sys
        print("[WARN] DEBUG_MODE=false — no per-request debug output will appear on console", file=sys.stderr, flush=True)
    await _run_migrations()
    try:
        from app.memory import archive as _archive_dao
        _reloaded = await _archive_dao.reload_personas_from_files()
        log.info("persona file reload on startup: updated %d archive personas", _reloaded)
        debug.status(f"persona file reload complete: updated {_reloaded} archive personas")
    except Exception:
        log.exception("persona file reload on startup failed (non-fatal)")

    # 2026-05-18 P167: 启动 MinerU OCR 后台服务（避免每次 OCR 冷启动 ~30s）
    _mineru_bg_proc = None
    if not settings.startup_ocr_warm_enabled:
        log.info("startup OCR warm disabled by STARTUP_OCR_WARM_ENABLED=false")
    else:
        try:
            from app.llm.tools.ocr_bridge import (
                _default_mineru_config,
                _mineru_available,
                _mineru_backend_default,
                _mineru_bg_api_url,
                _terminate_stale_mineru_processes,
                _terminate_umi_processes,
                _start_mineru_api_process,
                _wait_gpu_competitors_idle,
                clear_mineru_service_state,
                mineru_service_start_lock,
                warm_umi_worker,
            )
            _mineru_config = _default_mineru_config(None, backend=_mineru_backend_default(), formula=True, table=True, image_analysis=False)
            if _mineru_available():
                with mineru_service_start_lock(timeout=120):
                    if not _mineru_bg_api_url(_mineru_config):
                        if not _wait_gpu_competitors_idle(timeout=120):
                            log.warning("MinerU startup deferred: UmiOCR or TTS is active")
                            raise RuntimeError("GPU resource busy; not killing active UmiOCR/TTS")
                        _terminate_umi_processes()
                        _terminate_stale_mineru_processes()
                        clear_mineru_service_state()
                        _mineru_bg_proc = _start_mineru_api_process(_mineru_config, port=51111)
                        log.info("MinerU API service starting (pid=%d)", _mineru_bg_proc.pid)
            else:
                try:
                    if warm_umi_worker(timeout=60):
                        log.info("UmiOCR worker warmed")
                except Exception:
                    log.exception("UmiOCR worker warm failed (non-fatal)")
        except Exception:
            log.exception("MinerU background service launch failed (non-fatal)")

    # 2026-05-16 Dream: 启动后台空闲整理子系统
    if settings.dream_enabled:
        try:
            from app.core.dream import supervisor as _dream_sup
            from app.core.dream import cache as _dream_cache

            # 注入 workspace_root_getter (避免循环 import)
            # 2026-05-16 修订: 之前直接读 settings.workspace_root 但默认是 "",
            # 应该走 workspace.py 的 _get_workspace_root() (它有 fallback 到
            # project_root/data/workspaces). 实测 dream 任务因此把所有图片/文件
            # 都 skip 掉了。
            def _ws_root():
                from app.llm.tools.workspace import _get_workspace_root as _gwr
                return str(_gwr())
            _dream_cache.set_workspace_root_getter(_ws_root)

            # 启动 supervisor (后台 task, fire-and-forget)
            _dream_sup.start_supervisor()
            log.info("dream supervisor started")
        except Exception:
            log.exception("dream supervisor failed to start; service continues without dream")

    yield

    # P167: 关闭 MinerU 后台服务
    if _mineru_bg_proc is not None:
        try:
            import subprocess, sys as _sys
            from app.llm.tools.ocr_bridge import clear_mineru_service_state

            if _mineru_bg_proc.poll() is None and _sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(_mineru_bg_proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                )
            else:
                _mineru_bg_proc.terminate()
                try:
                    _mineru_bg_proc.wait(timeout=10)
                except Exception:
                    _mineru_bg_proc.kill()
            clear_mineru_service_state()
            log.info("MinerU API service stopped")
        except Exception:
            log.exception("MinerU background service stop failed (non-fatal)")

    # 优雅关闭 dream (在 close_pool 之前, 因为 dream 任务可能用 pool)
    if settings.dream_enabled:
        try:
            from app.core.dream import supervisor as _dream_sup
            await _dream_sup.shutdown_dream()
        except Exception:
            log.exception("dream shutdown failed (non-fatal)")

    await close_pool()


app = FastAPI(
    title="Chatbot",
    description="Author: bh1234666 — https://github.com/bh1234666/chatbot",
    version="0.1.0",
    contact={"name": "bh1234666", "url": "https://github.com/bh1234666"},
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    lifespan=lifespan,
)
app.include_router(archives.router)
app.include_router(personas.router)
app.include_router(chat.router)
app.include_router(observe.router)
app.include_router(memory.router)
app.include_router(bot_api.router)
app.include_router(group_files.router)
app.include_router(environment.router)
app.include_router(agent.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "author": "bh1234666"}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Prometheus exposition format. 累积自进程启动,/metrics 端点单 worker 暴露;
    多 worker 部署时 scrape 端按 worker 标签聚合即可。
    """
    from app.core import metrics as _metrics
    return _metrics.render_prometheus()
