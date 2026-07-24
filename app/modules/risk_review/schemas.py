from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ReviewStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
WorkflowNodeStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"]
FindingStatus = Literal["PASS", "RISK", "INSUFFICIENT_INFORMATION"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


class ReviewContractOption(BaseModel):
    """前端发起审查时可选择的当前合同版本。"""

    contract_id: UUID
    contract_no: str
    contract_name: str
    contract_type_code: str
    document_id: UUID
    revision_no: int
    clause_count: int
    vectorized_clause_count: int
    review_ready: bool
    latest_review_run_id: UUID | None = None
    latest_review_status: ReviewStatus | None = None
    latest_review_created_at: datetime | None = None
    latest_review_is_current: bool | None = None


class RiskReviewCreateRequest(BaseModel):
    """创建审查只需要合同 ID，后端固定使用其当前文档版本。"""

    contract_id: UUID


class RiskReviewCreateResponse(BaseModel):
    review_run_id: UUID
    job_id: UUID
    celery_task_id: str
    status: Literal["QUEUED"] = "QUEUED"
    message: str = "风险审查任务已进入队列。"


class RiskEvidence(BaseModel):
    chunk_id: UUID
    evidence_type: Literal["CONTRACT", "POLICY"]
    document_title: str
    clause_no: str | None = None
    title: str | None = None
    cited_text: str
    relevance_score: float | None = None


class RiskRetrievalCandidate(BaseModel):
    """审查节点实际检索到的候选条款，不等同于最终采纳证据。"""

    chunk_id: UUID
    evidence_type: Literal["CONTRACT", "POLICY"]
    document_title: str
    clause_no: str | None = None
    title: str | None = None
    content: str
    # 同一条款可能在首次查询和补检查询中分别命中，轮次字段让前端可以稳定区分两条轨迹。
    retrieval_attempt: int = Field(default=1, ge=1, le=2)
    query_kind: Literal["INITIAL", "SUPPLEMENTAL"] = "INITIAL"
    rank_no: int = Field(ge=1)
    similarity_score: float
    rerank_rank_no: int | None = Field(default=None, ge=1)
    rerank_score: float | None = None
    selected_for_context: bool = False
    ranking_strategy: Literal["VECTOR", "RERANK", "RERANK_FALLBACK"] = "VECTOR"
    rerank_model: str | None = None
    selected_as_evidence: bool = False


class RiskFinding(BaseModel):
    id: UUID
    check_code: str
    check_name: str
    status: FindingStatus
    severity: RiskLevel
    title: str
    description: str
    suggestion: str | None = None
    confidence: float | None = None
    evidence: list[RiskEvidence] = Field(default_factory=list)
    retrieval_candidates: list[RiskRetrievalCandidate] = Field(default_factory=list)


class RiskReviewDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    review_run_id: UUID
    contract_id: UUID
    contract_no: str
    contract_name: str
    contract_type_code: str
    contract_document_id: UUID
    revision_no: int
    status: ReviewStatus
    progress: int = Field(ge=0, le=100)
    overall_risk_level: RiskLevel | None = None
    summary: str | None = None
    approval_suggestion: Literal[
        "APPROVE", "APPROVE_AFTER_REVISION", "REJECT"
    ] | None = None
    error_message: str | None = None
    findings: list[RiskFinding] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class RiskTraceRetrieval(BaseModel):
    """单次证据检索的脱敏摘要，不返回候选正文或数据库内部过滤条件。"""

    source: Literal["CONTRACT", "POLICY"]
    retrieval_attempt: int = Field(ge=1, le=2)
    query_kind: Literal["INITIAL", "SUPPLEMENTAL"]
    query_text: str
    query_embedding_model: str | None = None
    top_k: int = Field(ge=0)
    final_top_k: int | None = Field(default=None, ge=1)
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    ranking_strategy: Literal["VECTOR", "RERANK", "RERANK_FALLBACK"]
    rerank_model: str | None = None
    rerank_latency_ms: int | None = Field(default=None, ge=0)
    fallback_reason: str | None = None
    applied_threshold: float | None = None
    query_score: float | None = None
    confidence_band: str | None = None
    created_at: datetime


class RiskTraceModelCall(BaseModel):
    """模型调用的可观测性摘要，明确排除 Prompt 和结构化输出正文。"""

    provider: str | None = None
    model_name: str
    prompt_name: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    status: Literal["SUCCEEDED", "FAILED"]
    created_at: datetime


class RiskTraceNode(BaseModel):
    """LangGraph 外层节点及其检索、模型调用摘要。"""

    node_name: str
    display_name: str
    sequence_no: int = Field(ge=0)
    status: WorkflowNodeStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    error_message: str | None = None
    route: str | None = None
    finding_status: FindingStatus | None = None
    severity: RiskLevel | None = None
    retrieval_attempts: int | None = Field(default=None, ge=0, le=2)
    initial_missing_sources: list[str] = Field(default_factory=list)
    retried_sources: list[str] = Field(default_factory=list)
    final_missing_sources: list[str] = Field(default_factory=list)
    contract_evidence_count: int | None = Field(default=None, ge=0)
    policy_evidence_count: int | None = Field(default=None, ge=0)
    retrievals: list[RiskTraceRetrieval] = Field(default_factory=list)
    model_calls: list[RiskTraceModelCall] = Field(default_factory=list)


class RiskReviewTrace(BaseModel):
    """供前端展示的风险审查 Agent 执行轨迹。"""

    review_run_id: UUID
    status: ReviewStatus
    graph_name: str
    graph_version: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    total_latency_ms: int | None = Field(default=None, ge=0)
    error_message: str | None = None
    nodes: list[RiskTraceNode] = Field(default_factory=list)


RevisionAction = Literal["REPLACE", "ADD"]


class ModelContractRevisionChange(BaseModel):
    """模型只选择后端提供的检查项编码和合同条款标签。"""

    check_code: str = Field(min_length=1)
    action: RevisionAction
    target_clause_ref: str | None = None
    proposed_clause_no: str | None = Field(default=None, max_length=64)
    proposed_title: str | None = Field(default=None, max_length=255)
    proposed_content: str = Field(min_length=1)
    change_summary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class ModelContractRevisionPlan(BaseModel):
    """模型生成的整合同整改计划，后端仍会逐项校验引用和风险归属。"""

    summary: str = Field(min_length=1)
    changes: list[ModelContractRevisionChange] = Field(min_length=1, max_length=8)
    warnings: list[str] = Field(default_factory=list)


class ContractRevisionDraftChange(BaseModel):
    change_id: UUID
    finding_id: UUID
    check_code: str
    check_name: str
    finding_title: str
    action: RevisionAction
    target_clause_id: UUID | None = None
    target_clause_no: str | None = None
    target_clause_title: str | None = None
    original_content: str | None = None
    proposed_clause_no: str | None = Field(default=None, max_length=64)
    proposed_title: str | None = Field(default=None, max_length=255)
    proposed_content: str = Field(min_length=1)
    change_summary: str
    rationale: str
    warnings: list[str] = Field(default_factory=list)


class ContractRevisionDraftResponse(BaseModel):
    draft_id: UUID
    review_run_id: UUID
    contract_id: UUID
    contract_no: str
    contract_name: str
    source_document_id: UUID
    source_revision_no: int = Field(ge=1)
    target_revision_no: int = Field(ge=2)
    model_name: str
    summary: str
    changes: list[ContractRevisionDraftChange] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime


class ContractRevisionApplyChange(BaseModel):
    """用户确认采用的一项修改；未出现在请求中的草案项不会写入新版本。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    finding_id: UUID
    action: RevisionAction
    target_clause_id: UUID | None = None
    proposed_clause_no: str | None = Field(default=None, max_length=64)
    proposed_title: str | None = Field(default=None, max_length=255)
    proposed_content: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target_clause(self) -> "ContractRevisionApplyChange":
        # 替换操作必须指向来源文档中的真实条款；新增操作可指定插入位置或追加到末尾。
        if self.action == "REPLACE" and self.target_clause_id is None:
            raise ValueError("替换条款必须提供 target_clause_id。")
        return self


class ContractRevisionCreateRequest(BaseModel):
    """人工确认后的合同修订请求，client_request_id 用于避免重复创建版本。"""

    source_document_id: UUID
    client_request_id: UUID
    changes: list[ContractRevisionApplyChange] = Field(min_length=1, max_length=8)


class ContractRevisionCreateResponse(BaseModel):
    review_run_id: UUID
    contract_id: UUID
    document_id: UUID
    contract_no: str
    source_document_id: UUID
    source_revision_no: int = Field(ge=1)
    revision_no: int = Field(ge=2)
    clause_count: int = Field(ge=1)
    vectorization_job_id: UUID | None = None
    vectorization_status: Literal[
        "NOT_CONFIGURED",
        "NOT_STARTED",
        "QUEUED",
        "RUNNING",
        "RETRYING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ]
    message: str


class ModelRiskDecision(BaseModel):
    """LLM 的受控结构化输出，证据引用只能使用提示词中的 C/P 标签。"""

    status: FindingStatus
    severity: RiskLevel
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    suggestion: str | None = None
    confidence: float = Field(ge=0, le=1)
    contract_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    code: str
    message: str
