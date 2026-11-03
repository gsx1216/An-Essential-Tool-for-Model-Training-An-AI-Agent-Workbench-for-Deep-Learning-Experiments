CREATE TABLE IF NOT EXISTS experiment_assistant.personal_skills (
    skill_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    snapshot_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (skill_id),
    KEY idx_personal_skill_user (user_id, created_at, skill_id),
    CONSTRAINT fk_personal_skill_user FOREIGN KEY (user_id)
        REFERENCES experiment_assistant.users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
