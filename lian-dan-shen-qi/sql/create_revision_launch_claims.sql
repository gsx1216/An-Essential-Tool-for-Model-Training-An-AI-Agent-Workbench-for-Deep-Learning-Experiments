CREATE TABLE IF NOT EXISTS experiment_assistant.revision_launch_claims (
 session_id CHAR(32) NOT NULL, turn_id CHAR(32) NOT NULL, user_id BIGINT UNSIGNED NOT NULL,
 run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(session_id,turn_id),
 CONSTRAINT fk_revision_claim_session FOREIGN KEY(session_id,user_id) REFERENCES experiment_assistant.chat_sessions(id,user_id) ON DELETE CASCADE,
 CONSTRAINT fk_revision_claim_turn FOREIGN KEY(session_id,turn_id) REFERENCES experiment_assistant.chat_turns(session_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_revision_claim_run FOREIGN KEY(run_id,user_id) REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
