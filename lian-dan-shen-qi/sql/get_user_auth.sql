-- 仅供服务端认证流程读取；不能原样返回给前端或打印到日志。
SELECT id, username, password_hash
FROM experiment_assistant.users
WHERE username = %s;
