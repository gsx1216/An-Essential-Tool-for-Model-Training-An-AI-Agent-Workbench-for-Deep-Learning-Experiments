INSERT INTO experiment_assistant.revision_runs(run_id,user_id,original_run_id,change_id,request_id,binding_json,binding_digest,origin_session_id,created_at)
VALUES(%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6));
