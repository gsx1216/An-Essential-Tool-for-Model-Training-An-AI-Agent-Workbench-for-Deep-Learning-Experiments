SELECT id, status, user_seq, attempt_id,
       COALESCE(lease_until > CURRENT_TIMESTAMP(6), 0), error_code
FROM experiment_assistant.chat_turns
WHERE session_id = %s AND status <> 'completed'
ORDER BY user_seq DESC LIMIT 1;
