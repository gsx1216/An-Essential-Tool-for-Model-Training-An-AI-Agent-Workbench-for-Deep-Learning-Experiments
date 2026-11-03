SELECT c.id FROM experiment_assistant.code_changes c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s
AND c.status IN ('planning','applying') AND c.lease_until>UTC_TIMESTAMP(6) LIMIT 1 FOR UPDATE;
