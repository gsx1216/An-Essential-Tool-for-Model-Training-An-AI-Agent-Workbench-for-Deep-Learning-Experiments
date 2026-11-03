SELECT id,turn_id,run_id,advice_json,rendered_text,advice_sha256,created_at
FROM experiment_assistant.research_advice WHERE user_id=%s AND session_id=%s ORDER BY created_at DESC,id DESC LIMIT %s;
