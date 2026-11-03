CREATE TABLE IF NOT EXISTS experiment_assistant.suggestion_evidence (
 task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 attempt_token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 call_index INT UNSIGNED NOT NULL,
 tool_name VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 arguments_json TEXT COLLATE utf8mb4_bin NOT NULL,
 result_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
 created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(task_id,attempt_token,call_index),
 CONSTRAINT fk_suggestion_evidence_task FOREIGN KEY(task_id) REFERENCES experiment_assistant.agent_tasks(id) ON DELETE RESTRICT,
 CONSTRAINT ck_suggestion_evidence_args CHECK(JSON_VALID(arguments_json) AND OCTET_LENGTH(arguments_json)<=2048),
 CONSTRAINT ck_suggestion_evidence_result CHECK(JSON_VALID(result_json) AND OCTET_LENGTH(result_json)<=16384),
 CONSTRAINT ck_suggestion_evidence_index CHECK(call_index BETWEEN 1 AND 8)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
