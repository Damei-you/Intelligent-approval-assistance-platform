from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ApprovalStatus = Literal["IN_PROGRESS", "APPROVED", "REJECTED", "RETURNED"]
ApprovalDecision = Literal["APPROVED", "REJECTED", "RETURNED"]
ApprovalStepStatus = Literal["PENDING", "IN_PROGRESS", "COMPLETED", "SKIPPED"]


class ApprovalCandidate(BaseModel):
    """风险审查完成后可以进入人工审批的合同报告。"""

    review_run_id: UUID
    contract_id: UUID
    contract_no: str
    contract_name: str
    contract_type_code: str
    contract_document_id: UUID
    revision_no: int
    overall_risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    review_summary: str
    approval_suggestion: Literal["APPROVE", "APPROVE_AFTER_REVISION", "REJECT"]
    review_completed_at: datetime
    is_current_revision: bool
    approval_instance_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    approval_ready: bool


class ApprovalCreateRequest(BaseModel):
    """创建审批时只接收风险审查 ID，合同和版本由后端回查固定。"""

    review_run_id: UUID


class ApprovalActionRequest(BaseModel):
    """对当前审批节点执行一次人工决策。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    # 项目暂不引入用户表，因此用文本保存演示操作人；后续可替换成 approver_id。
    approver_name: str = Field(min_length=1, max_length=128)
    decision: ApprovalDecision
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_comment_for_negative_decision(self) -> "ApprovalActionRequest":
        """驳回或退回必须说明原因，确保审批历史可理解。"""

        if self.decision in {"REJECTED", "RETURNED"} and not self.comment:
            raise ValueError("驳回或退回时必须填写审批意见")
        return self


class ApprovalStepDetail(BaseModel):
    id: UUID
    step_no: int
    step_type: Literal["BUSINESS", "LEGAL"]
    step_name: str
    status: ApprovalStepStatus
    approver_name: str | None = None
    decision: ApprovalDecision | None = None
    comment: str | None = None
    handled_at: datetime | None = None


class ApprovalFindingSummary(BaseModel):
    check_code: str
    check_name: str
    status: Literal["PASS", "RISK", "INSUFFICIENT_INFORMATION"]
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    title: str
    suggestion: str | None = None


class ApprovalDetail(BaseModel):
    """审批页面所需的流程状态和风险审查摘要。"""

    approval_instance_id: UUID
    review_run_id: UUID
    contract_id: UUID
    contract_no: str
    contract_name: str
    contract_type_code: str
    contract_document_id: UUID
    revision_no: int
    status: ApprovalStatus
    current_step_no: int
    final_decision: ApprovalDecision | None = None
    overall_risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    review_summary: str
    approval_suggestion: Literal["APPROVE", "APPROVE_AFTER_REVISION", "REJECT"]
    created_at: datetime
    completed_at: datetime | None = None
    steps: list[ApprovalStepDetail] = Field(default_factory=list)
    findings: list[ApprovalFindingSummary] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    code: str
    message: str
