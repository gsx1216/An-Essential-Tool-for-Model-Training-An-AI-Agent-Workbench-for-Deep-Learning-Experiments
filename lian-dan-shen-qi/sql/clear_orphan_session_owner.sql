UPDATE experiment_assistant.chat_sessions AS s
LEFT JOIN experiment_assistant.users AS u ON u.id = s.user_id
SET s.user_id = NULL
WHERE s.id = %s
  AND s.user_id = %s
  AND u.id IS NULL;
