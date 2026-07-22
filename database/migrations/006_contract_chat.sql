BEGIN;

-- 已有数据库不会重新执行 init 脚本，因此使用可空锚点字段兼容历史通用会话。
-- 应用层新建风险会话时必须同时写入 finding_id 和 contract_document_id。
ALTER TABLE chat_sessions
    ADD COLUMN IF NOT EXISTS finding_id UUID,
    ADD COLUMN IF NOT EXISTS contract_document_id UUID;

-- 历史消息没有意图和客户端请求 UUID，保持 NULL 即可；
-- 结构化输出与状态提供默认值，使已有消息可以无损升级。
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS intent VARCHAR(32),
    ADD COLUMN IF NOT EXISTS structured_output JSONB NOT NULL DEFAULT '{}'::JSONB,
    ADD COLUMN IF NOT EXISTS client_request_id UUID,
    ADD COLUMN IF NOT EXISTS reply_to_message_id UUID,
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'SUCCEEDED';

-- 保存模型实际使用且已经后端校验的 C/P 标签，历史引用允许保持 NULL。
ALTER TABLE chat_message_citations
    ADD COLUMN IF NOT EXISTS citation_label VARCHAR(16);

-- 即使上次执行在增加字段后中断，重跑迁移也会恢复默认值和非空约束。
UPDATE chat_messages
SET structured_output = '{}'::JSONB
WHERE structured_output IS NULL;

UPDATE chat_messages
SET status = 'SUCCEEDED'
WHERE status IS NULL;

ALTER TABLE chat_messages
    ALTER COLUMN structured_output SET DEFAULT '{}'::JSONB,
    ALTER COLUMN structured_output SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'SUCCEEDED',
    ALTER COLUMN status SET NOT NULL;

-- 外键和检查约束使用显式名称及 DO 块，确保迁移重复执行时不会重复创建。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_chat_sessions_finding'
          AND conrelid = 'chat_sessions'::regclass
    ) THEN
        ALTER TABLE chat_sessions
            ADD CONSTRAINT fk_chat_sessions_finding
            FOREIGN KEY (finding_id) REFERENCES risk_findings(id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_chat_sessions_contract_document'
          AND conrelid = 'chat_sessions'::regclass
    ) THEN
        ALTER TABLE chat_sessions
            ADD CONSTRAINT fk_chat_sessions_contract_document
            FOREIGN KEY (contract_document_id) REFERENCES documents(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_chat_messages_reply_to_message'
          AND conrelid = 'chat_messages'::regclass
    ) THEN
        ALTER TABLE chat_messages
            ADD CONSTRAINT fk_chat_messages_reply_to_message
            FOREIGN KEY (reply_to_message_id) REFERENCES chat_messages(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_chat_messages_intent'
          AND conrelid = 'chat_messages'::regclass
    ) THEN
        ALTER TABLE chat_messages
            ADD CONSTRAINT ck_chat_messages_intent
            CHECK (
                intent IS NULL
                OR intent IN ('AUTO', 'EXPLAIN', 'EVIDENCE_QUERY', 'DRAFT_CLAUSE')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_chat_messages_status'
          AND conrelid = 'chat_messages'::regclass
    ) THEN
        ALTER TABLE chat_messages
            ADD CONSTRAINT ck_chat_messages_status
            CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_chat_message_citations_label'
          AND conrelid = 'chat_message_citations'::regclass
    ) THEN
        ALTER TABLE chat_message_citations
            ADD CONSTRAINT ck_chat_message_citations_label
            CHECK (
                citation_label IS NULL
                OR citation_label ~ '^[CP][1-9][0-9]*$'
            );
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_sessions_finding
    ON chat_sessions (finding_id)
    WHERE finding_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_session_client_request
    ON chat_messages (session_id, client_request_id)
    WHERE client_request_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_reply_to_message
    ON chat_messages (reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_message_citations_label
    ON chat_message_citations (message_id, citation_label)
    WHERE citation_label IS NOT NULL;

COMMENT ON COLUMN chat_sessions.finding_id IS '会话锚定的风险结论；历史通用会话可为空，新建风险会话必须写入';
COMMENT ON COLUMN chat_sessions.contract_document_id IS '会话固定使用的合同修订文档，避免后续修订导致引用漂移';
COMMENT ON COLUMN chat_messages.intent IS '本轮问答意图，SYSTEM 消息和历史消息可为空';
COMMENT ON COLUMN chat_messages.structured_output IS '条款修改草案等可机读的结构化回答';
COMMENT ON COLUMN chat_messages.client_request_id IS '前端生成的请求 UUID，用于在同一会话内防止重复生成';
COMMENT ON COLUMN chat_messages.reply_to_message_id IS '助手消息回复的用户消息，用于请求重试时准确恢复同一问答对';
COMMENT ON COLUMN chat_messages.status IS '消息生成状态，历史消息按已成功处理';
COMMENT ON COLUMN chat_message_citations.citation_label IS '模型回答使用且经后端校验的 C/P 引用标签';
COMMENT ON INDEX uq_chat_sessions_finding IS '一个风险项最多对应一个可持续恢复的会话';
COMMENT ON INDEX uq_chat_messages_session_client_request IS '同一会话内的客户端请求 UUID 唯一，用于消息写入幂等';
COMMENT ON INDEX uq_chat_messages_reply_to_message IS '一条用户消息最多只对应一条助手回复';
COMMENT ON INDEX uq_chat_message_citations_label IS '同一条助手消息中的引用标签必须唯一';

COMMIT;
