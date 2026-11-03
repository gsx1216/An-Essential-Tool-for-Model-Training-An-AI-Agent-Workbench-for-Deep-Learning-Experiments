SELECT c.id,c.paper_id,c.chunk_index,c.page_number,c.content_text,p.title,
MATCH(c.content_text) AGAINST(%s IN NATURAL LANGUAGE MODE) AS relevance
FROM experiment_assistant.paper_chunks c JOIN experiment_assistant.papers p ON p.id=c.paper_id AND p.user_id=c.user_id
WHERE c.user_id=%s AND p.status='ready' AND (%s IS NULL OR c.paper_id=%s)
AND MATCH(c.content_text) AGAINST(%s IN NATURAL LANGUAGE MODE)>0
ORDER BY relevance DESC,c.paper_id,c.chunk_index LIMIT %s;
