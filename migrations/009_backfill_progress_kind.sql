-- 2026-05-07 Bug 3 backfill: fix kind for old progress reports
-- Old progress reports were stored with kind='narration' (the migration default).
-- load_group_hot now filters on narration prefix as a runtime fix,
-- but the data should be semantically correct at rest too.
UPDATE group_events
SET kind = 'progress'
WHERE narration LIKE '（进度报告）%'
  AND kind = 'narration';
