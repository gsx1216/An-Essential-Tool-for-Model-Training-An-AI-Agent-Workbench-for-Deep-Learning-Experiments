SELECT p.*,(p.lease_until>UTC_TIMESTAMP(6)) AS lease_active FROM experiment_assistant.papers p WHERE p.user_id=%s AND p.id=%s;
