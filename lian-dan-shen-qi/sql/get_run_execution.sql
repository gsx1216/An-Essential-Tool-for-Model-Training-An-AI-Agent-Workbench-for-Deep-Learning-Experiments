SELECT e.run_id, e.server_id, e.user_id, e.script_id, e.job_id, e.launch_json, e.launch_digest,
       r.status, r.exit_code, r.error_code, e.created_at, e.checked_at
FROM experiment_assistant.run_executions e
JOIN experiment_assistant.experiment_runs r ON r.id = e.run_id
WHERE r.user_id = %s AND r.id = %s;
