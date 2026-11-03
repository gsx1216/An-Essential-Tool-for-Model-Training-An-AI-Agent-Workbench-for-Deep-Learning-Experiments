UPDATE experiment_assistant.run_executions e
JOIN experiment_assistant.experiment_runs r ON r.id = e.run_id
SET e.checked_at = UTC_TIMESTAMP(6) WHERE r.user_id = %s AND r.id = %s;
