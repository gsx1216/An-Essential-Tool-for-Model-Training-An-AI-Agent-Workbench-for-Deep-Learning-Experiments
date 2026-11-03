CREATE TABLE IF NOT EXISTS experiment_assistant.revision_runs (
 run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 original_run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 change_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 request_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 binding_json MEDIUMTEXT COLLATE utf8mb4_bin NOT NULL,
 binding_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 origin_session_id CHAR(32) NULL,
 created_at DATETIME(6) NOT NULL,
 PRIMARY KEY(run_id), UNIQUE KEY uq_revision_owner_request(user_id,request_id),
 KEY idx_revision_change(user_id,change_id,created_at DESC),
 CONSTRAINT fk_revision_run_owner FOREIGN KEY(run_id,user_id) REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT,
 CONSTRAINT fk_revision_original_owner FOREIGN KEY(original_run_id,user_id) REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT,
 CONSTRAINT fk_revision_change_owner FOREIGN KEY(change_id,user_id) REFERENCES experiment_assistant.code_changes(id,user_id) ON DELETE RESTRICT,
 CONSTRAINT ck_revision_binding CHECK(JSON_VALID(binding_json) AND JSON_TYPE(binding_json)='OBJECT' AND OCTET_LENGTH(binding_json)<=131072),
 CONSTRAINT ck_revision_distinct CHECK(run_id<>original_run_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
