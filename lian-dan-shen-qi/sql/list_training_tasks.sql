SELECT id AS task_id,name,script_id FROM experiment_assistant.training_tasks
 WHERE user_id=%s AND deleted_at IS NULL AND LOCATE(%s,name)>0 ORDER BY id LIMIT %s OFFSET %s;
