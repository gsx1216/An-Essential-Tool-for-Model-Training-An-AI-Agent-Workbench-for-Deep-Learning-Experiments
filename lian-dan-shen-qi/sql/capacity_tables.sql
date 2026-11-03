SELECT TABLE_NAME AS table_name, ENGINE AS engine,
       TABLE_ROWS AS estimated_rows, DATA_LENGTH AS data_bytes,
       INDEX_LENGTH AS index_bytes
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
  AND TABLE_NAME IN ('users', 'login_sessions', 'chat_sessions', 'chat_messages',
                     'chat_turns', 'model_credentials', 'chat_context_summaries', 'experiments', 'experiment_runs', 'server_connections', 'run_executions', 'run_metric_streams', 'run_metric_events', 'run_reports', 'experiment_evaluation_specs', 'run_evaluation_specs', 'chat_run_bindings', 'chat_tool_calls',
                     'research_references', 'research_advice', 'research_rate_limits', 'training_tasks', 'chat_training_plans', 'training_launch_claims', 'run_monitor_tasks', 'event_outbox', 'agent_tasks', 'analysis_preferences', 'analysis_results')
ORDER BY TABLE_NAME;
