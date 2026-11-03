CREATE TABLE IF NOT EXISTS experiment_assistant.run_executions (
    run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    server_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    script_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    launch_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
    launch_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL,
    checked_at DATETIME(6) NULL,
    PRIMARY KEY (run_id),
    UNIQUE KEY uq_execution_job (job_id),
    KEY idx_execution_server (server_id),
    CONSTRAINT fk_execution_run FOREIGN KEY (run_id, user_id) REFERENCES experiment_assistant.experiment_runs (id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_execution_server FOREIGN KEY (server_id, user_id) REFERENCES experiment_assistant.server_connections (id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_execution_launch CHECK (JSON_VALID(launch_json) AND OCTET_LENGTH(launch_json) <= 131072)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
