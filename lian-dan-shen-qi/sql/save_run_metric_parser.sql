INSERT INTO experiment_assistant.run_metric_parsers(run_id,user_id,rules_json,updated_at)
VALUES(%s,%s,%s,UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE rules_json=VALUES(rules_json),updated_at=UTC_TIMESTAMP(6);
