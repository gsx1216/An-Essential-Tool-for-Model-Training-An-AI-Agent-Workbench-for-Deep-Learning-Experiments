SELECT id AS experiment_id, title, kind, created_at, updated_at
FROM experiment_assistant.experiments
WHERE user_id = %s
  AND (created_at < %s OR (created_at = %s AND id < %s))
ORDER BY created_at DESC, id DESC
LIMIT %s;
