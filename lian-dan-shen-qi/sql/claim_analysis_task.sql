UPDATE experiment_assistant.agent_tasks
SET status='running',attempts=attempts+1,attempts_in_cycle=attempts_in_cycle+1,
 lease_token=%s,lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),
 started_at=COALESCE(started_at,UTC_TIMESTAMP(6)),finished_at=NULL,last_error_code=NULL,next_attempt_at=NULL
WHERE id=%s;
