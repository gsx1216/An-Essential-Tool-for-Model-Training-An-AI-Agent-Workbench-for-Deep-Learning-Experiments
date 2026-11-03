UPDATE experiment_assistant.experiment_runs SET result_version = %s, result_ref = %s, updated_at = UTC_TIMESTAMP(6)
WHERE user_id = %s AND id = %s;
