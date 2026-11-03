SELECT t.* FROM experiment_assistant.agent_tasks AS t
JOIN experiment_assistant.analysis_preferences AS p ON p.user_id=t.user_id AND p.enabled=1
WHERE t.agent_name='analysis' AND t.attempts_in_cycle<%s
 AND ((t.status='queued' AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=UTC_TIMESTAMP(6)))
   OR (t.status='running' AND t.lease_until<=UTC_TIMESTAMP(6)))
ORDER BY t.created_at,t.id LIMIT 1 FOR UPDATE SKIP LOCKED;
