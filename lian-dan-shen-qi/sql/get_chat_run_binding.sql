SELECT b.run_id FROM experiment_assistant.chat_run_bindings b
JOIN experiment_assistant.experiment_runs r ON r.id=b.run_id AND r.user_id=b.user_id
WHERE b.user_id=%s AND b.session_id=%s;
