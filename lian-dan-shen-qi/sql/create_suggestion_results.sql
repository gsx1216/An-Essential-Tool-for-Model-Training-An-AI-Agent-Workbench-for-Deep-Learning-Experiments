CREATE TABLE IF NOT EXISTS experiment_assistant.suggestion_results (
 task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 content_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
 content_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 model_name VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(task_id),
 CONSTRAINT fk_suggestion_result_task FOREIGN KEY(task_id)
  REFERENCES experiment_assistant.agent_tasks(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
 CONSTRAINT ck_suggestion_result_json CHECK(JSON_VALID(content_json) AND OCTET_LENGTH(content_json)<=131072)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
