INSERT INTO experiment_assistant.run_metric_streams
    (run_id, user_id, revision, source_offset, state_json, state_digest, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE revision = VALUES(revision), source_offset = VALUES(source_offset),
    state_json = VALUES(state_json), state_digest = VALUES(state_digest), updated_at = UTC_TIMESTAMP(6);
