CREATE TABLE IF NOT EXISTS experiment_assistant.chat_run_bindings (
 session_id CHAR(32) NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(session_id),
 CONSTRAINT fk_chat_binding_session FOREIGN KEY(session_id,user_id) REFERENCES experiment_assistant.chat_sessions(id,user_id) ON DELETE CASCADE ON UPDATE RESTRICT,
 CONSTRAINT fk_chat_binding_run FOREIGN KEY(run_id,user_id) REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
