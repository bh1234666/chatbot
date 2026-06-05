-- ============================================================
-- File KB support: file_metadata column for cold_nodes
-- node_type='file' entries store file path/metadata here
-- ============================================================

ALTER TABLE cold_nodes ADD COLUMN file_metadata TEXT;
