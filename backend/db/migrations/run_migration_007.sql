-- Run as admin/root: mysql -u root -p gam_db < db/migrations/run_migration_007.sql
USE gam_db;

-- Add delivered_to column only when it doesn't already exist (MySQL 5.7+ compatible)
SET @dbname = DATABASE();
SET @tablename = 'requisitions';
SET @columnname = 'delivered_to';
SET @preparedStatement = (
    SELECT IF(
        NOT EXISTS (
            SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = @dbname
              AND TABLE_NAME   = @tablename
              AND COLUMN_NAME  = @columnname
        ),
        CONCAT('ALTER TABLE ', @tablename,
               ' ADD COLUMN delivered_to VARCHAR(320) NULL AFTER cc_emails'),
        'SELECT ''Column already exists, skipping.'' AS result'
    )
);
PREPARE stmt FROM @preparedStatement;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT 'Migration 007 applied successfully.' AS result;
