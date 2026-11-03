SELECT id AS task_id,event_id,run_id,agent_name,report_version,status,created_at,last_error_code,COALESCE(lease_until>UTC_TIMESTAMP(6),0) AS lease_active
FROM experiment_assistant.agent_tasks WHERE user_id=%s AND run_id=%s ORDER BY created_at,id LIMIT %s OFFSET %s;
