-- 当前只支持 DeepSeek；再次保存更新同一条记录，不创建额外副本。
-- MySQL 8.0.19+ 支持 VALUES 后的行别名；无需使用已弃用的 VALUES(column)。
INSERT INTO experiment_assistant.model_credentials
    (user_id, provider, encrypted_key, created_at, updated_at)
VALUES (%s, 'deepseek', %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)) AS incoming
ON DUPLICATE KEY UPDATE
    encrypted_key = incoming.encrypted_key,
    updated_at = UTC_TIMESTAMP(6);
