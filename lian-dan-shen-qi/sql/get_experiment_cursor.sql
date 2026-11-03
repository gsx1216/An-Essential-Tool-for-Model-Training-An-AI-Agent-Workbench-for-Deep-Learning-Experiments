SELECT created_at
FROM experiment_assistant.experiments
WHERE user_id = %s AND id = %s;
