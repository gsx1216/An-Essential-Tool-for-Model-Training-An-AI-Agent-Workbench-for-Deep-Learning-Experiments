INSERT INTO experiment_assistant.research_references
(id,session_id,user_id,turn_id,attempt_id,run_id,source_url,title,source_kind,content_text,content_sha256,metadata_json,retrieved_at)
VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6));
