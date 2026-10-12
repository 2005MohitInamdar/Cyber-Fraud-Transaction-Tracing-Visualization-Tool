-- Additive migration for bank-response parsing and immutable graph revisions.
-- Run once after the existing case/flow schema migration.

ALTER TABLE cases
  ADD COLUMN revision INT NOT NULL DEFAULT 1,
  ADD COLUMN parent_case_id BIGINT UNSIGNED NULL,
  ADD COLUMN change_note VARCHAR(255) NULL;

ALTER TABLE cases DROP INDEX uq_cases_upload;
ALTER TABLE cases ADD UNIQUE KEY uq_cases_upload_rev (upload_id, revision);
ALTER TABLE cases ADD KEY idx_cases_latest (upload_id, supabase_user_id, revision);

CREATE TABLE bank_responses (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  upload_id CHAR(36) NOT NULL,
  supabase_user_id CHAR(36) NOT NULL,
  requisition_id BIGINT UNSIGNED NULL,
  file_hash CHAR(64) NOT NULL,
  file_path VARCHAR(512) NOT NULL,
  status ENUM('parsed','applied','rejected') NOT NULL DEFAULT 'parsed',
  parse_report JSON NULL,
  anchor_node_id VARCHAR(64) NULL,
  layer_shift INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  applied_at TIMESTAMP NULL,
  UNIQUE KEY uq_resp_hash (upload_id, file_hash),
  KEY idx_response_owner (upload_id, supabase_user_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE bank_response_rows (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  response_id BIGINT UNSIGNED NOT NULL,
  table_name ENUM('lien_transactions','hold_accounts') NOT NULL,
  row_no INT NOT NULL,
  data JSON NOT NULL,
  row_key CHAR(40) NOT NULL,
  is_duplicate TINYINT(1) NOT NULL DEFAULT 0,
  duplicate_reason VARCHAR(100) NULL,
  included TINYINT(1) NOT NULL DEFAULT 1,
  KEY idx_response_rows (response_id),
  CONSTRAINT fk_response_rows_response FOREIGN KEY (response_id) REFERENCES bank_responses(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE node_annotations (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  upload_id CHAR(36) NOT NULL,
  supabase_user_id CHAR(36) NOT NULL,
  node_id VARCHAR(64) NOT NULL,
  kind ENUM('no_onward','withdrawn','lien_confirmed') NOT NULL,
  note VARCHAR(500) NULL,
  response_id BIGINT UNSIGNED NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_annotations_node (upload_id, node_id),
  CONSTRAINT fk_annotations_response FOREIGN KEY (response_id) REFERENCES bank_responses(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
