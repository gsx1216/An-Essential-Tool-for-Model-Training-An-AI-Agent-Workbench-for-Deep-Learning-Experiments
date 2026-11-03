SELECT id AS task_id,name,experiment_id,server_id,script_id,configuration_json,created_at
 FROM experiment_assistant.training_tasks WHERE user_id=%s AND id=%s AND deleted_at IS NULL;
