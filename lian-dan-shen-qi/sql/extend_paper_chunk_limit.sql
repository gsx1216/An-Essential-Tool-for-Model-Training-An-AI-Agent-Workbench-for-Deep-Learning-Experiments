ALTER TABLE experiment_assistant.paper_chunks DROP CHECK ck_paper_chunk,
ADD CONSTRAINT ck_paper_chunk CHECK(chunk_index<3000 AND page_number BETWEEN 1 AND 200 AND char_start<=100000 AND CHAR_LENGTH(content_text) BETWEEN 1 AND 6000);
