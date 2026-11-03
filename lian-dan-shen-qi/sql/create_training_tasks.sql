CREATE TABLE IF NOT EXISTS experiment_assistant.training_tasks (
 id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 name VARCHAR(80) NOT NULL,
 experiment_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 server_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 script_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 configuration_json TEXT COLLATE utf8mb4_bin NOT NULL,
 created_at DATETIME(6) NOT NULL,
 deleted_at DATETIME(6) NULL,
 active_name VARCHAR(80) GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN name ELSE NULL END) STORED,
 PRIMARY KEY(id), UNIQUE KEY uq_training_task_owner(id,user_id),
 UNIQUE KEY uq_training_task_name(user_id,active_name),
 CONSTRAINT fk_training_task_experiment FOREIGN KEY(experiment_id,user_id) REFERENCES experiment_assistant.experiments(id,user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
 CONSTRAINT fk_training_task_server FOREIGN KEY(server_id,user_id) REFERENCES experiment_assistant.server_connections(id,user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
 CONSTRAINT ck_training_task_name CHECK(CHAR_LENGTH(TRIM(name)) BETWEEN 1 AND 80),
 CONSTRAINT ck_training_task_config CHECK(JSON_VALID(configuration_json) AND OCTET_LENGTH(configuration_json)<=32768)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
