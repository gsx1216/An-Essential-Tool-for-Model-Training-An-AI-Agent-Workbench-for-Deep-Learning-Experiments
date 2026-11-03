SELECT v.run_id FROM experiment_assistant.revision_runs v
JOIN experiment_assistant.experiment_runs r ON r.id=v.original_run_id AND r.user_id=v.user_id
JOIN experiment_assistant.experiment_runs child ON child.id=v.run_id AND child.user_id=v.user_id
WHERE r.user_id=%s AND r.{target}=%s AND child.{target}<>%s LIMIT 1;
