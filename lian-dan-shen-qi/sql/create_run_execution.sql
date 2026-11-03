INSERT INTO experiment_assistant.run_executions
    (run_id, server_id, user_id, script_id, job_id, launch_json, launch_digest, created_at)
SELECT %s, %s, r.user_id, %s, %s, %s, %s, UTC_TIMESTAMP(6)
FROM experiment_assistant.experiment_runs r
JOIN experiment_assistant.server_connections s ON s.user_id = r.user_id
WHERE r.user_id = %s AND r.id = %s AND s.user_id = %s AND s.id = %s AND s.deleted_at IS NULL;
