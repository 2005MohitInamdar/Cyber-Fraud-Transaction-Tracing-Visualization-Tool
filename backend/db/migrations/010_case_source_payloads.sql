-- Migration 010: preserve mapped extraction JSON for immutable case rebuilds.
-- Additive only; run against gam_db before deploying bank-response apply.
USE gam_db;

CREATE TABLE IF NOT EXISTS case_source_payloads (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    upload_id CHAR(36) NOT NULL,
    supabase_user_id CHAR(36) NOT NULL,
    table_name VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_payload (upload_id, table_name),
    KEY idx_case_source_payloads_user_upload (supabase_user_id, upload_id)
);
