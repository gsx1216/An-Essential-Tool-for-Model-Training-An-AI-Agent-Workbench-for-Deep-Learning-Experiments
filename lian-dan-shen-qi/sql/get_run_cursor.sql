SELECT created_at FROM experiment_assistant.experiment_runs
WHERE user_id = %s AND experiment_id = %s AND id = %s;
