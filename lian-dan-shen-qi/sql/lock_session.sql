SELECT id, title
FROM experiment_assistant.chat_sessions
WHERE user_id = %s
  AND id = %s
FOR UPDATE;
