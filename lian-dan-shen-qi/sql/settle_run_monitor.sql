UPDATE experiment_assistant.run_monitor_tasks SET status=%s,
 next_poll_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),failure_count=%s,last_error_code=%s,
 report_version=COALESCE(%s,report_version),last_success_at=CASE WHEN %s THEN UTC_TIMESTAMP(6) ELSE last_success_at END,
 completed_at=CASE WHEN %s='completed' THEN UTC_TIMESTAMP(6) ELSE NULL END,
 lease_token=NULL,lease_until=NULL,updated_at=UTC_TIMESTAMP(6)
 WHERE user_id=%s AND run_id=%s AND status='running' AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6);
