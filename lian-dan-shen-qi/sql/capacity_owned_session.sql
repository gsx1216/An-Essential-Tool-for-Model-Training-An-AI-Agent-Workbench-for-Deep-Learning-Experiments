SELECT 1 AS found
FROM experiment_assistant.chat_sessions
WHERE user_id = %s AND id = %s;
