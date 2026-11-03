DELETE p FROM experiment_assistant.chat_training_plans p
JOIN experiment_assistant.training_tasks t ON t.id=p.task_id AND t.user_id=p.user_id
WHERE t.user_id=%s AND t.experiment_id=%s;

DELETE FROM experiment_assistant.training_tasks WHERE user_id=%s AND experiment_id=%s;

DELETE FROM experiment_assistant.experiment_evaluation_specs WHERE user_id=%s AND experiment_id=%s;

DELETE FROM experiment_assistant.experiments WHERE user_id=%s AND id=%s;
