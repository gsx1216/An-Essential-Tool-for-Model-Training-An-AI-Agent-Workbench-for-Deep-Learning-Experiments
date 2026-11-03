SELECT m.role, m.content
FROM experiment_assistant.chat_messages AS m
INNER JOIN experiment_assistant.chat_sessions AS s ON s.id = m.session_id
WHERE s.user_id = %s
  AND s.id = %s
ORDER BY m.seq ASC;
