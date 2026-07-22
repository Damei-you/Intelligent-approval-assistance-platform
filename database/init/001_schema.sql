BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE TABLE contract_types (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(32) NOT NULL UNIQUE,
    name VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE contracts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_no VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    contract_type_id UUID NOT NULL REFERENCES contract_types(id),
    counterparty VARCHAR(255),
    amount NUMERIC(18, 2),
    currency VARCHAR(16) NOT NULL DEFAULT 'CNY',
    status VARCHAR(32) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN (
            'DRAFT', 'PARSING', 'READY', 'REVIEWING',
            'PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'RETURNED'
        )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount IS NULL OR amount >= 0)
);

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_type VARCHAR(32) NOT NULL
        CHECK (document_type IN ('CONTRACT', 'POLICY')),
    contract_id UUID REFERENCES contracts(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    revision_no INTEGER NOT NULL DEFAULT 1 CHECK (revision_no > 0),
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    storage_uri TEXT,
    file_name VARCHAR(255),
    mime_type VARCHAR(128),
    file_hash VARCHAR(64),
    parse_status VARCHAR(32) NOT NULL DEFAULT 'PENDING'
        CHECK (parse_status IN ('PENDING', 'PARSING', 'PARSED', 'FAILED')),
    raw_text TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (document_type = 'CONTRACT' AND contract_id IS NOT NULL)
        OR (document_type = 'POLICY' AND contract_id IS NULL)
    )
);

CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_type VARCHAR(32) NOT NULL
        CHECK (chunk_type IN ('CONTRACT_CLAUSE', 'POLICY_SECTION')),
    clause_no VARCHAR(64),
    title VARCHAR(255),
    content TEXT NOT NULL,
    page_no INTEGER CHECK (page_no IS NULL OR page_no > 0),
    token_count INTEGER CHECK (token_count IS NULL OR token_count >= 0),
    embedding VECTOR(1536),
    embedding_model VARCHAR(128),
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, chunk_index)
);

CREATE TABLE review_check_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    description TEXT NOT NULL,
    prompt_template TEXT NOT NULL,
    default_severity VARCHAR(16) NOT NULL DEFAULT 'MEDIUM'
        CHECK (default_severity IN ('LOW', 'MEDIUM', 'HIGH')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE review_check_item_scopes (
    check_item_id UUID NOT NULL REFERENCES review_check_items(id) ON DELETE CASCADE,
    contract_type_id UUID NOT NULL REFERENCES contract_types(id) ON DELETE CASCADE,
    PRIMARY KEY (check_item_id, contract_type_id)
);

CREATE TABLE review_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id UUID NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    contract_document_id UUID NOT NULL REFERENCES documents(id),
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    overall_risk_level VARCHAR(16)
        CHECK (overall_risk_level IS NULL OR overall_risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    summary TEXT,
    approval_suggestion VARCHAR(32)
        CHECK (
            approval_suggestion IS NULL
            OR approval_suggestion IN ('APPROVE', 'APPROVE_AFTER_REVISION', 'REJECT')
        ),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE risk_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_run_id UUID NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    check_item_id UUID NOT NULL REFERENCES review_check_items(id),
    status VARCHAR(32) NOT NULL
        CHECK (status IN ('PASS', 'RISK', 'INSUFFICIENT_INFORMATION')),
    severity VARCHAR(16) NOT NULL
        CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH')),
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    suggestion TEXT,
    confidence NUMERIC(5, 4),
    structured_output JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (review_run_id, check_item_id),
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE TABLE finding_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id UUID NOT NULL REFERENCES risk_findings(id) ON DELETE CASCADE,
    chunk_id UUID NOT NULL REFERENCES document_chunks(id),
    evidence_type VARCHAR(16) NOT NULL
        CHECK (evidence_type IN ('CONTRACT', 'POLICY')),
    relevance_score NUMERIC(8, 6),
    cited_text TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (relevance_score IS NULL OR (relevance_score >= -1 AND relevance_score <= 1))
);

CREATE TABLE approval_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id UUID NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    review_run_id UUID NOT NULL REFERENCES review_runs(id),
    status VARCHAR(32) NOT NULL DEFAULT 'IN_PROGRESS'
        CHECK (status IN ('IN_PROGRESS', 'APPROVED', 'REJECTED', 'RETURNED')),
    current_step_no INTEGER NOT NULL DEFAULT 1 CHECK (current_step_no > 0),
    final_decision VARCHAR(32)
        CHECK (final_decision IS NULL OR final_decision IN ('APPROVED', 'REJECTED', 'RETURNED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_approval_instances_review_run UNIQUE (review_run_id)
);

CREATE TABLE approval_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_instance_id UUID NOT NULL REFERENCES approval_instances(id) ON DELETE CASCADE,
    step_no INTEGER NOT NULL CHECK (step_no > 0),
    step_type VARCHAR(32) NOT NULL CHECK (step_type IN ('BUSINESS', 'LEGAL')),
    step_name VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED')),
    approver_name VARCHAR(128),
    decision VARCHAR(32)
        CHECK (decision IS NULL OR decision IN ('APPROVED', 'REJECTED', 'RETURNED')),
    comment TEXT,
    handled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (approval_instance_id, step_no)
);

CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id UUID NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    review_run_id UUID REFERENCES review_runs(id) ON DELETE SET NULL,
    finding_id UUID,
    contract_document_id UUID,
    title VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_chat_sessions_finding
        FOREIGN KEY (finding_id) REFERENCES risk_findings(id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_sessions_contract_document
        FOREIGN KEY (contract_document_id) REFERENCES documents(id)
);

CREATE TABLE chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL CHECK (role IN ('USER', 'ASSISTANT', 'SYSTEM')),
    content TEXT NOT NULL,
    intent VARCHAR(32),
    structured_output JSONB NOT NULL DEFAULT '{}'::JSONB,
    client_request_id UUID,
    reply_to_message_id UUID,
    status VARCHAR(16) NOT NULL DEFAULT 'SUCCEEDED',
    model_name VARCHAR(128),
    token_usage JSONB NOT NULL DEFAULT '{}'::JSONB,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_chat_messages_reply_to_message
        FOREIGN KEY (reply_to_message_id) REFERENCES chat_messages(id) ON DELETE SET NULL,
    CONSTRAINT ck_chat_messages_intent CHECK (
        intent IS NULL
        OR intent IN ('AUTO', 'EXPLAIN', 'EVIDENCE_QUERY', 'DRAFT_CLAUSE')
    ),
    CONSTRAINT ck_chat_messages_status
        CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED'))
);

CREATE TABLE chat_message_citations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    chunk_id UUID NOT NULL REFERENCES document_chunks(id),
    citation_label VARCHAR(16),
    relevance_score NUMERIC(8, 6),
    cited_text TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (relevance_score IS NULL OR (relevance_score >= -1 AND relevance_score <= 1)),
    CONSTRAINT ck_chat_message_citations_label CHECK (
        citation_label IS NULL OR citation_label ~ '^[CP][1-9][0-9]*$'
    )
);

CREATE TABLE workflow_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type VARCHAR(32) NOT NULL CHECK (run_type IN ('RISK_REVIEW', 'CONTRACT_CHAT')),
    review_run_id UUID REFERENCES review_runs(id) ON DELETE CASCADE,
    chat_message_id UUID REFERENCES chat_messages(id) ON DELETE CASCADE,
    graph_name VARCHAR(128) NOT NULL,
    graph_version VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    state_snapshot JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (num_nonnulls(review_run_id, chat_message_id) = 1),
    CHECK (
        (run_type = 'RISK_REVIEW' AND review_run_id IS NOT NULL)
        OR (run_type = 'CONTRACT_CHAT' AND chat_message_id IS NOT NULL)
    )
);

CREATE TABLE workflow_node_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_name VARCHAR(128) NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 0),
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    input_data JSONB NOT NULL DEFAULT '{}'::JSONB,
    output_data JSONB NOT NULL DEFAULT '{}'::JSONB,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (workflow_run_id, sequence_no)
);

CREATE TABLE retrieval_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_run_id UUID NOT NULL REFERENCES workflow_node_runs(id) ON DELETE CASCADE,
    query_text TEXT NOT NULL,
    query_embedding_model VARCHAR(128),
    filters JSONB NOT NULL DEFAULT '{}'::JSONB,
    top_k INTEGER NOT NULL DEFAULT 5 CHECK (top_k > 0),
    final_top_k INTEGER CHECK (final_top_k IS NULL OR final_top_k > 0),
    ranking_strategy VARCHAR(32) NOT NULL DEFAULT 'VECTOR'
        CHECK (ranking_strategy IN ('VECTOR', 'RERANK', 'RERANK_FALLBACK')),
    rerank_model VARCHAR(128),
    rerank_latency_ms INTEGER CHECK (rerank_latency_ms IS NULL OR rerank_latency_ms >= 0),
    rerank_error TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE retrieval_hits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retrieval_run_id UUID NOT NULL REFERENCES retrieval_runs(id) ON DELETE CASCADE,
    chunk_id UUID NOT NULL REFERENCES document_chunks(id),
    rank_no INTEGER NOT NULL CHECK (rank_no > 0),
    similarity_score NUMERIC(8, 6) NOT NULL,
    rerank_rank_no INTEGER CHECK (rerank_rank_no IS NULL OR rerank_rank_no > 0),
    rerank_score DOUBLE PRECISION,
    selected_for_context BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (retrieval_run_id, rank_no),
    UNIQUE (retrieval_run_id, chunk_id),
    CHECK (similarity_score >= -1 AND similarity_score <= 1)
);

CREATE TABLE llm_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_run_id UUID NOT NULL REFERENCES workflow_node_runs(id) ON DELETE CASCADE,
    provider VARCHAR(64),
    model_name VARCHAR(128) NOT NULL,
    prompt_name VARCHAR(128),
    input_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    output_data JSONB NOT NULL DEFAULT '{}'::JSONB,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    status VARCHAR(32) NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE async_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    celery_task_id VARCHAR(128) NOT NULL UNIQUE,
    task_type VARCHAR(64) NOT NULL
        CHECK (task_type IN ('DOCUMENT_PARSE', 'DOCUMENT_EMBEDDING', 'RISK_REVIEW')),
    resource_type VARCHAR(32) NOT NULL
        CHECK (resource_type IN ('DOCUMENT', 'CONTRACT', 'REVIEW_RUN')),
    resource_id UUID NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'RETRYING', 'CANCELLED')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    result_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    error_message TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_documents_contract_revision
    ON documents (contract_id, revision_no)
    WHERE document_type = 'CONTRACT';

CREATE UNIQUE INDEX uq_documents_current_contract
    ON documents (contract_id)
    WHERE document_type = 'CONTRACT' AND is_current;

CREATE INDEX idx_documents_type_parse_status
    ON documents (document_type, parse_status);
CREATE INDEX idx_document_chunks_document_id
    ON document_chunks (document_id);
CREATE INDEX idx_document_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_review_runs_contract_created
    ON review_runs (contract_id, created_at DESC);
CREATE INDEX idx_risk_findings_review_severity
    ON risk_findings (review_run_id, severity);
CREATE INDEX idx_finding_evidence_finding
    ON finding_evidence (finding_id, sort_order);
CREATE INDEX idx_approval_instances_contract_status
    ON approval_instances (contract_id, status);
CREATE INDEX idx_approval_steps_pending
    ON approval_steps (approval_instance_id, step_no)
    WHERE status IN ('PENDING', 'IN_PROGRESS');
CREATE UNIQUE INDEX uq_chat_sessions_finding
    ON chat_sessions (finding_id)
    WHERE finding_id IS NOT NULL;
CREATE INDEX idx_chat_messages_session_created
    ON chat_messages (session_id, created_at);
CREATE UNIQUE INDEX uq_chat_messages_session_client_request
    ON chat_messages (session_id, client_request_id)
    WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX uq_chat_messages_reply_to_message
    ON chat_messages (reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;
CREATE UNIQUE INDEX uq_chat_message_citations_label
    ON chat_message_citations (message_id, citation_label)
    WHERE citation_label IS NOT NULL;
CREATE INDEX idx_workflow_runs_status_created
    ON workflow_runs (status, created_at);
CREATE INDEX idx_async_jobs_status_queued
    ON async_jobs (status, queued_at);
CREATE INDEX idx_async_jobs_resource
    ON async_jobs (resource_type, resource_id);

CREATE TRIGGER trg_contract_types_updated_at
BEFORE UPDATE ON contract_types
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_contracts_updated_at
BEFORE UPDATE ON contracts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_documents_updated_at
BEFORE UPDATE ON documents
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_review_check_items_updated_at
BEFORE UPDATE ON review_check_items
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_chat_sessions_updated_at
BEFORE UPDATE ON chat_sessions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_async_jobs_updated_at
BEFORE UPDATE ON async_jobs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE document_chunks IS '合同条款与制度段落的统一 RAG 分块及向量存储';
COMMENT ON TABLE review_runs IS '一次完整的合同风险检查业务任务';
COMMENT ON TABLE risk_findings IS '按预定义检查项输出的结构化风险结论';
COMMENT ON TABLE finding_evidence IS '风险结论引用的合同条款或制度依据';
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
COMMENT ON TABLE workflow_runs IS 'LangGraph 风险审查或合同问答的运行记录';
COMMENT ON TABLE async_jobs IS 'Celery 异步任务的持久化业务状态，Redis 仅负责调度';

COMMIT;
