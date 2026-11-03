CREATE TABLE IF NOT EXISTS experiment_assistant.paper_files (
 paper_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 pdf_bytes MEDIUMBLOB NOT NULL,
 PRIMARY KEY(paper_id),
 CONSTRAINT fk_paper_file_owner FOREIGN KEY(paper_id,user_id) REFERENCES experiment_assistant.papers(id,user_id) ON DELETE CASCADE,
 CONSTRAINT ck_paper_binary CHECK(OCTET_LENGTH(pdf_bytes) BETWEEN 6 AND 10485760)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
