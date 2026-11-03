CREATE TABLE IF NOT EXISTS experiment_assistant.run_evaluation_specs (
    run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    spec_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
    spec_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT fk_run_evaluation_specs_owner FOREIGN KEY (run_id, user_id)
        REFERENCES experiment_assistant.experiment_runs (id, user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_run_evaluation_specs_json CHECK (JSON_VALID(spec_json) AND JSON_TYPE(spec_json) = 'OBJECT' AND OCTET_LENGTH(spec_json) <= 32768)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
