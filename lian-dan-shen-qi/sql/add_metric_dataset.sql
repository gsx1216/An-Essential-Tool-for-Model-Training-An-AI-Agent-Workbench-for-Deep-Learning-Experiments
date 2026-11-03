ALTER TABLE experiment_assistant.run_metric_events ADD COLUMN dataset VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'unspecified';
