-- 登录记录：每次成功登录可以产生一条记录，与 chat_sessions 聊天会话分开。
-- 原始 Token 只发给客户端；这里只保存 SHA-256 哈希。
CREATE TABLE IF NOT EXISTS `experiment_assistant`.`login_sessions` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `user_id` BIGINT UNSIGNED NOT NULL,
    `token_hash` CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `created_at` DATETIME(6) NOT NULL COMMENT 'UTC 签发时间',
    `expires_at` DATETIME(6) NOT NULL COMMENT 'UTC 到期时间，默认签发后 3 天',
    `revoked_at` DATETIME(6) NULL DEFAULT NULL COMMENT 'UTC 撤销时间，后续退出登录时设置',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_login_sessions_token_hash` (`token_hash`),
    KEY `idx_login_sessions_user` (`user_id`),
    KEY `idx_login_sessions_expires` (`expires_at`),
    CONSTRAINT `fk_login_sessions_user`
        FOREIGN KEY (`user_id`) REFERENCES `experiment_assistant`.`users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT `chk_login_sessions_expiry` CHECK (`expires_at` > `created_at`)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    COMMENT='登录凭证记录；此表的 ID 不是登录凭证';
