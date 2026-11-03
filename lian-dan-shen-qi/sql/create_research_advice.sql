CREATE TABLE IF NOT EXISTS experiment_assistant.research_advice (
 id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,session_id CHAR(32) NOT NULL,user_id BIGINT UNSIGNED NOT NULL,
 turn_id CHAR(32) NOT NULL,attempt_id CHAR(32) NOT NULL,run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
 advice_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,rendered_text MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
 advice_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(id),UNIQUE KEY uq_research_advice_turn(turn_id),KEY idx_advice_session(session_id,created_at DESC,id DESC),
 CONSTRAINT fk_advice_session FOREIGN KEY(session_id,user_id) REFERENCES experiment_assistant.chat_sessions(id,user_id) ON DELETE CASCADE,
 CONSTRAINT fk_advice_turn FOREIGN KEY(session_id,turn_id) REFERENCES experiment_assistant.chat_turns(session_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_advice_run FOREIGN KEY(run_id,user_id) REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT,
 CONSTRAINT ck_advice_content CHECK(JSON_VALID(advice_json) AND OCTET_LENGTH(advice_json)<=32768 AND OCTET_LENGTH(rendered_text)<=32768)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
