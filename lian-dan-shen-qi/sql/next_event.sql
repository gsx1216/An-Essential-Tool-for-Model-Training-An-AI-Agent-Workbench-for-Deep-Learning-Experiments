SELECT e.id FROM experiment_assistant.event_outbox e
WHERE (e.status='pending' AND e.next_attempt_at<=UTC_TIMESTAMP(6))
 OR (e.status='sending' AND e.lease_until<=UTC_TIMESTAMP(6))
 OR (e.status='sent' AND e.next_attempt_at<=UTC_TIMESTAMP(6)
     AND NOT EXISTS (SELECT 1 FROM experiment_assistant.agent_tasks t WHERE t.event_id=e.id AND t.agent_name='analysis'))
ORDER BY e.next_attempt_at,e.id LIMIT 1 FOR UPDATE SKIP LOCKED;
