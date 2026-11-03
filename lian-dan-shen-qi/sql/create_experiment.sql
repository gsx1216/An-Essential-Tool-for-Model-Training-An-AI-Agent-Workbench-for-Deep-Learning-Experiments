-- 只有用户仍然存在时插入；外键提供最终关联约束。
INSERT INTO experiment_assistant.experiments
    (id, user_id, title, kind, description, created_at, updated_at)
SELECT %s, id, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
FROM experiment_assistant.users
WHERE id = %s;
