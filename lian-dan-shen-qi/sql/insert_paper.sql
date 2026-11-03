INSERT INTO experiment_assistant.papers(id,user_id,request_id,title,status,file_sha256,file_bytes,empty_pages_json,lease_token,lease_until,created_at,updated_at)
VALUES(%s,%s,%s,%s,'processing',%s,%s,'[]',%s,TIMESTAMPADD(SECOND,180,UTC_TIMESTAMP(6)),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6));
