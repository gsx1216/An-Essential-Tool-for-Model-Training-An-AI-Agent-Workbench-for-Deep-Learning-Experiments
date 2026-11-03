CREATE TABLE IF NOT EXISTS experiment_assistant.run_metric_events (
    run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    source_offset INT UNSIGNED NOT NULL,
    step BIGINT UNSIGNED NOT NULL,
    epoch BIGINT UNSIGNED NULL,
    split VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    dataset VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'unspecified',
    values_json TEXT COLLATE utf8mb4_bin NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (run_id, source_offset),
    KEY idx_metrics_run_split_offset (run_id, split, source_offset),
    KEY idx_metrics_run_dataset_split (run_id, dataset, split, source_offset),
    CONSTRAINT fk_metric_event_run FOREIGN KEY (run_id, user_id)
        REFERENCES experiment_assistant.experiment_runs (id, user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_metric_event_offset CHECK (source_offset <= 10485760),
    CONSTRAINT ck_metric_event_step CHECK (step <= 9007199254740991 AND (epoch IS NULL OR epoch <= 9007199254740991)),
    CONSTRAINT ck_metric_event_split CHECK (split IN ('train', 'validation', 'test')),
    CONSTRAINT ck_metric_event_values CHECK (JSON_VALID(values_json) AND JSON_TYPE(values_json) = 'OBJECT' AND OCTET_LENGTH(values_json) <= 8192)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
