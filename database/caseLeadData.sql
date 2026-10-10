
drop database gam_db;
create database if not exists gam_db;
use gam_db;




ALTER TABLE cases
  ADD COLUMN supabase_user_id VARCHAR(64) NOT NULL AFTER id,
  ADD COLUMN upload_id        VARCHAR(64) NOT NULL AFTER supabase_user_id,
  ADD COLUMN report_extras    JSON NULL,
  MODIFY ack_no VARCHAR(32) NULL,
  DROP INDEX uq_cases_ack,
  ADD UNIQUE KEY uq_cases_upload (upload_id),
  ADD KEY idx_cases_user_ack (supabase_user_id, ack_no);
  
  
CREATE TABLE cases (
  id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ack_no                VARCHAR(32)   NOT NULL,
  status                VARCHAR(64)   NULL,
  base_debit_total      DECIMAL(14,2) NULL,
  reported_fraud_total  DECIMAL(14,2) NULL,
  hold_total            DECIMAL(14,2) NULL,
  reported_lien_total   DECIMAL(14,2) NULL,
  holds_match_lien      TINYINT(1)    NULL,
  checks                JSON          NULL,   -- the full "checks" object
  raw_flow              JSON          NULL,   -- full transaction_flow.json (audit)
  created_at            TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_cases_ack (ack_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE nodes (
  case_id            BIGINT UNSIGNED NOT NULL,
  node_id            VARCHAR(64)   NOT NULL,           -- e.g. node:776d500dfffdca5e
  layer              TINYINT UNSIGNED NOT NULL,        -- 0 = victim debit
  bank               VARCHAR(255)  NULL,               -- receiving bank
  action_taken_by    VARCHAR(255)  NULL,
  account_no         VARCHAR(64)   NULL,
  utr                VARCHAR(32)   NULL,
  tx_amount          DECIMAL(14,2) NULL,
  disputed_amount    DECIMAL(14,2) NULL,
  amount_estimated   TINYINT(1)    NOT NULL DEFAULT 0, -- disputed amount was missing
  frozen_amount      DECIMAL(14,2) NOT NULL DEFAULT 0,
  unaccounted_amount DECIMAL(14,2) NULL,
  embedded_ids       JSON          NULL,
  root_ids           JSON          NULL,
  source_row_ids     JSON          NULL,
  remarks            TEXT          NULL,
  PRIMARY KEY (case_id, node_id),
  KEY idx_nodes_layer   (case_id, layer),
  KEY idx_nodes_account (account_no),
  KEY idx_nodes_utr     (utr),
  CONSTRAINT fk_nodes_case FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE edges (
  id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  case_id          BIGINT UNSIGNED NOT NULL,
  from_node        VARCHAR(64)   NOT NULL,
  to_node          VARCHAR(64)   NOT NULL,
  match_rule       VARCHAR(48)   NOT NULL,
  confidence       DECIMAL(3,2)  NOT NULL,
  amount_passed    DECIMAL(14,2) NULL,
  ambiguous        TINYINT(1)    NOT NULL DEFAULT 0,
  merged           TINYINT(1)    NOT NULL DEFAULT 0,
  amount_estimated TINYINT(1)    NOT NULL DEFAULT 0,
  UNIQUE KEY uq_edge (case_id, from_node, to_node),
  KEY idx_edges_to (case_id, to_node),
  CONSTRAINT fk_edges_from FOREIGN KEY (case_id, from_node) REFERENCES nodes(case_id, node_id) ON DELETE CASCADE,
  CONSTRAINT fk_edges_to   FOREIGN KEY (case_id, to_node)   REFERENCES nodes(case_id, node_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE holds (
  case_id         BIGINT UNSIGNED NOT NULL,
  hold_id         VARCHAR(64)   NOT NULL,              -- e.g. hold:51ed214c2403ebe2
  account_no      VARCHAR(64)   NULL,
  hold_amount     DECIMAL(14,2) NULL,
  hold_date       DATE          NULL,
  action_taken_by VARCHAR(255)  NULL,
  embedded_ids    JSON          NULL,
  source_row_ids  JSON          NULL,
  remarks         TEXT          NULL,
  PRIMARY KEY (case_id, hold_id),
  KEY idx_holds_account (account_no),
  CONSTRAINT fk_holds_case FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE hold_links (
  case_id    BIGINT UNSIGNED NOT NULL,
  hold_id    VARCHAR(64)   NOT NULL,
  node_id    VARCHAR(64)   NOT NULL,
  match_rule VARCHAR(48)   NOT NULL,
  confidence DECIMAL(3,2)  NOT NULL,
  amount     DECIMAL(14,2) NULL,
  PRIMARY KEY (case_id, hold_id),
  KEY idx_hold_links_node (case_id, node_id),
  CONSTRAINT fk_hl_hold FOREIGN KEY (case_id, hold_id) REFERENCES holds(case_id, hold_id) ON DELETE CASCADE,
  CONSTRAINT fk_hl_node FOREIGN KEY (case_id, node_id) REFERENCES nodes(case_id, node_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE rejected_candidates (
  id        BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  case_id   BIGINT UNSIGNED NOT NULL,
  from_node VARCHAR(64) NOT NULL,
  to_node   VARCHAR(64) NOT NULL,
  reason    VARCHAR(128) NOT NULL,
  KEY idx_rejected_case (case_id),
  CONSTRAINT fk_rej_case FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The "no flow" lists are derived, so they can never go stale.
CREATE VIEW v_unlinked_rows AS      -- trail rows with no parent
SELECT n.* FROM nodes n
WHERE n.layer > 0
  AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.case_id = n.case_id AND e.to_node = n.node_id);

CREATE VIEW v_end_of_trail AS       -- has a parent, passes nothing on
SELECT n.* FROM nodes n
WHERE n.layer > 0
  AND EXISTS     (SELECT 1 FROM edges e WHERE e.case_id = n.case_id AND e.to_node   = n.node_id)
  AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.case_id = n.case_id AND e.from_node = n.node_id);

CREATE VIEW v_untraced_base AS      -- victim debits that matched nothing
SELECT n.* FROM nodes n
WHERE n.layer = 0
  AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.case_id = n.case_id AND e.from_node = n.node_id);

CREATE VIEW v_no_flow AS            -- no parent, no child, no hold
SELECT n.* FROM nodes n
WHERE NOT EXISTS (SELECT 1 FROM edges e WHERE e.case_id = n.case_id AND (e.from_node = n.node_id OR e.to_node = n.node_id))
  AND NOT EXISTS (SELECT 1 FROM hold_links h WHERE h.case_id = n.case_id AND h.node_id = n.node_id);
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  
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




create table if not exists placeholder(
	id 					int 		primary key 	auto_increment,
    supabase_user_id 	CHAR(36) 	NOT NULL,
    upload_id         	CHAR(36)    NOT NULL
);


desc requisitions;

CREATE TABLE requisitions (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    upload_id        CHAR(36)     NOT NULL,
    supabase_user_id CHAR(36)     NOT NULL,
    ack_no           VARCHAR(32)  NULL,
    bank_name        VARCHAR(255) NOT NULL,
    normalized_name  VARCHAR(255) NOT NULL,
    to_emails        JSON         NOT NULL,
    delivered_to     VARCHAR(320) NULL,
    reply_to         VARCHAR(320) NOT NULL,
    subject          VARCHAR(500) NOT NULL,
    body_text        MEDIUMTEXT   NOT NULL,
    item_hash        CHAR(64)     NOT NULL,
    test_mode        TINYINT      NOT NULL DEFAULT 0,
    status           VARCHAR(16)  NOT NULL DEFAULT 'sending',
    message_id       VARCHAR(255) NULL,
    error_message    VARCHAR(500) NULL,
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at          DATETIME     NULL,
    KEY idx_req_dup  (upload_id, normalized_name, item_hash, status),
    KEY idx_req_user (supabase_user_id, upload_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;