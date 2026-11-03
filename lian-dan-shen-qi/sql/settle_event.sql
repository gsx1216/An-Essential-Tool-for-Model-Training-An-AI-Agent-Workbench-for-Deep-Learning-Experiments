UPDATE experiment_assistant.event_outbox SET status=%s,failure_count=%s,last_error_code=%s,
 next_attempt_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),redis_message_id=COALESCE(%s,redis_message_id),
 sent_at=CASE WHEN %s='sent' THEN UTC_TIMESTAMP(6) ELSE sent_at END,lease_token=NULL,lease_until=NULL
WHERE id=%s AND status='sending' AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6);
