SELECT r.id,r.status FROM experiment_assistant.experiment_runs r
WHERE r.user_id=%s AND r.{target}=%s ORDER BY r.id FOR UPDATE;
