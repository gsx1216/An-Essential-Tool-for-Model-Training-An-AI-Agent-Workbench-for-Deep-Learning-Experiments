UPDATE experiment_assistant.agent_tasks
SET status='queued',attempts_in_cycle=0,lease_token=NULL,lease_until=NULL,
 next_attempt_at=UTC_TIMESTAMP(6),last_error_code=NULL,finished_at=NULL
WHERE id=%s AND user_id=%s AND run_id=%s;
