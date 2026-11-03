CREATE TABLE IF NOT EXISTS experiment_assistant.run_metric_parsers (
 run_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 rules_json TEXT COLLATE utf8mb4_bin NOT NULL,
 updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(run_id),
 CONSTRAINT fk_metric_parser_run FOREIGN KEY(run_id,user_id)
  REFERENCES experiment_assistant.experiment_runs(id,user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
 CONSTRAINT ck_metric_parser_rules CHECK(JSON_VALID(rules_json) AND JSON_TYPE(rules_json)='ARRAY' AND OCTET_LENGTH(rules_json)<=32768)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
