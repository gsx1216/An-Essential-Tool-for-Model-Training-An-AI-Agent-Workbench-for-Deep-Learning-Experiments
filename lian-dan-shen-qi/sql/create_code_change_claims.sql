CREATE TABLE IF NOT EXISTS experiment_assistant.code_change_claims (
 session_id CHAR(32) NOT NULL,
 turn_id CHAR(32) NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 change_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(session_id,turn_id),
 CONSTRAINT fk_code_change_claim_session FOREIGN KEY(session_id,user_id) REFERENCES experiment_assistant.chat_sessions(id,user_id) ON DELETE CASCADE,
 CONSTRAINT fk_code_change_claim_turn FOREIGN KEY(session_id,turn_id) REFERENCES experiment_assistant.chat_turns(session_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_code_change_claim_record FOREIGN KEY(change_id) REFERENCES experiment_assistant.code_changes(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
