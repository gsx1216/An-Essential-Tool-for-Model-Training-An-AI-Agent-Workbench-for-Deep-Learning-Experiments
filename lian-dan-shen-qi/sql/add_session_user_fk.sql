ALTER TABLE experiment_assistant.chat_sessions
ADD CONSTRAINT fk_chat_sessions_user
FOREIGN KEY (user_id) REFERENCES experiment_assistant.users (id)
ON DELETE RESTRICT ON UPDATE RESTRICT;
