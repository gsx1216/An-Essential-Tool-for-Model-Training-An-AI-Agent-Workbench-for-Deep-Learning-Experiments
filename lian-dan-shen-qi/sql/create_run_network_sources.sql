CREATE TABLE IF NOT EXISTS experiment_assistant.run_network_sources (
 run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 path VARCHAR(512) NOT NULL,
 file_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 selected_at DATETIME(6) NOT NULL,
 PRIMARY KEY (run_id),
 CONSTRAINT fk_network_source_owner FOREIGN KEY (run_id,user_id)
 REFERENCES experiment_assistant.experiment_runs (id,user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4;
