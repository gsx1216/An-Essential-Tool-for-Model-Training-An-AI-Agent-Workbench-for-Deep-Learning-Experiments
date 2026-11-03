DELETE FROM experiment_assistant.chat_messages
WHERE session_id IN (
    SELECT id
    FROM experiment_assistant.chat_sessions
    WHERE user_id = %s
      AND id = %s
);
