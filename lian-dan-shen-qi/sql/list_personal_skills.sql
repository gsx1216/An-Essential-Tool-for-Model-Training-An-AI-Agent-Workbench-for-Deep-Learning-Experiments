SELECT JSON_REMOVE(snapshot_json, '$.instructions') FROM experiment_assistant.personal_skills
WHERE user_id=%s ORDER BY created_at,skill_id;
