-- Run this as a MySQL admin/root user.
-- Usage: mysql -u root -p gam_db < db/migrations/run_migration_006.sql

USE gam_db;

CREATE TABLE IF NOT EXISTS bank_contacts (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    normalized_name  VARCHAR(255)    NOT NULL,
    bank_name        VARCHAR(255)    NOT NULL,
    email            VARCHAR(320)    NOT NULL,
    cc_emails        JSON            NULL,
    source           ENUM('directory','discovered','manual') NOT NULL,
    source_url       VARCHAR(500)    NULL,
    confidence       DECIMAL(3,2)    NULL,
    evidence         VARCHAR(500)    NULL,
    verified         TINYINT(1)      NOT NULL DEFAULT 0,
    verified_by      CHAR(36)        NULL,
    verified_at      DATETIME        NULL,
    is_active        TINYINT(1)      NOT NULL DEFAULT 1,
    last_checked_at  DATETIME        NULL,
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_bank_email (normalized_name, email),
    KEY idx_bank_norm (normalized_name, is_active, verified)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

GRANT SELECT, INSERT, UPDATE ON gam_db.bank_contacts TO 'gam_app'@'localhost';
FLUSH PRIVILEGES;

SELECT 'Migration 006 applied successfully.' AS result;
