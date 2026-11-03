-- 每个用户、每个供应商一条个人凭据；只保存加密密文。
CREATE TABLE IF NOT EXISTS `experiment_assistant`.`model_credentials` (
    `user_id` BIGINT UNSIGNED NOT NULL,
    `provider` VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `encrypted_key` VARCHAR(8192) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`user_id`, `provider`),
    CONSTRAINT `fk_model_credentials_user`
        FOREIGN KEY (`user_id`) REFERENCES `experiment_assistant`.`users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT `chk_model_credentials_key_length`
        CHECK (CHAR_LENGTH(`encrypted_key`) BETWEEN 100 AND 8192)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    COMMENT='个人模型凭据；主密钥由服务器另行保管';
