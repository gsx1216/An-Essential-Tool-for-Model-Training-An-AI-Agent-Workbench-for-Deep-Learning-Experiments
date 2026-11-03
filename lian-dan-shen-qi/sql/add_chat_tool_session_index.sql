ALTER TABLE experiment_assistant.chat_sessions ADD UNIQUE INDEX uq_chat_session_identity_owner (id,user_id);
