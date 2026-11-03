ALTER TABLE experiment_assistant.server_connections ADD CONSTRAINT ck_servers_secret
CHECK ((deleted_at IS NULL AND OCTET_LENGTH(encrypted_credentials) BETWEEN 100 AND 131072)
    OR (deleted_at IS NOT NULL AND encrypted_credentials=''));
