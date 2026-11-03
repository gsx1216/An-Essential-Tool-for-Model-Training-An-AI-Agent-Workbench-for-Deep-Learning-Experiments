CREATE TABLE IF NOT EXISTS experiment_assistant.run_reports (
    run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    version BIGINT UNSIGNED NOT NULL,
    source_revision BIGINT UNSIGNED NOT NULL,
    run_signature CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    report_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
    report_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    generated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT fk_report_run FOREIGN KEY (run_id, user_id)
        REFERENCES experiment_assistant.experiment_runs (id, user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_report_version CHECK (version > 0),
    CONSTRAINT ck_report_json CHECK (JSON_VALID(report_json) AND JSON_TYPE(report_json) = 'OBJECT' AND OCTET_LENGTH(report_json) <= 131072)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
