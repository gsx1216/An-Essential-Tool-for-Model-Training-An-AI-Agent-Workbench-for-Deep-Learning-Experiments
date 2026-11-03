CREATE TABLE IF NOT EXISTS experiment_assistant.training_launch_claims (
 session_id CHAR(32) NOT NULL,
 turn_id CHAR(32) NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 plan_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(session_id,turn_id),
 CONSTRAINT fk_training_claim_session FOREIGN KEY(session_id,user_id) REFERENCES experiment_assistant.chat_sessions(id,user_id) ON DELETE CASCADE ON UPDATE RESTRICT,
 CONSTRAINT fk_training_claim_turn FOREIGN KEY(session_id,turn_id) REFERENCES experiment_assistant.chat_turns(session_id,id) ON DELETE CASCADE ON UPDATE RESTRICT,
 CONSTRAINT fk_training_claim_plan FOREIGN KEY(plan_id,user_id) REFERENCES experiment_assistant.chat_training_plans(id,user_id) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
