INSERT INTO experiment_assistant.agent_tasks(id,user_id,run_id,event_id,agent_name,report_version,status,created_at)
VALUES(%s,%s,%s,%s,'suggestion',%s,'queued',UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE id=id;
