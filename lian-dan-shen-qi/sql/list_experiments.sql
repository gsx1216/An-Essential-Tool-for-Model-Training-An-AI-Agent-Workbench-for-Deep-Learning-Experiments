SELECT id AS experiment_id, title, kind, created_at, updated_at
FROM experiment_assistant.experiments
WHERE user_id = %s
ORDER BY created_at DESC, id DESC
LIMIT %s;
