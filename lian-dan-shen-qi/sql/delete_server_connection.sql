UPDATE experiment_assistant.server_connections SET deleted_at=UTC_TIMESTAMP(6),encrypted_credentials=''
WHERE user_id=%s AND id=%s AND deleted_at IS NULL;
