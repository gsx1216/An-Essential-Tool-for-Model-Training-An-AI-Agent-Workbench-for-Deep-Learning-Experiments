UPDATE experiment_assistant.chat_sessions
SET updated_at = CURRENT_TIMESTAMP(6)
WHERE id = %s;
