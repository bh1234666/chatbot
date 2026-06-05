-- ============================================================
-- Initial Schema (SQLite)
-- Covers all tables: archives, personas, hot_user_turns,
-- group_events, group_messages, warm_memories, cold_nodes,
-- cold_edges, node_user_avoid
-- ============================================================

PRAGMA foreign_keys = ON;

-- Archive (top-level isolation)
CREATE TABLE IF NOT EXISTS archives (
    archive_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at      TEXT
);

-- Persona (one per archive)
CREATE TABLE IF NOT EXISTS personas (
    archive_id      TEXT PRIMARY KEY REFERENCES archives(archive_id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Hot user turns: one row per message
CREATE TABLE IF NOT EXISTS hot_user_turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_hot_user
    ON hot_user_turns (archive_id, group_id, user_id, created_at);

-- Group events (narration in 3rd person)
CREATE TABLE IF NOT EXISTS group_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    actor_user_id   TEXT,
    actor_name      TEXT NOT NULL,
    narration       TEXT NOT NULL,
    raw_content     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_group_events
    ON group_events (archive_id, group_id, created_at);

-- Group messages log (all messages, even bot not addressed)
CREATE TABLE IF NOT EXISTS group_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    user_id         TEXT,
    user_name       TEXT NOT NULL,
    content         TEXT NOT NULL,
    addressed_bot   INTEGER NOT NULL DEFAULT 0,
    kb_processed    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_group_msgs
    ON group_messages (archive_id, group_id, created_at);
CREATE INDEX IF NOT EXISTS idx_group_msgs_unprocessed
    ON group_messages (archive_id, group_id, created_at)
    WHERE kb_processed = 0;

-- Warm memories
CREATE TABLE IF NOT EXISTS warm_memories (
    id              TEXT PRIMARY KEY,
    archive_id      TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    user_id         TEXT,
    scope           TEXT NOT NULL CHECK (scope IN ('user', 'group')),
    headline        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    internal_hint   TEXT,
    tendencies      TEXT NOT NULL DEFAULT '{}',
    entities        TEXT NOT NULL DEFAULT '[]',
    source_refs     TEXT NOT NULL DEFAULT '[]',
    refs_to_cold    TEXT NOT NULL DEFAULT '[]',
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_access     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_warm_user
    ON warm_memories (archive_id, group_id, user_id, created_at DESC)
    WHERE scope = 'user';
CREATE INDEX IF NOT EXISTS idx_warm_group
    ON warm_memories (archive_id, group_id, created_at DESC)
    WHERE scope = 'group';

-- Cold nodes (scope: user / group / kb)
CREATE TABLE IF NOT EXISTS cold_nodes (
    id              TEXT PRIMARY KEY,
    archive_id      TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    user_id         TEXT,
    scope           TEXT NOT NULL CHECK (scope IN ('user', 'group', 'kb')),
    node_type       TEXT NOT NULL,
    headline        TEXT NOT NULL,
    content         TEXT NOT NULL,
    salience        REAL NOT NULL DEFAULT 0.5 CHECK (salience BETWEEN 0 AND 1),
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_access     TEXT,
    source_refs     TEXT NOT NULL DEFAULT '[]',
    avoid_mention   INTEGER NOT NULL DEFAULT 0,
    avoid_reason    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cold_user
    ON cold_nodes (archive_id, group_id, user_id, salience DESC, last_access DESC)
    WHERE scope = 'user';
CREATE INDEX IF NOT EXISTS idx_cold_group
    ON cold_nodes (archive_id, group_id, salience DESC, last_access DESC)
    WHERE scope = 'group';
CREATE INDEX IF NOT EXISTS idx_cold_kb
    ON cold_nodes (archive_id, group_id, salience DESC, last_access DESC)
    WHERE scope = 'kb';

-- Cold edges (DAG relationships)
CREATE TABLE IF NOT EXISTS cold_edges (
    archive_id      TEXT NOT NULL,
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (archive_id, src_id, dst_id),
    FOREIGN KEY (src_id) REFERENCES cold_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (dst_id) REFERENCES cold_nodes(id) ON DELETE CASCADE,
    CHECK (src_id <> dst_id)
);
CREATE INDEX IF NOT EXISTS idx_cold_edges_dst
    ON cold_edges (archive_id, dst_id);

-- Soft-avoid mask for shared cold/KB nodes (per-user)
CREATE TABLE IF NOT EXISTS node_user_avoid (
    archive_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (archive_id, user_id, node_id),
    FOREIGN KEY (node_id) REFERENCES cold_nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_node_avoid_lookup
    ON node_user_avoid (archive_id, user_id);
