UPDATE experiment_assistant.agent_tasks AS t
JOIN experiment_assistant.suggestion_preferences AS p ON p.user_id=t.user_id AND p.enabled=1
SET t.lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6))
WHERE t.id=%s AND t.user_id=%s AND t.lease_token=%s AND t.status='running' AND t.lease_until>UTC_TIMESTAMP(6);
