SELECT t.status, q.role, q.content, a.content
FROM experiment_assistant.chat_sessions AS s
LEFT JOIN experiment_assistant.chat_turns AS t
    ON t.session_id = s.id AND t.id = %s
LEFT JOIN experiment_assistant.chat_messages AS q
    ON q.session_id = t.session_id AND q.seq = t.user_seq
LEFT JOIN experiment_assistant.chat_messages AS a
    ON a.session_id = t.session_id AND a.seq = t.user_seq + 1 AND a.role = 'ai'
WHERE s.user_id = %s AND s.id = %s;
