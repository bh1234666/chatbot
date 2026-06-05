"""
Database pool — SQLite (aiosqlite) with asyncpg-compatible interface.

All SQL queries in the codebase use PostgreSQL syntax ($1, JSONB, NOW(),
ANY(array), ILIKE 等)。本模块在运行时把它们翻译成 SQLite，调用方代码不用改。

切换回 PostgreSQL：装 asyncpg，把 DATABASE_URL 改成 postgresql+asyncpg://...
然后删掉本文件、恢复原版 pool.py。

------------------------------------------------------------
2026-05-01 重构记录（解决 per-user 并行下两个阻塞 bug）
------------------------------------------------------------

【Bug A】SQL 翻译器对"中位 ANY 参数"乱序绑定
  旧版 _translate_sql 分两步：先 sub ANY、就地展开 args_list，再 finditer
  收集 $N。展开 args_list 之后下游的 $M（M>N）位置全部错位，参数和占位符
  绑定关系断裂。
  典型受害：cold.expand_cold(ids=≥2 个) 的 UPDATE，salience 拿到的是 id
  字符串，access_count 漏更新；apply_avoid_mention 同理。
  修法：单遍线性扫描 SQL，遇 ANY 当场展开+绑值，遇 $N 当场绑值，杜绝
  位置漂移。

【Bug B】单连接"池"导致事务隔离失效
  旧版 SqlitePool 全局共享一个 aiosqlite.Connection，每次 acquire() 都返
  回同一个对象。conn.transaction() 只是 yield 一下没有 BEGIN，多个协程
  同时进事务会共用同一隐式 transaction，rollback 会撤掉别人的写。
  改成 per-user 并发后这条会立刻爆炸。
  修法：真正的连接池（默认 max=8 条独立 aiosqlite 连接），autocommit 模
  式 + 显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK，每条连接独立事务。

【顺便修】
  - 增加 ILIKE → LIKE 翻译（SQLite 无 ILIKE，LIKE 默认对 ASCII
    case-insensitive、对中文本来就不区分），原代码 cold.topk_cold_*、
    kb.search_files 用的全部 ILIKE 不再炸。
  - cast 移除规则放在最前面统一处理，规避 ANY($N::text[]) 这种"cast 写
    在参数后面"的情况。

------------------------------------------------------------
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiosqlite

from app.config import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL translator: PostgreSQL → SQLite
# ---------------------------------------------------------------------------
# 2026-05-20 重构: 翻译器实现已抽离到 app/db/sql_translate.py(纯 stdlib,可独立
# 单元测试),并对"扫描 SQL"这一与参数无关的步骤加了 lru_cache。translate_sql 的
# 输出与重构前 _translate_sql 逐字节等价(见 tests/test_sql_translate.py 差分测试)。
# 这里保留 _translate_sql 别名,pool 内部调用点零改动。
from app.db.sql_translate import translate_sql as _translate_sql  # noqa: E402


# ---------------------------------------------------------------------------
# Connection wrapper — 模拟 asyncpg.Connection 接口
# ---------------------------------------------------------------------------

def _sqlite_may_write(sql: str) -> bool:
    head = sql.lstrip().upper()
    if head.startswith("SELECT"):
        return False
    if head.startswith("WITH"):
        return bool(re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", head))
    if head.startswith("PRAGMA"):
        return "=" in head or "WAL_CHECKPOINT" in head
    return head.startswith((
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "VACUUM",
    ))


_SQLITE_LOCK_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0)


def _is_sqlite_locked(exc: BaseException) -> bool:
    if not isinstance(exc, (sqlite3.OperationalError, aiosqlite.OperationalError)):
        return False
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


async def _execute_with_locked_retry(conn: aiosqlite.Connection, sql: str, args=()):
    for attempt, delay in enumerate((0.0, *_SQLITE_LOCK_RETRY_DELAYS)):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await conn.execute(sql, args)
        except Exception as exc:
            if not _is_sqlite_locked(exc) or attempt >= len(_SQLITE_LOCK_RETRY_DELAYS):
                raise
            log.warning(
                "SQLite locked during execute; retrying in %.2fs (attempt %d/%d)",
                _SQLITE_LOCK_RETRY_DELAYS[attempt],
                attempt + 1,
                len(_SQLITE_LOCK_RETRY_DELAYS),
            )


async def _executescript_with_locked_retry(conn: aiosqlite.Connection, sql: str):
    for attempt, delay in enumerate((0.0, *_SQLITE_LOCK_RETRY_DELAYS)):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await conn.executescript(sql)
        except Exception as exc:
            if not _is_sqlite_locked(exc) or attempt >= len(_SQLITE_LOCK_RETRY_DELAYS):
                raise
            log.warning(
                "SQLite locked during executescript; retrying in %.2fs (attempt %d/%d)",
                _SQLITE_LOCK_RETRY_DELAYS[attempt],
                attempt + 1,
                len(_SQLITE_LOCK_RETRY_DELAYS),
            )


class SqliteConnection:
    """
    一条独立的 aiosqlite 连接的薄包装。

    事务模型：
      - 底层 conn 跑在 autocommit 模式（isolation_level=None），每条
        execute 自动提交，不会有"隐式 begin 一直挂着不还锁"的情况。
      - transaction() 上下文管理器内显式 BEGIN IMMEDIATE → COMMIT/ROLLBACK，
        多条连接互不干扰，事务隔离真实有效（WAL 下读不阻塞写、写不阻塞读）。
      - 不支持嵌套事务（嵌套时主动报错），代码里也没有这种用法。
    """

    def __init__(self, conn: aiosqlite.Connection, write_lock: asyncio.Lock):
        self._conn = conn
        self._write_lock = write_lock
        self._in_tx = False

    def transaction(self):
        """显式事务：BEGIN IMMEDIATE / COMMIT / ROLLBACK"""
        @asynccontextmanager
        async def _tx():
            if self._in_tx:
                raise RuntimeError(
                    "nested transaction not supported on SqliteConnection; "
                    "callers should not nest conn.transaction() blocks"
                )
            async with self._write_lock:
                await _execute_with_locked_retry(self._conn, "BEGIN IMMEDIATE")
                self._in_tx = True
                try:
                    yield
                except BaseException:
                    try:
                        await _execute_with_locked_retry(self._conn, "ROLLBACK")
                    except Exception:
                        log.exception("ROLLBACK failed; connection may be in bad state")
                    raise
                else:
                    await _execute_with_locked_retry(self._conn, "COMMIT")
                finally:
                    self._in_tx = False
        return _tx()

    async def fetchrow(self, sql: str, *args) -> Optional[dict]:
        sql, args = _translate_sql(sql, args)
        if not self._in_tx and _sqlite_may_write(sql):
            async with self._write_lock:
                cursor = await _execute_with_locked_retry(self._conn, sql, args)
                row = await cursor.fetchone()
        else:
            cursor = await _execute_with_locked_retry(self._conn, sql, args)
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_dict(cursor, row)

    async def fetch(self, sql: str, *args) -> list[dict]:
        sql, args = _translate_sql(sql, args)
        if not self._in_tx and _sqlite_may_write(sql):
            async with self._write_lock:
                cursor = await _execute_with_locked_retry(self._conn, sql, args)
                rows = await cursor.fetchall()
        else:
            cursor = await _execute_with_locked_retry(self._conn, sql, args)
            rows = await cursor.fetchall()
        return [_row_to_dict(cursor, r) for r in rows]

    async def fetchval(self, sql: str, *args) -> Any:
        sql, args = _translate_sql(sql, args)
        if not self._in_tx and _sqlite_may_write(sql):
            async with self._write_lock:
                cursor = await _execute_with_locked_retry(self._conn, sql, args)
                row = await cursor.fetchone()
        else:
            cursor = await _execute_with_locked_retry(self._conn, sql, args)
            row = await cursor.fetchone()
        if row is None:
            return None
        return row[0]

    async def executescript(self, sql: str) -> str:
        """执行多语句脚本（迁移用）。executescript 自带隐式提交。"""
        async with self._write_lock:
            await _executescript_with_locked_retry(self._conn, sql)
        return "OK 0"

    async def execute(self, sql: str, *args) -> str:
        """
        返回 'INSERT 0 N' / 'DELETE N' / 'UPDATE N' / 'OK N' 形式状态串，
        调用方有时会做 result.endswith(' 1') 之类判断。
        """
        sql, args = _translate_sql(sql, args)
        head = sql.lstrip().upper()
        if not self._in_tx:
            async with self._write_lock:
                cursor = await _execute_with_locked_retry(self._conn, sql, args)
        else:
            cursor = await _execute_with_locked_retry(self._conn, sql, args)
        rc = cursor.rowcount
        if head.startswith("INSERT"):
            return f"INSERT 0 {rc}"
        if head.startswith("DELETE"):
            return f"DELETE {rc}"
        if head.startswith("UPDATE"):
            return f"UPDATE {rc}"
        return f"OK {rc}"

    async def executemany(self, sql: str, args_list: list[tuple]) -> None:
        """同 SQL 不同参数批量执行。"""
        if self._in_tx:
            for args in args_list:
                t_sql, t_args = _translate_sql(sql, args)
                await _execute_with_locked_retry(self._conn, t_sql, t_args)
            return
        async with self._write_lock:
            for args in args_list:
                t_sql, t_args = _translate_sql(sql, args)
                await _execute_with_locked_retry(self._conn, t_sql, t_args)


def _row_to_dict(cursor, row) -> dict:
    """把 aiosqlite row 转成 dict（行为对齐 asyncpg.Record）"""
    if row is None:
        return {}
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Pool — 真正的多连接池
# ---------------------------------------------------------------------------

class SqlitePool:
    """
    多连接池。

    设计：
      - 默认 max_size=40（per-user 并行场景下够用：当前架构每个 SSE 流对应
        一个用户串行任务,N 个用户对应 N 条连接,再加每个任务自己的 9 路 gather
        load + 后台维护并发,40 条连接通常够;不够时调用方等待）。
      - 懒创建：第一次需要时才打开连接。
      - WAL 模式 + busy_timeout=5s（写锁竞争时不立即抛 DatabaseLocked）。
      - autocommit 模式（isolation_level=None,在 aiosqlite.connect 构造时传入,
        而非事后设属性——后者会从主线程触碰 worker 线程持有的 sqlite3.Connection,
        触发 "SQLite objects created in a thread can only be used in that same thread"
        ProgrammingError),事务由 SqliteConnection 显式 BEGIN/COMMIT 控制。
    """

    DEFAULT_MAX_SIZE = 40

    def __init__(self, db_path: str, max_size: int = DEFAULT_MAX_SIZE):
        self._db_path = db_path
        self._max_size = max(1, max_size)
        self._all_conns: list[aiosqlite.Connection] = []
        self._available: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(
            maxsize=self._max_size
        )
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def _new_conn(self) -> aiosqlite.Connection:
        # isolation_level 必须通过构造参数传入 —— aiosqlite 把每条连接绑死在
        # 自己的 worker 线程,后续从主线程对 conn 的属性赋值(如
        # `conn.isolation_level = None` / `conn.row_factory = ...`)会直接穿透
        # 到底层 sqlite3.Connection,触发 "SQLite objects created in a thread
        # can only be used in that same thread" ProgrammingError。
        # 通过 connect() 传 isolation_level 让 aiosqlite 在 worker 线程内部创建
        # 时就设好,主线程后续永远不碰这个属性。
        # row_factory 我们不需要 —— SqliteConnection.fetchrow / fetch 已经用
        # cursor.description 自己构造 dict 了,row 是 Row 还是 tuple 都不影响。
        conn = await aiosqlite.connect(
            self._db_path,
            isolation_level=None,   # autocommit；事务由 SqliteConnection.transaction() 显式管
        )
        # ── per-connection PRAGMA(per-user 并发 + cold/kb 大表查询性能调优)──
        # WAL: 读写不互相阻塞(per-user 并发改造的前提)
        # synchronous=NORMAL: WAL 模式下 journal=NORMAL 安全且比 FULL 快 2-3x
        # busy_timeout: 写竞争时不立即报错,等 30s; wrapper 仍会对短暂 lock 退避重试
        # cache_size=-65536: 64MB 页缓存,显著加速 cold/kb 的 julianday()/exp() 实时计算查询
        #   (负数 = KB,正数 = pages;每个 conn 独享,8 conn × 64MB = 512MB)
        # temp_store=MEMORY: 临时表/排序中间结果放内存,cold_edges 双向 JOIN 受益明显
        # mmap_size=268435456: 256MB mmap,大表读取走 zero-copy 比传统 read 快 3-5x
        # wal_autocheckpoint=1000: 默认值,显式声明便于审计
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=30000")
        await conn.execute("PRAGMA cache_size=-65536")
        await conn.execute("PRAGMA temp_store=MEMORY")
        await conn.execute("PRAGMA mmap_size=268435456")
        await conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn

    async def _acquire_conn(self) -> aiosqlite.Connection:
        if self._closed:
            raise RuntimeError("pool is closed")

        # 尝试拿一条空闲的
        try:
            return self._available.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # 没空闲、未达上限：开新连接
        async with self._init_lock:
            if len(self._all_conns) < self._max_size:
                conn = await self._new_conn()
                self._all_conns.append(conn)
                return conn

        # 满了，等
        return await self._available.get()

    async def _release_conn(self, conn: aiosqlite.Connection) -> None:
        if self._closed:
            try:
                await conn.close()
            except Exception:
                pass
            return
        # put_nowait 必然成功：每条 conn 都是从池里拿的，回放不会超容量
        try:
            self._available.put_nowait(conn)
        except asyncio.QueueFull:
            # 理论上不可能；防御性关连接
            try:
                await conn.close()
            except Exception:
                pass

    def acquire(self) -> "_AcquireContext":
        return _AcquireContext(self)

    async def close(self) -> None:
        self._closed = True
        for conn in self._all_conns:
            try:
                await conn.close()
            except Exception:
                pass
        self._all_conns.clear()
        # drain queue
        while not self._available.empty():
            try:
                self._available.get_nowait()
            except asyncio.QueueEmpty:
                break

    @property
    def stats(self) -> dict:
        return {
            "total": len(self._all_conns),
            "idle": self._available.qsize(),
            "max_size": self._max_size,
        }


class _AcquireContext:
    """async with pool.acquire() as conn: ..."""

    def __init__(self, pool: SqlitePool):
        self._pool = pool
        self._raw: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> SqliteConnection:
        self._raw = await self._pool._acquire_conn()
        return SqliteConnection(self._raw, self._pool._write_lock)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._raw is not None:
            # 即使内部出了异常，也把连接归还给池——SqliteConnection.transaction
            # 已经做了 rollback；普通 execute 在 autocommit 模式下不会留着事务
            # 不还（无 dangling tx 风险）。
            await self._pool._release_conn(self._raw)
            self._raw = None


# ---------------------------------------------------------------------------
# 模块级 API（保持和原 pool.py 一致的签名）
# ---------------------------------------------------------------------------
# 2026-05-15 PG support (Item 8): 在不打破任何 SQLite 调用点的前提下,
# 加入 asyncpg 分支。dispatch 由 _db_kind() 看 DATABASE_URL 前缀决定:
#   sqlite:/// 或 sqlite+aiosqlite:///  → 走 SqlitePool(原有代码,行为零变化)
#   postgres:// / postgresql:// / postgresql+asyncpg://  → 走 PgPool(本节新加)
# PgConnection 对齐 SqliteConnection 的接口(transaction / fetch / fetchrow /
# fetchval / execute / executemany),所有 `async with pool().acquire() as conn:`
# 调用点不用改。
# asyncpg 是延迟 import — sqlite-only 部署不需要装 asyncpg。
# 调用方需要做方言判断时调 `db_kind()`(public 函数)。
# ---------------------------------------------------------------------------

_pool: Optional[Any] = None  # SqlitePool | PgPool


def _db_kind() -> str:
    """返回 'sqlite' 或 'postgres'。新前缀加进来时统一只动这里。"""
    url = settings.database_url
    if url.startswith(("sqlite:", "sqlite+")):
        return "sqlite"
    if url.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
        return "postgres"
    # 没识别到的前缀:为了保持向后兼容,旧代码"含 'sqlite'"也归到 sqlite
    if "sqlite" in url:
        return "sqlite"
    raise RuntimeError(
        f"DATABASE_URL={url!r} 不识别。支持前缀:\n"
        f"  sqlite:///path/to/file.db\n"
        f"  postgresql://user:pass@host:port/db\n"
        f"  postgresql+asyncpg://user:pass@host:port/db"
    )


def db_kind() -> str:
    """public alias — 让 cold.py / bot_config.py 等模块判断方言。"""
    return _db_kind()


def _db_path() -> str:
    """SQLite 文件路径(仅 sqlite 分支用)。"""
    url = settings.database_url
    if _db_kind() != "sqlite":
        raise RuntimeError(f"_db_path called for non-sqlite URL: {url!r}")
    path = re.sub(r'^sqlite(?:\+aiosqlite)?:///', '', url)
    return path or "chatbot.db"


def _pg_dsn() -> str:
    """asyncpg 用 DSN(剥离 +asyncpg 后缀,asyncpg 不识别)。"""
    url = settings.database_url
    return re.sub(r'^postgresql\+asyncpg://', 'postgresql://', url)


async def init_pool():
    """初始化连接池。根据 DATABASE_URL 自动选择 sqlite / postgres 分支。"""
    global _pool
    if _pool is not None:
        return _pool
    kind = _db_kind()
    if kind == "sqlite":
        _pool = SqlitePool(_db_path())
        log.info(
            "SQLite pool initialized: %s (max_size=%d)",
            _db_path(), _pool._max_size,
        )
    else:
        _pool = await _build_pg_pool()
        log.info("PostgreSQL pool initialized: %s", _pg_dsn().split("@")[-1])
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool():
    if _pool is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first")
    return _pool


# ---------------------------------------------------------------------------
# PostgreSQL branch (asyncpg) — 2026-05-15
# ---------------------------------------------------------------------------
# 用 lazy import 避免 sqlite-only 部署强制依赖 asyncpg。
# PgConnection 接口完全对齐 SqliteConnection:
#   - SQL 不走 _translate_sql(源 SQL 已经是 PG 语法,asyncpg 直接吃)
#   - execute 返回 'INSERT 0 N' / 'UPDATE N' / 'DELETE N' 状态串
#     (asyncpg 原生就是这个格式,与 SqliteConnection.execute 兼容)
#   - fetch/fetchrow 把 asyncpg.Record 转 dict,让调用方一致用 r["key"]
#     和 r.get("key") —— Record 不支持 .get(),不转换会炸
#   - executescript 抛 NotImplementedError(asyncpg 不支持 SQLite 风格 multi-script;
#     PG 部署的迁移要在 _run_migrations 里走另一条路径,见 main.py)

async def _build_pg_pool() -> "PgPool":
    """惰性 import asyncpg 并创建连接池。"""
    try:
        import asyncpg  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "DATABASE_URL 配置为 PostgreSQL,但环境里没装 asyncpg。\n"
            "请执行: pip install asyncpg\n"
            "(sqlite-only 部署不需要装这个包。)"
        ) from e

    raw = await asyncpg.create_pool(
        _pg_dsn(),
        min_size=2,
        max_size=20,
        # asyncpg 默认把每个 query 编译成 prepared statement, 与 PGBouncer 的
        # transaction pooling 模式不兼容(每次连接都是新的, prepared statement
        # 复用不上, 反而拖慢)。关掉缓存让 asyncpg 走 simple query protocol。
        statement_cache_size=0,
    )
    return PgPool(raw)


class PgPool:
    """asyncpg 池的薄包装。接口与 SqlitePool 对齐(acquire / close / stats)。"""

    def __init__(self, raw):
        self._raw = raw

    def acquire(self) -> "_PgAcquireContext":
        return _PgAcquireContext(self)

    async def close(self) -> None:
        await self._raw.close()

    @property
    def stats(self) -> dict:
        return {
            "kind": "postgres",
            "total": self._raw.get_size(),
            "idle": self._raw.get_idle_size(),
            "max_size": self._raw.get_max_size(),
        }


class _PgAcquireContext:
    """async with pool.acquire() as conn: ..."""

    def __init__(self, pg_pool: PgPool):
        self._pool = pg_pool
        self._raw = None

    async def __aenter__(self) -> "PgConnection":
        self._raw = await self._pool._raw.acquire()
        return PgConnection(self._raw)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._raw is not None:
            await self._pool._raw.release(self._raw)
            self._raw = None


class PgConnection:
    """asyncpg connection 的薄包装。接口和 SqliteConnection 对齐。

    重点差异:
      - SQL 不走 _translate_sql(源 SQL 已经是 PG 语法)
      - fetch/fetchrow 把 asyncpg.Record 转 dict —— Record 不支持 .get(),
        而调用方多处用 `r.get("col")`,不转换会炸
      - executescript 抛 NotImplementedError(参见模块顶部说明)
    """

    def __init__(self, raw):
        self._raw = raw

    def transaction(self):
        """asyncpg 的 conn.transaction() 是 async context manager,可直接返回。"""
        return self._raw.transaction()

    async def fetchrow(self, sql: str, *args) -> Optional[dict]:
        row = await self._raw.fetchrow(sql, *args)
        return dict(row) if row else None

    async def fetch(self, sql: str, *args) -> list[dict]:
        rows = await self._raw.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def fetchval(self, sql: str, *args) -> Any:
        return await self._raw.fetchval(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        """asyncpg execute 原生返回 'INSERT 0 N' / 'UPDATE N' 形式状态串。"""
        return await self._raw.execute(sql, *args)

    async def executemany(self, sql: str, args_list: list[tuple]) -> None:
        await self._raw.executemany(sql, args_list)

    async def executescript(self, sql: str) -> str:
        """SQLite 风格的多语句脚本 — asyncpg 不直接支持(simple-query 多语句受限,
        混 prepared/multi 会更乱)。PG 部署上,迁移走 main._run_migrations 里专门
        的 PG 分支(每条 SQL 分开 execute,或在外部用 psql -f 提前导入)。
        """
        raise NotImplementedError(
            "executescript 不支持 asyncpg。PG 部署请手动按文件执行迁移,"
            "或调用 conn.execute(sql) 一条一条跑(确保没有依赖前面定义的 PLpgSQL 等)。"
        )
