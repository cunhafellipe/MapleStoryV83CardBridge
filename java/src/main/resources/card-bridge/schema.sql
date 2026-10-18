CREATE TABLE IF NOT EXISTS card_bridge_codes (
 issuance_id CHAR(36) PRIMARY KEY,
 code_hash CHAR(64) NOT NULL UNIQUE,
 account_id INT NOT NULL,
 item_id INT NOT NULL,
 quantity SMALLINT NOT NULL,
 pet_days INT NOT NULL DEFAULT 0,
 state VARCHAR(10) NOT NULL DEFAULT 'READY',
 claim_session CHAR(36),
 claim_character INT,
 receipt_id CHAR(36),
 used_at BIGINT,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX card_bridge_owner(account_id,state)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS card_bridge_payments (
 order_id CHAR(64) PRIMARY KEY,
 account_id INT NOT NULL,
 cash_type INT NOT NULL DEFAULT 1,
 amount INT NOT NULL,
 balance_after INT NOT NULL,
 payment_state VARCHAR(16) NOT NULL DEFAULT 'LEGACY_PAID',
 intent TEXT,
 fingerprint CHAR(64),
 preparation_id CHAR(67),
 account_incarnation CHAR(36),
 purchase_id VARCHAR(128),
 compensation_id CHAR(67),
 refund_owed BOOLEAN NOT NULL DEFAULT FALSE,
 cancellation_reason VARCHAR(40),
 terminal_at BIGINT,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX card_bridge_payment_pending(payment_state,order_id),
 INDEX card_bridge_payment_headroom(account_id,cash_type,payment_state)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS card_bridge_account_identity (
 account_id INT PRIMARY KEY,
 live_account_id INT UNIQUE,
 incarnation CHAR(36) NOT NULL UNIQUE,
 account_name VARCHAR(13) NOT NULL,
 FOREIGN KEY(live_account_id) REFERENCES accounts(id) ON DELETE RESTRICT
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS card_bridge_awards (
 award_id CHAR(64) PRIMARY KEY,
 account_id INT NOT NULL,
 cash_type INT NOT NULL,
 amount INT NOT NULL,
 source VARCHAR(24) NOT NULL,
 source_id VARCHAR(128) NOT NULL,
 payer_account INT,
 payer_character INT,
 payment_amount INT,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX card_bridge_award_owner(account_id,created_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS card_bridge_coupon_claims (
 code_id INT PRIMARY KEY,
 account_id INT NOT NULL,
 receipt_id CHAR(36) NOT NULL UNIQUE,
 payload TEXT NOT NULL,
 state VARCHAR(16) NOT NULL,
 claim_session CHAR(36),
 claim_character INT,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX card_bridge_coupon_pending(account_id,state)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS card_bridge_outbox (
 receipt_id CHAR(36) PRIMARY KEY,
 payload TEXT NOT NULL,
 delivered BOOLEAN NOT NULL DEFAULT FALSE,
 attempts INT NOT NULL DEFAULT 0,
 last_attempt BIGINT,
 next_attempt BIGINT NOT NULL DEFAULT 0,
 last_error VARCHAR(48),
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX card_bridge_delivery(delivered,created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS card_bridge_sessions (
 token_hash CHAR(64) PRIMARY KEY,
 account_id INT NOT NULL,
 expires_at BIGINT NOT NULL,
 INDEX card_bridge_session_expiry(expires_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS card_bridge_native_purchases (
 receipt_id CHAR(36) PRIMARY KEY,
 account_id INT NOT NULL,
 character_id INT NOT NULL,
 cash_type INT NOT NULL,
 amount INT NOT NULL,
 kind VARCHAR(16) NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS card_bridge_payment_attribution (
 order_id CHAR(64) PRIMARY KEY,
 decision_key VARCHAR(128) NOT NULL UNIQUE,
 payload TEXT NOT NULL,
 evidence_sha CHAR(64) NOT NULL,
 account_incarnation CHAR(36) NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(order_id) REFERENCES card_bridge_payments(order_id) ON DELETE RESTRICT
) ENGINE=InnoDB;
