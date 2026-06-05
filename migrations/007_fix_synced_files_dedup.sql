-- Fix synced_files deduplication: NapCat returns unstable file_ids,
-- so we deduplicate by (archive_id, group_id, file_name, file_size) instead.

-- 1. Remove duplicate rows, keeping only the row with the earliest synced_at per file
DELETE FROM synced_files
WHERE rowid NOT IN (
    SELECT MIN(rowid)
    FROM synced_files
    GROUP BY archive_id, group_id, file_name, file_size
);

-- 2. Add unique index for the new dedup key
CREATE UNIQUE INDEX IF NOT EXISTS idx_synced_files_dedup
    ON synced_files(archive_id, group_id, file_name, file_size);
