SELECT id, status FROM experiment_assistant.experiment_runs WHERE user_id = %s AND id = %s FOR UPDATE;
