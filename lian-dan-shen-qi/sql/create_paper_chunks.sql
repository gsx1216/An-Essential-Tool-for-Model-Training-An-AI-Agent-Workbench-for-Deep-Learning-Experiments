CREATE TABLE IF NOT EXISTS experiment_assistant.paper_chunks (
 id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 paper_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 chunk_index INT UNSIGNED NOT NULL,page_number INT UNSIGNED NOT NULL,char_start INT UNSIGNED NOT NULL,
 content_text TEXT NOT NULL,
 content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 PRIMARY KEY(id),UNIQUE KEY uq_paper_chunk(paper_id,chunk_index),KEY idx_paper_chunk_owner(user_id,paper_id),
 FULLTEXT KEY ft_paper_text(content_text) WITH PARSER ngram,
 CONSTRAINT fk_paper_chunk_owner FOREIGN KEY(paper_id,user_id) REFERENCES experiment_assistant.papers(id,user_id) ON DELETE CASCADE,
 CONSTRAINT ck_paper_chunk CHECK(chunk_index<3000 AND page_number BETWEEN 1 AND 200 AND char_start<=100000 AND CHAR_LENGTH(content_text) BETWEEN 1 AND 6000)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
