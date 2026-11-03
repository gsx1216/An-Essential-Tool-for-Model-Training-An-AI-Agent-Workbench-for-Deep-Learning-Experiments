SELECT m.role,SUBSTRING(m.content,%s,800),CHAR_LENGTH(m.content)
FROM experiment_assistant.chat_messages m
JOIN experiment_assistant.chat_sessions s ON s.id=m.session_id
WHERE s.user_id=%s AND s.id=%s AND m.seq=%s AND m.seq<%s;
