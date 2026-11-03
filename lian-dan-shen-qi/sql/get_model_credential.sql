-- 仅供内部使用，公开接口不能直接返回密文。
SELECT encrypted_key
FROM experiment_assistant.model_credentials
WHERE user_id = %s AND provider = 'deepseek';
