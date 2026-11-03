INSERT INTO experiment_assistant.code_changes(id,user_id,run_id,suggestion_task_id,suggestion_index,request_id,origin_session_id,status,source_json,source_digest,lease_token,lease_until,created_at,updated_at)
VALUES(%s,%s,%s,%s,%s,%s,%s,'planning',%s,%s,%s,TIMESTAMPADD(SECOND,300,UTC_TIMESTAMP(6)),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6));
