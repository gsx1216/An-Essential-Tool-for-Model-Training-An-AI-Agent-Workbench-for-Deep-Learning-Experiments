ALTER TABLE experiment_assistant.server_connections ADD UNIQUE KEY uq_servers_identity_owner (id, user_id);
