SELECT run_id,user_id FROM experiment_assistant.run_monitor_tasks
 WHERE (status='scheduled' AND next_poll_at<=UTC_TIMESTAMP(6))
 OR (status='running' AND lease_until<=UTC_TIMESTAMP(6))
 ORDER BY next_poll_at,run_id LIMIT 1 FOR UPDATE SKIP LOCKED;
