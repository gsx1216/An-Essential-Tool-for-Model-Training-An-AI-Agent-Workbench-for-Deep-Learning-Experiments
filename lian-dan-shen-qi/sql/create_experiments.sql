-- 实验定义，与每次运行结果分开；重复执行不修改已有实验或其他业务记录。
CREATE TABLE IF NOT EXISTS experiment_assistant.experiments (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '服务端生成的实验 UUID',
    user_id BIGINT UNSIGNED NOT NULL COMMENT '实验所属用户',
    title VARCHAR(200) NOT NULL,
    kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT 'training 或 script；仅分类，不执行命令',
    description TEXT NOT NULL COMMENT '实验目的、材料与限制，最多一万字符',
    created_at DATETIME(6) NOT NULL COMMENT 'UTC 创建时间，由 INSERT 显式设置',
    updated_at DATETIME(6) NOT NULL COMMENT 'UTC 更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uq_experiments_id_owner (id, user_id),
    KEY idx_experiments_user_created_id (user_id, created_at DESC, id DESC),
    CONSTRAINT fk_experiments_user FOREIGN KEY (user_id)
        REFERENCES experiment_assistant.users (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_experiments_title CHECK (CHAR_LENGTH(TRIM(title)) BETWEEN 1 AND 200),
    CONSTRAINT ck_experiments_kind CHECK (kind IN ('training', 'script')),
    CONSTRAINT ck_experiments_description CHECK (CHAR_LENGTH(description) <= 10000),
    CONSTRAINT ck_experiments_times CHECK (updated_at >= created_at)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
