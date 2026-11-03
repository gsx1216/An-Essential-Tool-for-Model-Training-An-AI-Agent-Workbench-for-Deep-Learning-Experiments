INSERT INTO experiment_assistant.chat_run_bindings(session_id,user_id,run_id,updated_at)
VALUES(%s,%s,%s,UTC_TIMESTAMP(6)) AS incoming ON DUPLICATE KEY UPDATE run_id=incoming.run_id,updated_at=incoming.updated_at;
