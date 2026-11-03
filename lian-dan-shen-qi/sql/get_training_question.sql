SELECT m.content FROM experiment_assistant.chat_turns t
 JOIN experiment_assistant.chat_messages m ON m.session_id=t.session_id AND m.seq=t.user_seq AND m.role='human'
 WHERE t.session_id=%s AND t.id=%s;
