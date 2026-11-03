CREATE TABLE IF NOT EXISTS experiment_assistant.research_rate_limits (
 provider VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,next_allowed DATETIME(6) NOT NULL
) ENGINE=InnoDB;
