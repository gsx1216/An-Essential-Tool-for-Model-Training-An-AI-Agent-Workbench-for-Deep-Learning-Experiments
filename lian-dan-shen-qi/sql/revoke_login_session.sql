-- 参数：当前请求 Token 的 SHA-256 哈希；只撤销这个 Token，不批量撤销该用户其他登录。
-- 使用一个 UPDATE 同时检查有效期和撤销状态，避免先查询再修改之间状态变化。
UPDATE experiment_assistant.login_sessions
SET revoked_at = UTC_TIMESTAMP(6)
WHERE token_hash = %s
  AND revoked_at IS NULL
  AND expires_at > UTC_TIMESTAMP(6);
