SELECT e.redis_message_id,(e.status='sending' AND e.lease_until>UTC_TIMESTAMP(6)) AS publishing
FROM experiment_assistant.event_outbox e
JOIN experiment_assistant.experiment_runs r ON r.id=e.run_id AND r.user_id=e.user_id
WHERE r.user_id=%s AND r.{target}=%s FOR UPDATE;
