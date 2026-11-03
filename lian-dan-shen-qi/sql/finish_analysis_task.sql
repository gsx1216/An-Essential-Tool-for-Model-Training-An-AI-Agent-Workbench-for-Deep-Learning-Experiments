UPDATE experiment_assistant.agent_tasks
SET status=%s,lease_token=NULL,lease_until=NULL,finished_at=UTC_TIMESTAMP(6),last_error_code=%s
WHERE id=%s AND user_id=%s AND lease_token=%s AND status='running' AND lease_until>UTC_TIMESTAMP(6);
