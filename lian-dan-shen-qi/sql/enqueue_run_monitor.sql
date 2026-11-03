INSERT INTO experiment_assistant.run_monitor_tasks(run_id,user_id,next_poll_at,created_at,updated_at)
 SELECT run_id,user_id,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)
 FROM experiment_assistant.run_executions WHERE user_id=%s AND run_id=%s
 ON DUPLICATE KEY UPDATE run_id=run_monitor_tasks.run_id;
