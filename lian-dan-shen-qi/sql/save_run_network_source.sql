INSERT INTO experiment_assistant.run_network_sources (run_id,user_id,path,file_sha256,selected_at)
SELECT id,user_id,%s,%s,UTC_TIMESTAMP(6) FROM experiment_assistant.experiment_runs WHERE user_id=%s AND id=%s
ON DUPLICATE KEY UPDATE path=VALUES(path),file_sha256=VALUES(file_sha256),selected_at=VALUES(selected_at);
