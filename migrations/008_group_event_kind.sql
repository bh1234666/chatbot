-- 2026-05-07 Bug 3 fix: add kind column to distinguish narration from progress
-- Only 'narration' events are loaded into system prompts.
-- 'progress' events are persisted for debugging but excluded from context injection.
ALTER TABLE group_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'narration';
CREATE INDEX IF NOT EXISTS idx_group_events_kind ON group_events (archive_id, group_id, kind, created_at);
