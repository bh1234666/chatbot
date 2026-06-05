ALTER TABLE group_messages ADD COLUMN kb_processing INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_group_msgs_kb_claim
    ON group_messages (archive_id, group_id, created_at)
    WHERE kb_processed = 0 AND kb_processing = 0;

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
);

CREATE INDEX IF NOT EXISTS idx_bot_delivered_artifacts_lookup
    ON bot_delivered_artifacts (archive_id, group_id, delivered_at);
