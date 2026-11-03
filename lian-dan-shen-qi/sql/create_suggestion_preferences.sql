CREATE TABLE IF NOT EXISTS experiment_assistant.suggestion_preferences (
 user_id BIGINT UNSIGNED NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(user_id),
 CONSTRAINT fk_suggestion_preference_user FOREIGN KEY(user_id)
  REFERENCES experiment_assistant.users(id) ON DELETE CASCADE ON UPDATE RESTRICT,
 CONSTRAINT ck_suggestion_preference_enabled CHECK(enabled IN (0,1))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
