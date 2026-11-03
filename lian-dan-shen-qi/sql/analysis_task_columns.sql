SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='experiment_assistant' AND TABLE_NAME='agent_tasks'
  AND COLUMN_NAME IN ('attempts','attempts_in_cycle','lease_token','lease_until',
                      'next_attempt_at','last_error_code','started_at','finished_at');
