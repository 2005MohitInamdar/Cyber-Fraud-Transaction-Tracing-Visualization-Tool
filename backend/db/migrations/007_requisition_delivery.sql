-- Migration 007: add delivered_to column to requisitions table
-- Additive only; app user (gam_app) has INSERT/UPDATE on requisitions already.
-- Run as admin: mysql -u root -p gam_db < db/migrations/run_migration_007.sql

USE gam_db;

ALTER TABLE requisitions
    ADD COLUMN delivered_to VARCHAR(320) NULL
    AFTER cc_emails;

SELECT 'Migration 007 applied successfully.' AS result;
