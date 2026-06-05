-- ============================================================
-- Bot settings key-value store (admin group, etc.)
-- ============================================================

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
