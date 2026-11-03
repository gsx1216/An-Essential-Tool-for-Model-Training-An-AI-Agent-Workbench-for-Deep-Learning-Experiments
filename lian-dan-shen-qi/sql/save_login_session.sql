-- 参数顺序：user_id、token_hash、转换为 UTC 后去掉时区标记的 expires_at。
INSERT INTO experiment_assistant.login_sessions
    (user_id, token_hash, created_at, expires_at)
VALUES (%s, %s, UTC_TIMESTAMP(6), %s);
