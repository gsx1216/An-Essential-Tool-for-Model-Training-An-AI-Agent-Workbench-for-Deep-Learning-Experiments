SELECT role, content FROM experiment_assistant.chat_messages
WHERE session_id = %s AND seq = %s
FOR UPDATE;
