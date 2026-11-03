CREATE TABLE IF NOT EXISTS experiment_assistant.papers (
 id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 request_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 title VARCHAR(200) NOT NULL,
 status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 file_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 file_bytes INT UNSIGNED NOT NULL,
 page_count INT UNSIGNED NOT NULL DEFAULT 0,
 chunk_count INT UNSIGNED NOT NULL DEFAULT 0,
 empty_pages_json TEXT COLLATE utf8mb4_bin NOT NULL,
 parser_version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 error_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
 lease_token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
 lease_until DATETIME(6) NULL,
 created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(id),UNIQUE KEY uq_paper_owner(id,user_id),UNIQUE KEY uq_paper_request(user_id,request_id),
 KEY idx_paper_owner_created(user_id,created_at DESC,id DESC),
 CONSTRAINT fk_paper_user FOREIGN KEY(user_id) REFERENCES experiment_assistant.users(id) ON DELETE RESTRICT,
 CONSTRAINT ck_paper_file CHECK(file_bytes BETWEEN 6 AND 10485760),
 CONSTRAINT ck_paper_count CHECK(page_count<=200 AND chunk_count<=3000),
 CONSTRAINT ck_paper_empty CHECK(JSON_VALID(empty_pages_json) AND JSON_TYPE(empty_pages_json)='ARRAY' AND OCTET_LENGTH(empty_pages_json)<=4096),
 CONSTRAINT ck_paper_status CHECK((status='processing' AND lease_token IS NOT NULL AND lease_until IS NOT NULL AND error_code IS NULL)
 OR (status='ready' AND lease_token IS NULL AND lease_until IS NULL AND error_code IS NULL AND page_count>0 AND chunk_count>0)
 OR (status='failed' AND lease_token IS NULL AND lease_until IS NULL AND error_code IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
