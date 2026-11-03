SELECT t.skill_snapshot_json FROM experiment_assistant.chat_sessions s
JOIN experiment_assistant.chat_turns t ON t.session_id=s.id
WHERE s.user_id=%s AND s.id=%s AND t.id=%s
AND (%s IS NULL OR (t.attempt_id=%s AND t.status='running' AND t.lease_until>UTC_TIMESTAMP(6)));
