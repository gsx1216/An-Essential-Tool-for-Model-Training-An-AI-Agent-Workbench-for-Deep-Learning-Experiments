-- 仅新增一张表；不删除或更新已有用户、会话、原文、轮次及凭据。
CREATE TABLE IF NOT EXISTS experiment_assistant.chat_context_summaries (
    session_id CHAR(32) NOT NULL COMMENT '每个聊天会话保存一份当前摘要',
    content LONGTEXT NOT NULL COMMENT '摘要正文；不覆盖 chat_messages 原文',
    summarized_until_seq BIGINT UNSIGNED NOT NULL COMMENT '已总结到的真实消息序号',
    version BIGINT UNSIGNED NOT NULL COMMENT '摘要乐观锁版本，从 1 递增',
    missing_ranges JSON NOT NULL COMMENT '已覆盖范围中缺失的真实序号区间',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (session_id),
    CONSTRAINT fk_context_summary_session FOREIGN KEY (session_id)
        REFERENCES experiment_assistant.chat_sessions (id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT ck_context_summary_seq CHECK (summarized_until_seq >= 1),
    CONSTRAINT ck_context_summary_version CHECK (version >= 1),
    CONSTRAINT ck_context_summary_gaps CHECK (JSON_TYPE(missing_ranges) = 'ARRAY')
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
