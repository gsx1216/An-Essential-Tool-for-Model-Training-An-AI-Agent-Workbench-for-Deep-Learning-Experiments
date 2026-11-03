UPDATE experiment_assistant.event_outbox SET status='received',failure_count=0,last_error_code=NULL,
 sent_at=COALESCE(sent_at,UTC_TIMESTAMP(6)),lease_token=NULL,lease_until=NULL
WHERE id=%s;
