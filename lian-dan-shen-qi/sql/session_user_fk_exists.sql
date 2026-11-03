SELECT 1
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = 'experiment_assistant'
  AND TABLE_NAME = 'chat_sessions'
  AND COLUMN_NAME = 'user_id'
  AND REFERENCED_TABLE_SCHEMA = 'experiment_assistant'
  AND REFERENCED_TABLE_NAME = 'users'
  AND REFERENCED_COLUMN_NAME = 'id'
LIMIT 1;
