INSERT INTO experiment_assistant.run_reports
    (run_id, user_id, version, source_revision, run_signature, report_json, report_digest, generated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE version=VALUES(version), source_revision=VALUES(source_revision),
    run_signature=VALUES(run_signature), report_json=VALUES(report_json), report_digest=VALUES(report_digest), generated_at=UTC_TIMESTAMP(6);
