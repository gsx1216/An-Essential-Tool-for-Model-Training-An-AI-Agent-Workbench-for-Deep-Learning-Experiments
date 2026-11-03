SELECT m.content
FROM experiment_assistant.chat_turns AS t
INNER JOIN experiment_assistant.chat_sessions AS s ON s.id = t.session_id
INNER JOIN experiment_assistant.chat_messages AS m
    ON m.session_id = t.session_id AND m.seq = t.user_seq + 1 AND m.role = 'ai'
WHERE s.user_id = %s AND s.id = %s AND t.id = %s AND t.status = 'completed';
