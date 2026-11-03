SELECT *,COALESCE(lease_until>UTC_TIMESTAMP(6),0) AS lease_active
 FROM experiment_assistant.run_monitor_tasks WHERE user_id=%s AND run_id=%s;
