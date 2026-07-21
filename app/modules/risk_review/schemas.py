from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


ReviewStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
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
