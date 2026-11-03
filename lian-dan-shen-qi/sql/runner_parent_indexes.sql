SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE
FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = 'experiment_assistant'
AND ((TABLE_NAME = 'experiment_runs' AND INDEX_NAME = 'uq_runs_identity_owner')
 OR (TABLE_NAME = 'server_connections' AND INDEX_NAME = 'uq_servers_identity_owner'))
ORDER BY TABLE_NAME, SEQ_IN_INDEX;
