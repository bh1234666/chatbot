-- ============================================================
-- Bot group configuration
-- Controls which groups the bot participates in and which
-- persona (archive) is active per group.
-- ============================================================

-- Per-group bot config
CREATE TABLE IF NOT EXISTS bot_group_config (
    group_id            TEXT PRIMARY KEY,
    active_archive_id   TEXT,
    participate         INTEGER NOT NULL DEFAULT 0,
    group_name          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Personas available per group (multiple, but only one active via bot_group_config.active_archive_id)
CREATE TABLE IF NOT EXISTS bot_group_personas (
    group_id            TEXT NOT NULL,
    archive_id          TEXT NOT NULL,
    persona_label       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, archive_id)
);
