UPDATE experiment_assistant.event_outbox SET status='pending',failure_count=0,last_error_code=NULL,
 next_attempt_at=UTC_TIMESTAMP(6),lease_token=NULL,lease_until=NULL
WHERE id=%s AND user_id=%s AND run_id=%s;
