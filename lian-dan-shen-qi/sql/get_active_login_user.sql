-- 参数：原始 Token 的 SHA-256 小写十六进制哈希。
-- DATETIME 按 UTC 保存，比较也使用 UTC，不依赖连接的时区。
-- 只返回公开身份；未知、到期、已撤销或关联用户不存在，均查不到结果。
SELECT users.id, users.username
FROM experiment_assistant.login_sessions AS sessions
INNER JOIN experiment_assistant.users AS users
    ON users.id = sessions.user_id
WHERE sessions.token_hash = %s
  AND sessions.revoked_at IS NULL
  AND sessions.expires_at > UTC_TIMESTAMP(6)
LIMIT 1;
