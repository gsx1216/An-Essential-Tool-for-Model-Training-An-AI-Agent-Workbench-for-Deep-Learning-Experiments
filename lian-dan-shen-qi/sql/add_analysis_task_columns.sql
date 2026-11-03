ALTER TABLE experiment_assistant.agent_tasks
 ADD COLUMN attempts BIGINT UNSIGNED NOT NULL DEFAULT 0,
 ADD COLUMN attempts_in_cycle INT UNSIGNED NOT NULL DEFAULT 0,
 ADD COLUMN lease_token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD COLUMN lease_until DATETIME(6) NULL,
 ADD COLUMN next_attempt_at DATETIME(6) NULL,
 ADD COLUMN last_error_code VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD COLUMN started_at DATETIME(6) NULL,
 ADD COLUMN finished_at DATETIME(6) NULL,
 ADD KEY idx_analysis_due(status,next_attempt_at,id),
 ADD KEY idx_analysis_lease(status,lease_until,id),
 ADD CONSTRAINT ck_analysis_lease CHECK((status='running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR (status<>'running' AND lease_token IS NULL AND lease_until IS NULL));
