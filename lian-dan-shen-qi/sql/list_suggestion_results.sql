SELECT upstream.id AS analysis_task_id,upstream_result.content_json AS analysis_json,upstream_result.content_digest AS analysis_digest,a.task_id,a.content_json,a.content_digest,a.model_name,a.created_at,
 t.run_id,t.event_id,t.report_version,e.report_digest,e.report_json,e.event_type,
 e.id,r.result_version AS current_report_version,r.status AS current_run_status
FROM experiment_assistant.suggestion_results AS a
JOIN experiment_assistant.agent_tasks AS t ON t.id=a.task_id
JOIN experiment_assistant.event_outbox AS e ON e.id=t.event_id
JOIN experiment_assistant.experiment_runs AS r ON r.id=t.run_id AND r.user_id=t.user_id
JOIN experiment_assistant.agent_tasks AS upstream ON upstream.event_id=t.event_id
 AND upstream.agent_name='analysis' AND upstream.status='succeeded'
JOIN experiment_assistant.analysis_results AS upstream_result ON upstream_result.task_id=upstream.id
WHERE t.agent_name='suggestion' AND t.user_id=%s AND t.run_id=%s AND t.status='succeeded'
ORDER BY t.report_version DESC,a.created_at DESC,a.task_id DESC
LIMIT %s OFFSET %s;
