INSERT INTO experiment_assistant.server_connections
    (id, user_id, name, host, port, username, auth_method, host_fingerprint, work_dir, encrypted_credentials, created_at)
SELECT %s, id, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6)
FROM experiment_assistant.users WHERE id = %s;
