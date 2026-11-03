UPDATE experiment_assistant.training_tasks SET deleted_at=UTC_TIMESTAMP(6)
WHERE user_id=%s AND id=%s AND deleted_at IS NULL;
