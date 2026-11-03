UPDATE experiment_assistant.run_monitor_tasks SET status='running',attempts=attempts+1,
 lease_token=%s,lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),last_attempt_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6)
 WHERE user_id=%s AND run_id=%s;
