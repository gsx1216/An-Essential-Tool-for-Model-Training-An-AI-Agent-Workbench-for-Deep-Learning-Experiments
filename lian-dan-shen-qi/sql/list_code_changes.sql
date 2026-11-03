SELECT c.*,(c.lease_until>UTC_TIMESTAMP(6)) AS lease_active FROM experiment_assistant.code_changes AS c WHERE c.user_id=%s AND c.run_id=%s ORDER BY c.created_at DESC,c.id DESC LIMIT %s OFFSET %s;
