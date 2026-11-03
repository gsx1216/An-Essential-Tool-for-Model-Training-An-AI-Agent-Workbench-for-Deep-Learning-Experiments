DELETE c FROM experiment_assistant.code_change_claims c
JOIN experiment_assistant.code_changes m ON m.id=c.change_id
JOIN experiment_assistant.experiment_runs r ON r.id=m.run_id AND r.user_id=m.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.revision_launch_claims c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE v FROM experiment_assistant.revision_runs v
JOIN experiment_assistant.experiment_runs r ON r.id=v.run_id AND r.user_id=v.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.code_changes c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.suggestion_evidence c
JOIN experiment_assistant.agent_tasks a ON a.id=c.task_id
JOIN experiment_assistant.experiment_runs r ON r.id=a.run_id AND r.user_id=a.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.suggestion_results c
JOIN experiment_assistant.agent_tasks a ON a.id=c.task_id
JOIN experiment_assistant.experiment_runs r ON r.id=a.run_id AND r.user_id=a.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.analysis_results c
JOIN experiment_assistant.agent_tasks a ON a.id=c.task_id
JOIN experiment_assistant.experiment_runs r ON r.id=a.run_id AND r.user_id=a.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.agent_tasks c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.event_outbox c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_monitor_tasks c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_metric_parsers c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_metric_events c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_metric_streams c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_reports c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_evaluation_specs c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.research_advice c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.research_references c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.chat_tool_calls c
JOIN experiment_assistant.experiment_runs r ON r.user_id=c.user_id AND
 (r.id=REPLACE(JSON_UNQUOTE(JSON_EXTRACT(c.result_meta_json,'$.run_id')),'-','') OR
  r.id=REPLACE(JSON_UNQUOTE(JSON_EXTRACT(c.arguments_json,'$.run_id')),'-',''))
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.chat_run_bindings c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.chat_training_plans c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE c FROM experiment_assistant.run_executions c
JOIN experiment_assistant.experiment_runs r ON r.id=c.run_id AND r.user_id=c.user_id
WHERE r.user_id=%s AND r.{target}=%s;

DELETE r FROM experiment_assistant.experiment_runs r WHERE r.user_id=%s AND r.{target}=%s;
