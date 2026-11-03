SELECT e.*,t.event_id,t.id AS task_id,t.lease_token,upstream.id AS analysis_task_id,upstream_result.content_json AS analysis_json,upstream_result.content_digest AS analysis_digest
FROM experiment_assistant.agent_tasks AS t
JOIN experiment_assistant.event_outbox AS e
 ON e.id=t.event_id AND e.user_id=t.user_id AND e.run_id=t.run_id AND e.report_version=t.report_version
JOIN experiment_assistant.suggestion_preferences AS p ON p.user_id=t.user_id AND p.enabled=1
JOIN experiment_assistant.agent_tasks AS upstream ON upstream.event_id=t.event_id
 AND upstream.agent_name='analysis' AND upstream.status='succeeded'
JOIN experiment_assistant.analysis_results AS upstream_result ON upstream_result.task_id=upstream.id
WHERE t.agent_name='suggestion' AND t.id=%s AND t.user_id=%s AND t.lease_token=%s AND t.status='running' AND t.lease_until>UTC_TIMESTAMP(6);
