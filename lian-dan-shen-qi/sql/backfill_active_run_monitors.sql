INSERT INTO experiment_assistant.run_monitor_tasks(run_id,user_id,next_poll_at,created_at,updated_at)
 SELECT e.run_id,e.user_id,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)
 FROM experiment_assistant.run_executions e JOIN experiment_assistant.experiment_runs r ON r.id=e.run_id AND r.user_id=e.user_id
 LEFT JOIN experiment_assistant.run_monitor_tasks m ON m.run_id=e.run_id
 WHERE r.status IN ('queued','running') AND m.run_id IS NULL
 LIMIT 1000
 ON DUPLICATE KEY UPDATE run_id=run_monitor_tasks.run_id;
