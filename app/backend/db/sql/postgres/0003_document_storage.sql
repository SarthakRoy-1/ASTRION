-- 0003 document storage: an original file lives in object storage, not on disk.
--
-- A document's row now says where its original file is (storage_key), what it
-- was called when uploaded, and how big it was; source_sha256 (already there) is
-- its checksum, and is part of the storage key. The binary itself is never in
-- the database.
--
-- storage_orphans records objects the application could not remove when it
-- meant to (a delete that raced a network failure, an upload whose database
-- write failed and whose cleanup also failed) so that reconciliation can retry
-- them instead of the cost being silent.

ALTER TABLE documents ADD COLUMN storage_key TEXT COLLATE "C";
ALTER TABLE documents ADD COLUMN original_filename TEXT COLLATE "C";
ALTER TABLE documents ADD COLUMN content_type TEXT COLLATE "C";
ALTER TABLE documents ADD COLUMN size_bytes INTEGER;
ALTER TABLE documents ADD COLUMN created_at_utc TEXT COLLATE "C";

CREATE INDEX idx_documents_storage_key ON documents (storage_key);

CREATE TABLE storage_orphans (
    orphan_id TEXT COLLATE "C" PRIMARY KEY,
    storage_key TEXT COLLATE "C" NOT NULL UNIQUE,
    org_id TEXT COLLATE "C" REFERENCES organizations (org_id),
    reason TEXT COLLATE "C" NOT NULL,
    recorded_at_utc TEXT COLLATE "C" NOT NULL,
    resolved_at_utc TEXT COLLATE "C"
);
