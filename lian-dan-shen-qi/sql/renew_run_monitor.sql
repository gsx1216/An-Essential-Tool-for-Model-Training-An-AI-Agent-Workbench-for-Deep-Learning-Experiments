UPDATE experiment_assistant.run_monitor_tasks SET lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),updated_at=UTC_TIMESTAMP(6)
 WHERE user_id=%s AND run_id=%s AND status='running' AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6);
