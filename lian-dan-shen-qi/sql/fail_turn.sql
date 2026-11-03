UPDATE experiment_assistant.chat_turns
SET status = 'failed', lease_until = NULL, error_code = %s
WHERE session_id = %s AND id = %s AND attempt_id = %s AND status = 'running';
