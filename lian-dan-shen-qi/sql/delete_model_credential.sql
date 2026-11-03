DELETE FROM experiment_assistant.model_credentials
WHERE user_id = %s AND provider = 'deepseek';
