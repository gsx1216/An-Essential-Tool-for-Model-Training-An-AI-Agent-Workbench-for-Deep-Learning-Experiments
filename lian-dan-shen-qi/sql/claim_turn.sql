UPDATE experiment_assistant.chat_turns
SET status = 'running', attempt_id = %s,
    lease_until = TIMESTAMPADD(SECOND, %s, CURRENT_TIMESTAMP(6)), error_code = NULL
WHERE session_id = %s AND id = %s;
