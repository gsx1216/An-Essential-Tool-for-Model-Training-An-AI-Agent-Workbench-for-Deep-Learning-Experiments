SELECT id AS server_id, name, host, port, username, auth_method, host_fingerprint, work_dir, created_at
FROM experiment_assistant.server_connections
WHERE user_id = %s AND deleted_at IS NULL AND (created_at < %s OR (created_at = %s AND id < %s))
ORDER BY created_at DESC, id DESC LIMIT %s;
