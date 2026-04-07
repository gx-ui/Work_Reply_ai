-- 单次 /chat 业务快照：works_info 全量；core_info / attention_info / query_info 为请求侧三块（按 intent 填写，其余为 NULL）；
-- rely_info 存本次模型回复内容及依据（Suggestion / Summary / QueryAnswer）。


CREATE TABLE IF NOT EXISTS work_reply_chat_run (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
    intent VARCHAR(16) NOT NULL COMMENT 'suggestion | summary | query',
    session_id VARCHAR(128) NULL DEFAULT NULL,
    ticket_id VARCHAR(512) NOT NULL DEFAULT '' COMMENT '从 works_info 冗余，便于按工单查询',
    works_info JSON NOT NULL COMMENT 'WorksInfo 整段',
    core_info JSON NULL COMMENT 'CoreInfo',
    attention_info JSON NULL COMMENT 'AttentionInfo',
    query_info JSON NULL COMMENT 'QueryInfo',
    rely_info JSON NULL COMMENT '本次回复及依据：Suggestion / Summary / QueryAnswer',
    PRIMARY KEY (id),
    KEY idx_ticket_created (ticket_id, created_at),
    KEY idx_intent_created (intent, created_at),
    KEY idx_session_id (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
