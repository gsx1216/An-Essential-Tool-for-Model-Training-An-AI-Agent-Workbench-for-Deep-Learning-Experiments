CREATE TABLE IF NOT EXISTS experiment_assistant.research_references (
 id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 session_id CHAR(32) NOT NULL,user_id BIGINT UNSIGNED NOT NULL,turn_id CHAR(32) NOT NULL,attempt_id CHAR(32) NOT NULL,
 run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
 source_url VARCHAR(1500) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 title VARCHAR(300) NOT NULL,source_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 content_text MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 metadata_json TEXT COLLATE utf8mb4_bin NOT NULL,retrieved_at DATETIME(6) NOT NULL,
 PRIMARY KEY(id),KEY idx_references_session(session_id,retrieved_at DESC,id DESC),
 CONSTRAINT fk_reference_session FOREIGN KEY(session_id,user_id) REFERENCES experiment_assistant.chat_sessions(id,user_id) ON DELETE CASCADE,
 CONSTRAINT fk_reference_turn FOREIGN KEY(session_id,turn_id) REFERENCES experiment_assistant.chat_turns(session_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_reference_run FOREIGN KEY(run_id,user_id) REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT,
 CONSTRAINT ck_reference_content CHECK(OCTET_LENGTH(content_text) BETWEEN 1 AND 60000 AND JSON_VALID(metadata_json) AND OCTET_LENGTH(metadata_json)<=4096),
 CONSTRAINT ck_reference_kind CHECK(source_kind IN ('search_abstract','paper_abstract','paper_html','official_documentation'))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
