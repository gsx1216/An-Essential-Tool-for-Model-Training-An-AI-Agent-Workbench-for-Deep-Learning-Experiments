SELECT s.id, s.user_id, s.title
FROM experiment_assistant.chat_sessions AS s
LEFT JOIN experiment_assistant.users AS u ON u.id = s.user_id
WHERE s.user_id IS NOT NULL
  AND u.id IS NULL
ORDER BY s.id;
