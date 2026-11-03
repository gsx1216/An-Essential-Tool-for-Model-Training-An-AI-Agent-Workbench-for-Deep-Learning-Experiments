SELECT * FROM experiment_assistant.revision_runs WHERE user_id=%s AND change_id=%s ORDER BY created_at DESC,run_id DESC LIMIT %s;
