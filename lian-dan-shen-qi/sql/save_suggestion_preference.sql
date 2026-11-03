INSERT INTO experiment_assistant.suggestion_preferences(user_id,enabled,updated_at)
VALUES(%s,%s,UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE enabled=%s,updated_at=UTC_TIMESTAMP(6);
