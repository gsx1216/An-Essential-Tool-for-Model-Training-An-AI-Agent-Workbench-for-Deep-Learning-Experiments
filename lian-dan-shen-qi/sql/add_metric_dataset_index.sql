ALTER TABLE experiment_assistant.run_metric_events ADD INDEX idx_metrics_run_dataset_split (run_id, dataset, split, source_offset);
