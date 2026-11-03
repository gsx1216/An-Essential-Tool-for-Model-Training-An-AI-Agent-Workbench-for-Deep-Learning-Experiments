CREATE TABLE IF NOT EXISTS experiment_assistant.experiment_runs (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    experiment_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    request_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '同一用户创建运行的幂等键',
    input_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    experiment_title VARCHAR(200) NOT NULL COMMENT '创建运行时的定义快照',
    experiment_kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    experiment_description TEXT NOT NULL,
    parameters_json TEXT COLLATE utf8mb4_bin NOT NULL COMMENT '规范 JSON 文本快照；保留精确序列化结果',
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'created',
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    exit_code INT NULL,
    error_code VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    log_ref VARCHAR(255) NULL COMMENT '后端受控日志引用，非客户端路径',
    result_ref VARCHAR(255) NULL,
    result_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_runs_identity_owner (id, user_id),
    UNIQUE KEY uq_runs_owner_request (user_id, request_id),
    KEY idx_runs_experiment_owner_created (experiment_id, user_id, created_at DESC, id DESC),
    KEY idx_runs_owner_kind_created (user_id, experiment_kind, created_at DESC, id DESC),
    KEY idx_runs_owner_kind_status_created (user_id, experiment_kind, status, created_at DESC, id DESC),
    CONSTRAINT fk_runs_experiment_owner FOREIGN KEY (experiment_id, user_id)
        REFERENCES experiment_assistant.experiments (id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_runs_kind CHECK (experiment_kind IN ('training', 'script')),
    CONSTRAINT ck_runs_status CHECK (status IN ('created', 'queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CONSTRAINT ck_runs_params_valid CHECK (JSON_VALID(parameters_json)),
    CONSTRAINT ck_runs_params_object CHECK (JSON_TYPE(parameters_json) = 'OBJECT'),
    CONSTRAINT ck_runs_params_size CHECK (OCTET_LENGTH(parameters_json) <= 32768),
    CONSTRAINT ck_runs_times CHECK (updated_at >= created_at
        AND (started_at IS NULL OR started_at >= created_at)
        AND (finished_at IS NULL OR finished_at >= created_at)
        AND (started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at)),
    CONSTRAINT ck_runs_state_times CHECK (
        (status IN ('created', 'queued') AND started_at IS NULL AND finished_at IS NULL AND exit_code IS NULL)
        OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND exit_code IS NULL)
        OR (status IN ('failed', 'cancelled') AND finished_at IS NOT NULL)
        OR (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL
            AND exit_code IS NOT NULL AND exit_code = 0)),
    CONSTRAINT ck_runs_result CHECK (
        (result_version = 0 AND result_ref IS NULL)
        OR (result_version > 0 AND result_ref IS NOT NULL AND CHAR_LENGTH(result_ref) > 0))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
