UPDATE experiment_assistant.experiment_runs SET status = 'queued', updated_at = UTC_TIMESTAMP(6)
WHERE user_id = %s AND id = %s AND status = 'created';
