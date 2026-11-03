-- 不改动原有用户、会话和消息表；新旧数据库均可重复执行。
CREATE TABLE IF NOT EXISTS experiment_assistant.chat_turns (
    id CHAR(32) NOT NULL COMMENT '轮次 ID，也作为一次发送的幂等请求 ID',
    session_id CHAR(32) NOT NULL,
    user_seq BIGINT UNSIGNED NOT NULL COMMENT '已经落库的用户提问序号',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempt_id CHAR(32) NULL COMMENT '每次领取生成任务时换一个 ID，拒绝旧任务写回',
    lease_until DATETIME(6) NULL COMMENT '生成任务的有效期，过期后允许重试',
    error_code VARCHAR(64) NULL COMMENT '简短错误代码，不保存密钥或原始异常',
    skill_snapshot_json TEXT COLLATE utf8mb4_bin NULL COMMENT '本轮可选Skill不可变快照，NULL表示未选择',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    open_slot TINYINT GENERATED ALWAYS AS (
        CASE WHEN status IN ('pending', 'running', 'failed') THEN 1 ELSE NULL END
    ) STORED,
    PRIMARY KEY (id),
    UNIQUE KEY uq_chat_turns_user_message (session_id, user_seq),
    UNIQUE KEY uq_chat_turns_one_open (session_id, open_slot),
    CONSTRAINT fk_chat_turns_session FOREIGN KEY (session_id)
        REFERENCES experiment_assistant.chat_sessions (id),
    CONSTRAINT fk_chat_turns_user_message FOREIGN KEY (session_id, user_seq)
        REFERENCES experiment_assistant.chat_messages (session_id, seq),
    CONSTRAINT ck_chat_turns_status CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    CONSTRAINT ck_chat_turns_seq CHECK (user_seq >= 1 AND user_seq < 18446744073709551615),
    CONSTRAINT ck_chat_turn_skill CHECK (skill_snapshot_json IS NULL OR (JSON_VALID(skill_snapshot_json) AND JSON_TYPE(skill_snapshot_json)='OBJECT' AND OCTET_LENGTH(skill_snapshot_json)<=12288)),
    CONSTRAINT ck_chat_turns_attempt CHECK (
        (status = 'pending' AND attempt_id IS NULL AND lease_until IS NULL)
        OR (status = 'running' AND attempt_id IS NOT NULL AND lease_until IS NOT NULL)
        OR (status IN ('completed', 'failed') AND attempt_id IS NOT NULL AND lease_until IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
