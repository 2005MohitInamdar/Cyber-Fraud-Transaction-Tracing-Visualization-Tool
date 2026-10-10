-- Migration 005: Requisition emails table
-- Run once against gam_db.
-- No DROP, TRUNCATE or ALTER of existing tables.
-- The app user (gam_app) has SELECT, INSERT, UPDATE only, so no GRANT is needed here.

USE gam_db;

CREATE TABLE IF NOT EXISTS requisitions (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    upload_id          CHAR(36)        NOT NULL,
    supabase_user_id   CHAR(36)        NOT NULL,
    ack_no             VARCHAR(32)     NOT NULL,
    bank_name          VARCHAR(255)    NOT NULL,
    normalized_name    VARCHAR(255)    NOT NULL,
    to_emails          JSON            NOT NULL,
    cc_emails          JSON            NULL,
    reply_to           VARCHAR(320)    NOT NULL,
    subject            VARCHAR(300)    NOT NULL,
    body_text          MEDIUMTEXT      NOT NULL,
    items              JSON            NOT NULL,
    item_hash          CHAR(64)        NOT NULL,
    test_mode          TINYINT(1)      NOT NULL DEFAULT 0,
    status             ENUM('sending', 'sent', 'failed') NOT NULL,
    message_id         VARCHAR(255)    NULL,
    error_message      VARCHAR(500)    NULL,
    created_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at            DATETIME        NULL,
    PRIMARY KEY (id),
    KEY idx_req_case   (upload_id, supabase_user_id),
    KEY idx_req_bank   (supabase_user_id, normalized_name, created_at),
    KEY idx_req_hash   (upload_id, normalized_name, item_hash)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
