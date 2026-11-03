ALTER TABLE experiment_assistant.chat_turns
ADD COLUMN skill_snapshot_json TEXT COLLATE utf8mb4_bin NULL COMMENT '本轮可选Skill不可变快照，NULL表示未选择',
ADD CONSTRAINT ck_chat_turn_skill CHECK (skill_snapshot_json IS NULL OR (JSON_VALID(skill_snapshot_json) AND JSON_TYPE(skill_snapshot_json)='OBJECT' AND OCTET_LENGTH(skill_snapshot_json)<=12288));
