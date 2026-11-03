SELECT COUNT(*) FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='experiment_assistant' AND TABLE_NAME='training_tasks' AND COLUMN_NAME='deleted_at';
