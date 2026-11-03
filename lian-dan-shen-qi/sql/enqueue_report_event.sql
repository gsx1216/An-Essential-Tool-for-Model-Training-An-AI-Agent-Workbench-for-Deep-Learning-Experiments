INSERT INTO experiment_assistant.event_outbox
 (id,user_id,run_id,event_type,report_version,report_json,report_digest,created_at,next_attempt_at)
VALUES (%s,%s,%s,'training.report_ready',%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE id=id;
