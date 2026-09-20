-- gam_db schema

CREATE TABLE fraud_case_uploads (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    supabase_user_id  CHAR(36)     NOT NULL,
    upload_id         CHAR(36)     NOT NULL,
    inspector_name    VARCHAR(150) NOT NULL,
    inspector_rank    VARCHAR(100) NOT NULL,
    inspector_branch  VARCHAR(200) NOT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_upload_id (upload_id),
    KEY idx_supabase_user_id (supabase_user_id),
    KEY idx_created_at (created_at)
);

CREATE TABLE file_upload_sessions (
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
    file_path         VARCHAR(512) NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at      DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_upload_id (upload_id),
    KEY idx_supabase_user_id (supabase_user_id),
    KEY idx_status (status),
    KEY idx_created_at (created_at)
);

CREATE TABLE amount_summary (
    s_no              INT NOT NULL,
    description       VARCHAR(255) NOT NULL,
    amount            DECIMAL(12,2) NOT NULL,
    supabase_user_id  CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    KEY idx_supabase_user_id (supabase_user_id)
);

CREATE TABLE complaint_meta (
    id                        INT NOT NULL AUTO_INCREMENT,
    complaint_accepted_by     VARCHAR(255) NULL,
    complaint_accepted_date   DATETIME NULL,
    current_status            VARCHAR(100) NULL,
    under_process_date        DATETIME NULL,
    supabase_user_id          CHAR(36) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_supabase_user_id (supabase_user_id)
);

CREATE TABLE complaint_transactions (
    s_no                   INT NOT NULL,
    account_wallet_id      VARCHAR(255) NULL,
    transaction_id         VARCHAR(50) NULL,
    card_details           VARCHAR(255) NULL,
    transaction_amount     DECIMAL(12,2) NULL,
    reference_no           VARCHAR(100) NULL,
    transaction_datetime   DATETIME NULL,
    complaint_date         DATETIME NULL,
    bank_fi                VARCHAR(100) NULL,
    supabase_user_id       CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    KEY idx_supabase_user_id (supabase_user_id)
);

CREATE TABLE failed_transactions (
    s_no                INT NOT NULL,
    account_no          VARCHAR(255) NULL,
    transaction_date    DATE NULL,
    transaction_amount  DECIMAL(12,2) NULL,
    reference_remarks   VARCHAR(255) NULL,
    action_taken_by     VARCHAR(100) NULL,
    date_of_action      DATETIME NULL,
    supabase_user_id    CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    KEY idx_supabase_user_id (supabase_user_id)
);

CREATE TABLE hold_accounts (
    s_no                INT NOT NULL,
    account_no          VARCHAR(50) NULL,
    hold_date           DATE NULL,
    hold_amount         DECIMAL(12,2) NULL,
    reference_remarks   VARCHAR(255) NULL,
    action_taken_by     VARCHAR(100) NULL,
    date_of_action      DATETIME NULL,
    supabase_user_id    CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    KEY idx_supabase_user_id (supabase_user_id)
);

CREATE TABLE lien_transactions (
    s_no                   INT NOT NULL,
    bank_fi                VARCHAR(255) NULL,
    account_no             VARCHAR(50) NULL,
    ifsc_code              VARCHAR(20) NULL,
    layer                  INT NULL,
    transaction_id         VARCHAR(50) NULL,
    transaction_datetime   DATETIME NULL,
    transaction_amount     DECIMAL(12,2) NULL,
    disputed_amount        DECIMAL(12,2) NULL,
    reference_remarks      VARCHAR(500) NULL,
    action_taken_by        VARCHAR(255) NULL,
    date_of_action         DATETIME NULL,
    supabase_user_id       CHAR(36) NOT NULL,
    PRIMARY KEY (s_no),
    KEY idx_supabase_user_id (supabase_user_id)
);

CREATE TABLE no_action_references (
    s_no                INT NOT NULL,
    reference_remarks   VARCHAR(255) NULL,
    action_taken_by     VARCHAR(100) NULL,
    date_of_action      DATETIME NULL,
    supabase_user_id    CHAR(36) NOT NULL,
    PRIMARY KEY (s_no)
);

CREATE TABLE pending_transactions (
    s_no                        INT NOT NULL,
    bank                        VARCHAR(100) NULL,
    no_of_transactions_pending  INT NULL,
    amount_pending              DECIMAL(12,2) NULL,
    pending_from                DATETIME NULL,
    supabase_user_id            CHAR(36) NOT NULL,
    PRIMARY KEY (s_no)
);