-- ============================================================
-- Group file sync dedup table
-- Tracks which NapCat group files have already been synced
-- ============================================================

CREATE TABLE IF NOT EXISTS synced_files (
    archive_id     TEXT NOT NULL,
    group_id       TEXT NOT NULL,
    file_id        TEXT NOT NULL,       -- NapCat file_id
    file_name      TEXT NOT NULL,
    file_size      BIGINT NOT NULL DEFAULT 0,
    upload_time    BIGINT NOT NULL,     -- NapCat upload_time (Unix timestamp)
    uploader_uin   BIGINT NOT NULL DEFAULT 0,
    uploader_name  TEXT NOT NULL DEFAULT '',
    busid          INTEGER NOT NULL DEFAULT 0,
    workspace_path TEXT NOT NULL,       -- relative path in workspace
    kb_node_id     TEXT,                -- cold_nodes.id
    synced_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (archive_id, group_id, file_id)
);
