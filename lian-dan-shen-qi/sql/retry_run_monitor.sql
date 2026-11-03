UPDATE experiment_assistant.run_monitor_tasks SET status='scheduled',failure_count=0,last_error_code=NULL,
 lease_token=NULL,lease_until=NULL,next_poll_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6),completed_at=NULL
 WHERE user_id=%s AND run_id=%s;
