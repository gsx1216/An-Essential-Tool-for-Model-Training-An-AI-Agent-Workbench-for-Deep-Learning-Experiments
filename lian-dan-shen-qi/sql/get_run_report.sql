SELECT version, source_revision, run_signature, report_json, report_digest, generated_at
FROM experiment_assistant.run_reports WHERE user_id = %s AND run_id = %s;
