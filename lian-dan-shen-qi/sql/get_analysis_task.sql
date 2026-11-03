SELECT t.*,t.id AS task_id,(t.lease_until>UTC_TIMESTAMP(6)) AS lease_active
FROM experiment_assistant.agent_tasks AS t
WHERE t.user_id=%s AND t.run_id=%s AND t.id=%s AND t.agent_name='analysis';
