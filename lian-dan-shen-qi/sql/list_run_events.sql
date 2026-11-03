SELECT id AS event_id,run_id,event_type,report_version,status,attempts,failure_count,last_error_code,
 next_attempt_at,sent_at,created_at,(status='sending' AND lease_until>UTC_TIMESTAMP(6)) AS lease_active
FROM experiment_assistant.event_outbox WHERE user_id=%s AND run_id=%s ORDER BY created_at,id LIMIT %s OFFSET %s;
