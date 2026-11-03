SELECT m.seq,m.role,SUBSTRING(m.content,GREATEST(1,LOCATE(%s,m.content)-120),320)
FROM experiment_assistant.chat_messages m
JOIN experiment_assistant.chat_sessions s ON s.id=m.session_id
WHERE s.user_id=%s AND s.id=%s AND m.seq<%s AND LOCATE(%s,m.content)>0
ORDER BY m.seq DESC LIMIT %s;
