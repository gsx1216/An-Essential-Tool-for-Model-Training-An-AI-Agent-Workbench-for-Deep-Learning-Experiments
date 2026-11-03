-- 已有用户保留 NULL，不分配默认密码、不修改既有用户 ID。
ALTER TABLE experiment_assistant.users
ADD COLUMN password_hash VARCHAR(255) NULL
COMMENT '登录密码的 Argon2id 哈希；NULL 表示尚未设置登录密码'
AFTER username;
