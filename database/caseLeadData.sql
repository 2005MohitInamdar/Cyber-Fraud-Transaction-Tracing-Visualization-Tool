-- ============================================================
-- gam_db schema
-- ============================================================

CREATE DATABASE IF NOT EXISTS gam_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'gam_app'@'localhost' IDENTIFIED BY '200520021970@.com';
GRANT SELECT, INSERT, UPDATE ON gam_db.* TO 'gam_app'@'localhost';
FLUSH PRIVILEGES;

USE gam_db;

-- ------------------------------------------------------------
-- fraud_case_uploads: lead officer / case metadata per upload
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fraud_case_uploads (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    supabase_user_id  CHAR(36)     NOT NULL,
    upload_id         CHAR(36)     NOT NULL,
    inspector_name    VARCHAR(150) NOT NULL,
    inspector_rank    VARCHAR(100) NOT NULL,
    inspector_branch  VARCHAR(200) NOT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_upload_id (upload_id),
    INDEX idx_supabase_user_id (supabase_user_id),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- file_upload_sessions: chunked-upload session tracking
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS file_upload_sessions (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    supabase_user_id  CHAR(36)     NOT NULL,
    upload_id         CHAR(36)     NOT NULL,
    file_name         VARCHAR(500) NOT NULL,
    file_size         BIGINT UNSIGNED NOT NULL,
    content_type      VARCHAR(100) NOT NULL,
    chunk_size        INT UNSIGNED NOT NULL,
    total_chunks      INT UNSIGNED NOT NULL,
    received_chunks   JSON NOT NULL DEFAULT (JSON_ARRAY()),
    file_hash         VARCHAR(128) NOT NULL,
    status            VARCHAR(50)  NOT NULL DEFAULT 'pending',
    file_path         VARCHAR(512) DEFAULT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    completed_at      DATETIME DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_upload_id (upload_id),
    INDEX idx_supabase_user_id (supabase_user_id),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- amount_summary
-- ------------------------------------------------------------

alter table amount_summary add column upload_id CHAR(36) NOT NULL after supabase_user_id;
alter table amount_summary drop primary key;
alter table amount_summary add column amount_summary_id int primary key auto_increment;
desc amount_summary;
ALTER TABLE amount_summary
    MODIFY description VARCHAR(255),
    MODIFY amount DECIMAL(12,2);
    
CREATE TABLE IF NOT EXISTS amount_summary (
    s_no              INT NOT NULL,
    description       VARCHAR(255) NOT NULL,
    amount            DECIMAL(12,2) NOT NULL,
    supabase_user_id  CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    INDEX idx_supabase_user_id (supabase_user_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;



-- ------------------------------------------------------------
-- complaint_meta
-- ------------------------------------------------------------
alter table complaint_meta add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE complaint_meta MODIFY id INT NOT NULL;
alter table complaint_meta drop primary key;
alter table complaint_meta add column complaint_meta_id int primary key auto_increment;
desc complaint_meta;
ALTER TABLE complaint_meta
    modify complaint_accepted_by     VARCHAR(255) DEFAULT NULL,
    modify complaint_accepted_date   DATETIME DEFAULT NULL,
    modify current_status            VARCHAR(100) DEFAULT NULL,
    modify under_process_date        DATETIME DEFAULT NULL;
    

CREATE TABLE IF NOT EXISTS complaint_meta (
    id                        INT NOT NULL AUTO_INCREMENT,
    complaint_accepted_by     VARCHAR(255) DEFAULT NULL,
    complaint_accepted_date   DATETIME DEFAULT NULL,
    current_status            VARCHAR(100) DEFAULT NULL,
    under_process_date        DATETIME DEFAULT NULL,
    supabase_user_id          CHAR(36) NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_supabase_user_id (supabase_user_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;



-- ------------------------------------------------------------
-- complaint_transactions
-- ------------------------------------------------------------

alter table complaint_transactions add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE complaint_transactions MODIFY id INT NOT NULL;
alter table complaint_transactions drop primary key;
alter table complaint_transactions add column complaint_transactions_id int primary key auto_increment;
desc complaint_transactions;
ALTER TABLE complaint_transactions
	modify account_wallet_id      VARCHAR(255) DEFAULT NULL,
    modify transaction_id         VARCHAR(50) DEFAULT NULL,
    modify card_details           VARCHAR(255) DEFAULT NULL,
    modify transaction_amount     DECIMAL(12,2) DEFAULT NULL,
    modify reference_no           VARCHAR(100) DEFAULT NULL,
    modify transaction_datetime   DATETIME DEFAULT NULL,
    modify complaint_date         DATETIME DEFAULT NULL,
    modify bank_fi                VARCHAR(100) DEFAULT NULL;
    
CREATE TABLE IF NOT EXISTS complaint_transactions (
    s_no                   INT NOT NULL,
    account_wallet_id      VARCHAR(255) DEFAULT NULL,
    transaction_id         VARCHAR(50) DEFAULT NULL,
    card_details           VARCHAR(255) DEFAULT NULL,
    transaction_amount     DECIMAL(12,2) DEFAULT NULL,
    reference_no           VARCHAR(100) DEFAULT NULL,
    transaction_datetime   DATETIME DEFAULT NULL,
    complaint_date         DATETIME DEFAULT NULL,
    bank_fi                VARCHAR(100) DEFAULT NULL,
    supabase_user_id       CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    INDEX idx_supabase_user_id (supabase_user_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- failed_transactions
-- ------------------------------------------------------------

alter table failed_transactions add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE failed_transactions MODIFY s_no INT NOT NULL;
alter table failed_transactions drop primary key;
alter table failed_transactions add column failed_transactions_id int primary key auto_increment;
desc failed_transactions;
ALTER TABLE failed_transactions
    modify account_no          VARCHAR(255) DEFAULT NULL,
    modify transaction_date    DATE DEFAULT NULL,
    modify transaction_amount  DECIMAL(12,2) DEFAULT NULL,
    modify reference_remarks   VARCHAR(255) DEFAULT NULL,
    modify action_taken_by     VARCHAR(100) DEFAULT NULL,
    modify date_of_action      DATETIME DEFAULT NULL;
CREATE TABLE IF NOT EXISTS failed_transactions (
    s_no                INT NOT NULL,
    account_no          VARCHAR(255) DEFAULT NULL,
    transaction_date    DATE DEFAULT NULL,
    transaction_amount  DECIMAL(12,2) DEFAULT NULL,
    reference_remarks   VARCHAR(255) DEFAULT NULL,
    action_taken_by     VARCHAR(100) DEFAULT NULL,
    date_of_action      DATETIME DEFAULT NULL,
    supabase_user_id    CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    INDEX idx_supabase_user_id (supabase_user_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- hold_accounts
-- ------------------------------------------------------------

alter table hold_accounts add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE hold_accounts MODIFY s_no INT NOT NULL;
alter table hold_accounts drop primary key;
alter table hold_accounts add column hold_accounts_id int primary key auto_increment;
desc hold_accounts;
ALTER TABLE hold_accounts
    modify account_no          VARCHAR(50) DEFAULT NULL,
    modify hold_date           DATE DEFAULT NULL,
    modify hold_amount         DECIMAL(12,2) DEFAULT NULL,
    modify reference_remarks   VARCHAR(255) DEFAULT NULL,
    modify action_taken_by     VARCHAR(100) DEFAULT NULL,
    modify date_of_action      DATETIME DEFAULT NULL;
    
CREATE TABLE IF NOT EXISTS hold_accounts (
    s_no                INT NOT NULL,
    account_no          VARCHAR(50) DEFAULT NULL,
    hold_date           DATE DEFAULT NULL,
    hold_amount         DECIMAL(12,2) DEFAULT NULL,
    reference_remarks   VARCHAR(255) DEFAULT NULL,
    action_taken_by     VARCHAR(100) DEFAULT NULL,
    date_of_action      DATETIME DEFAULT NULL,
    supabase_user_id    CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    INDEX idx_supabase_user_id (supabase_user_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- lien_transactions
-- ------------------------------------------------------------

alter table lien_transactions add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE lien_transactions MODIFY s_no INT NOT NULL;
alter table lien_transactions drop primary key;
alter table lien_transactions add column lien_transactions_id int primary key auto_increment;
desc lien_transactions;
ALTER TABLE lien_transactions
    modify bank_fi                VARCHAR(255) DEFAULT NULL,
    modify account_no             VARCHAR(50) DEFAULT NULL,
    modify ifsc_code              VARCHAR(20) DEFAULT NULL,
    modify layer                  INT DEFAULT NULL,
    modify transaction_id         VARCHAR(50) DEFAULT NULL,
    modify transaction_datetime   DATETIME DEFAULT NULL,
    modify transaction_amount     DECIMAL(12,2) DEFAULT NULL,
    modify disputed_amount        DECIMAL(12,2) DEFAULT NULL,
    modify reference_remarks      VARCHAR(500) DEFAULT NULL,
    modify action_taken_by        VARCHAR(255) DEFAULT NULL,
    modify date_of_action         DATETIME DEFAULT NULL;
    
CREATE TABLE IF NOT EXISTS lien_transactions (
    s_no                   INT NOT NULL,
    bank_fi                VARCHAR(255) DEFAULT NULL,
    account_no             VARCHAR(50) DEFAULT NULL,
    ifsc_code              VARCHAR(20) DEFAULT NULL,
    layer                  INT DEFAULT NULL,
    transaction_id         VARCHAR(50) DEFAULT NULL,
    transaction_datetime   DATETIME DEFAULT NULL,
    transaction_amount     DECIMAL(12,2) DEFAULT NULL,
    disputed_amount        DECIMAL(12,2) DEFAULT NULL,
    reference_remarks      VARCHAR(500) DEFAULT NULL,
    action_taken_by        VARCHAR(255) DEFAULT NULL,
    date_of_action         DATETIME DEFAULT NULL,
    supabase_user_id       CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    INDEX idx_supabase_user_id (supabase_user_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- no_action_references
-- ------------------------------------------------------------

alter table no_action_references add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE no_action_references MODIFY s_no INT NOT NULL;
alter table no_action_references drop primary key;
alter table no_action_references add column no_action_references_id int primary key auto_increment;
desc no_action_references;
ALTER TABLE no_action_references
    modify reference_remarks   VARCHAR(255) DEFAULT NULL,
    modify action_taken_by     VARCHAR(100) DEFAULT NULL,
    modify date_of_action      DATETIME DEFAULT NULL;
    
CREATE TABLE IF NOT EXISTS no_action_references (
    s_no                INT NOT NULL,
    reference_remarks   VARCHAR(255) DEFAULT NULL,
    action_taken_by     VARCHAR(100) DEFAULT NULL,
    date_of_action      DATETIME DEFAULT NULL,
    supabase_user_id    CHAR(36) NOT NULL,
    PRIMARY KEY (s_no)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- pending_transactions
-- ------------------------------------------------------------

alter table pending_transactions add column upload_id CHAR(36) NOT NULL after supabase_user_id;
ALTER TABLE pending_transactions MODIFY s_no INT NOT NULL;
alter table pending_transactions drop primary key;
alter table pending_transactions add column pending_transactions_id int primary key auto_increment;
desc pending_transactions;
ALTER TABLE pending_transactions
    modify bank                        VARCHAR(100) DEFAULT NULL,
    modify no_of_transactions_pending  INT DEFAULT NULL,
    modify amount_pending              DECIMAL(12,2) DEFAULT NULL,
    modify pending_from                DATETIME DEFAULT NULL;    

CREATE TABLE IF NOT EXISTS pending_transactions (
    s_no                        INT NOT NULL,
    bank                        VARCHAR(100) DEFAULT NULL,
    no_of_transactions_pending  INT DEFAULT NULL,
    amount_pending              DECIMAL(12,2) DEFAULT NULL,
    pending_from                DATETIME DEFAULT NULL,
    supabase_user_id            CHAR(36) NOT NULL,
    PRIMARY KEY (s_no)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


create table if not exists placeholder(
	id 					int 		primary key 	auto_increment,
    supabase_user_id 	CHAR(36) 	NOT NULL,
    upload_id         	CHAR(36)    NOT NULL
);

SHOW TABLES;
desc placeholder;
use gam_db;