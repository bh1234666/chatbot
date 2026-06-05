-- ============================================================
-- Conversation summaries on bot_group_personas
-- Stores a brief summary of the most recent conversation
-- using each persona, for display in persona selection UI.
-- ============================================================

ALTER TABLE bot_group_personas ADD COLUMN last_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE bot_group_personas ADD COLUMN last_summary_at TEXT;
