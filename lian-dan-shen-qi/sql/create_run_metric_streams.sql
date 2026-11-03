CREATE TABLE IF NOT EXISTS experiment_assistant.run_metric_streams (
    run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    revision BIGINT UNSIGNED NOT NULL,
    source_offset INT UNSIGNED NOT NULL,
    state_json TEXT COLLATE utf8mb4_bin NOT NULL,
    state_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT fk_metric_stream_run FOREIGN KEY (run_id, user_id)
        REFERENCES experiment_assistant.experiment_runs (id, user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_metric_stream_revision CHECK (revision > 0),
    CONSTRAINT ck_metric_stream_offset CHECK (source_offset <= 10485760),
    CONSTRAINT ck_metric_stream_state CHECK (JSON_VALID(state_json) AND JSON_TYPE(state_json) = 'OBJECT' AND OCTET_LENGTH(state_json) <= 32768)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
