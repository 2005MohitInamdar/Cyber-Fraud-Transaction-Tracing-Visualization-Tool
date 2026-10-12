-- Preserve the revision at which a bank response annotation first applies.
ALTER TABLE node_annotations
  ADD COLUMN case_revision INT NOT NULL DEFAULT 1 AFTER response_id,
  ADD KEY idx_annotations_revision (upload_id, supabase_user_id, case_revision);
