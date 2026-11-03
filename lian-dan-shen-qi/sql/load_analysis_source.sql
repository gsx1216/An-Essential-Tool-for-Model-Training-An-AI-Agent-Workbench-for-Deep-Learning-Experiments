SELECT e.*,t.event_id,t.id AS task_id,t.lease_token
FROM experiment_assistant.agent_tasks AS t
JOIN experiment_assistant.event_outbox AS e
 ON e.id=t.event_id AND e.user_id=t.user_id AND e.run_id=t.run_id AND e.report_version=t.report_version
JOIN experiment_assistant.analysis_preferences AS p ON p.user_id=t.user_id AND p.enabled=1
WHERE t.id=%s AND t.user_id=%s AND t.lease_token=%s AND t.status='running' AND t.lease_until>UTC_TIMESTAMP(6);
