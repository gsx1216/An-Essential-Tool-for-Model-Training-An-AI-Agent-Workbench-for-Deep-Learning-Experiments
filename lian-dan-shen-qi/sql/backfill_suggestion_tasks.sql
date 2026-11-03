INSERT INTO experiment_assistant.agent_tasks(id,user_id,run_id,event_id,agent_name,report_version,status,created_at)
SELECT LEFT(SHA2(CONCAT('suggestion:',t.event_id),256),32),t.user_id,t.run_id,t.event_id,'suggestion',t.report_version,'queued',UTC_TIMESTAMP(6)
FROM experiment_assistant.agent_tasks AS t
JOIN experiment_assistant.analysis_results AS a ON a.task_id=t.id
WHERE t.agent_name='analysis' AND t.status='succeeded'
ON DUPLICATE KEY UPDATE id=experiment_assistant.agent_tasks.id;
