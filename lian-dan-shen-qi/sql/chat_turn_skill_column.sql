SELECT COUNT(*) FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='experiment_assistant' AND TABLE_NAME='chat_turns' AND COLUMN_NAME='skill_snapshot_json';
