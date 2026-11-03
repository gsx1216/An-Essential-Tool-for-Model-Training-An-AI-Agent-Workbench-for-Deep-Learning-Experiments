UPDATE experiment_assistant.chat_turns
SET status = 'completed', lease_until = NULL, error_code = NULL
WHERE session_id = %s AND id = %s AND attempt_id = %s AND status = 'running';
