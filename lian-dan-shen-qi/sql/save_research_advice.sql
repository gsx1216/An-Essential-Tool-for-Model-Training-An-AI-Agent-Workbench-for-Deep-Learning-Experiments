INSERT INTO experiment_assistant.research_advice
(id,session_id,user_id,turn_id,attempt_id,run_id,advice_json,rendered_text,advice_sha256,created_at)
VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6));
