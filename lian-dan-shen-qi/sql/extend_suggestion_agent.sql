ALTER TABLE experiment_assistant.agent_tasks DROP CHECK ck_agent_task_name, ADD CONSTRAINT ck_agent_task_name CHECK(agent_name IN ('analysis','suggestion'));
