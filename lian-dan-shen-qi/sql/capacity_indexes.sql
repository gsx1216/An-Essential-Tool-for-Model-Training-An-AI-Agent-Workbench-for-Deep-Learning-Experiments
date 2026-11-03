SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name,
       NON_UNIQUE AS non_unique, SEQ_IN_INDEX AS position,
       COLUMN_NAME AS column_name, IS_VISIBLE AS is_visible
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s
  AND TABLE_NAME IN ('users', 'login_sessions', 'chat_sessions', 'chat_messages',
                     'chat_turns', 'model_credentials', 'chat_context_summaries', 'experiments', 'experiment_runs', 'server_connections', 'run_executions', 'run_metric_streams', 'run_metric_events', 'run_reports', 'experiment_evaluation_specs', 'run_evaluation_specs', 'chat_run_bindings', 'chat_tool_calls', 'research_references', 'research_advice', 'research_rate_limits', 'training_tasks', 'chat_training_plans', 'training_launch_claims', 'run_monitor_tasks', 'event_outbox', 'agent_tasks', 'analysis_preferences', 'analysis_results')
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
