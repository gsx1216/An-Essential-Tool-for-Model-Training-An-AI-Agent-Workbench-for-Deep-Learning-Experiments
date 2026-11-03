SELECT seq
FROM experiment_assistant.chat_messages
WHERE session_id = %s
ORDER BY seq DESC
LIMIT 1
FOR UPDATE;
