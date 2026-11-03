SELECT id FROM experiment_assistant.code_changes WHERE user_id=%s AND origin_session_id=%s ORDER BY created_at DESC,id DESC LIMIT 1;
