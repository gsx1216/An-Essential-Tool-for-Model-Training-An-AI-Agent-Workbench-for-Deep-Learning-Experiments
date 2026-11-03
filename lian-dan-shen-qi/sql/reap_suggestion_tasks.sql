UPDATE experiment_assistant.agent_tasks
SET status='attention',lease_token=NULL,lease_until=NULL,last_error_code='ATTEMPTS_EXHAUSTED',finished_at=UTC_TIMESTAMP(6)
WHERE agent_name='suggestion' AND attempts_in_cycle>=%s
 AND ((status='running' AND lease_until<=UTC_TIMESTAMP(6)) OR status='queued');
