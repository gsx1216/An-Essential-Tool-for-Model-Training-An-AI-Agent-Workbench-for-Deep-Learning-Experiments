ALTER TABLE experiment_assistant.training_tasks
 ADD COLUMN deleted_at DATETIME(6) NULL,
 ADD COLUMN active_name VARCHAR(80) GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN name ELSE NULL END) STORED,
 DROP INDEX uq_training_task_name,
 ADD UNIQUE KEY uq_training_task_name(user_id,active_name);
