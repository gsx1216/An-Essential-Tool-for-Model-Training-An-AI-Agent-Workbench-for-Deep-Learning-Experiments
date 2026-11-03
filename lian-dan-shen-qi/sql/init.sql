-- 实验助手：MySQL 初始化脚本。
-- Python 启动时连接 MySQL 服务，再读取并执行此文件。
-- 会话 ID 由 Python 的 uuid4().hex 生成，长度为 32 个字符。
-- IF NOT EXISTS 用于重复初始化；已有表的结构调整需要另外编写迁移。

-- 1. 创建项目数据库。
CREATE DATABASE IF NOT EXISTS `experiment_assistant`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- 2. 选中项目数据库。
USE `experiment_assistant`;

-- 3. 用户表：一行对应一个应用用户，当前先保存用户标识。
CREATE TABLE IF NOT EXISTS `users` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户内部编号',
    `username` VARCHAR(64) NOT NULL COMMENT '用户名',
    `password_hash` VARCHAR(255) NULL COMMENT 'Argon2id 登录密码哈希；NULL 表示未设置',
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_users_username` (`username`)
) ENGINE=InnoDB
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci
    COMMENT='应用用户，登录验证功能后续补充';

-- 4. 会话表：一行对应一个聊天会话。
CREATE TABLE IF NOT EXISTS `chat_sessions` (
    `id` CHAR(32) NOT NULL COMMENT '会话 UUID，使用 uuid4().hex 格式',
    `user_id` BIGINT UNSIGNED NULL COMMENT '所属用户 ID，接入登录功能后设置',
    `title` VARCHAR(200) NOT NULL DEFAULT '新聊天' COMMENT '聊天列表中显示的标题',
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6)
        COMMENT '会话更新时间，新增消息时需由仓储层同步更新会话行',
    PRIMARY KEY (`id`),
    KEY `idx_chat_sessions_user_updated` (`user_id`, `updated_at`),
    CONSTRAINT `fk_chat_sessions_user`
        FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci
    COMMENT='聊天会话';

-- 5. 消息表：一行对应一条用户消息、模型回复或系统消息。
CREATE TABLE IF NOT EXISTS `chat_messages` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '消息内部编号',
    `session_id` CHAR(32) NOT NULL COMMENT '所属会话 ID',
    `seq` BIGINT UNSIGNED NOT NULL COMMENT '会话内消息顺序，从 1 开始递增',
    `role` VARCHAR(16) NOT NULL COMMENT '与当前 CLI 一致：system、human、ai',
    `content` LONGTEXT NOT NULL COMMENT '消息正文',
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_chat_messages_session_seq` (`session_id`, `seq`),
    CONSTRAINT `fk_chat_messages_session`
        FOREIGN KEY (`session_id`) REFERENCES `chat_sessions` (`id`)
) ENGINE=InnoDB
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci
    COMMENT='聊天消息';

-- 外键保证消息必须属于一个已存在的会话。
-- 后续读取历史时使用 session_id 筛选，并按 seq 排序。
-- users 外键同时保证非空的 user_id 必须存在于 users 表。
-- user_id 保留 NULL，以兼容早期未关联用户的测试记录；新建会话由 Python 强制传入有效用户。
-- 已有数据库由 app.database 检查并补加外键，CREATE TABLE IF NOT EXISTS 本身不会修改旧表。
