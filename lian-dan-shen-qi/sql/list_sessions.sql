SELECT id, title
FROM experiment_assistant.chat_sessions
WHERE user_id = %s
ORDER BY updated_at DESC, id DESC;
