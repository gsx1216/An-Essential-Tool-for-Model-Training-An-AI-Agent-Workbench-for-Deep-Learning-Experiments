SELECT e.run_id FROM experiment_assistant.run_executions e
JOIN experiment_assistant.experiment_runs r ON r.id=e.run_id AND r.user_id=e.user_id
WHERE e.user_id=%s AND e.server_id=%s AND r.status NOT IN ('succeeded','failed','cancelled') LIMIT 1;
