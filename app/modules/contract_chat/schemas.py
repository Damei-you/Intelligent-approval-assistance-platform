from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


ChatIntent = Literal["AUTO", "EXPLAIN", "EVIDENCE_QUERY", "DRAFT_CLAUSE"]
ResolvedChatIntent = Literal["EXPLAIN", "EVIDENCE_QUERY", "DRAFT_CLAUSE"]
ChatMessageStatus = Literal["PENDING", "SUCCEEDED", "FAILED"]


class ChatMessageCreateRequest(BaseModel):
    """一轮用户追问；client_request_id 用于安全处理前端网络重试。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    # Field 同时限制空问题和超长输入，避免无效请求占用模型上下文。
    content: str = Field(min_length=1, max_length=2000)
    intent: ChatIntent = "AUTO"
    client_request_id: UUID


class ChatFindingSummary(BaseModel):
    check_code: str
    check_name: str
    status: Literal["PASS", "RISK", "INSUFFICIENT_INFORMATION"]
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    title: str
    description: str
    suggestion: str | None = None


class ChatCitation(BaseModel):
    chunk_id: UUID
    citation_label: str
    evidence_type: Literal["CONTRACT", "POLICY"]
    document_title: str
    clause_no: str | None = None
    title: str | None = None
    cited_text: str
    relevance_score: float | None = None


class ClauseDraft(BaseModel):
    target_clause_id: UUID
    clause_no: str | None = None
    clause_title: str | None = None
    original_text: str
    proposed_text: str
    change_summary: str
    rationale: str
    warnings: list[str] = Field(default_factory=list)


class ChatStructuredOutput(BaseModel):
    draft: ClauseDraft | None = None


class ChatMessageDetail(BaseModel):
    id: UUID
    role: Literal["USER", "ASSISTANT", "SYSTEM"]
    content: str
    intent: ChatIntent | None = None
    status: ChatMessageStatus
    structured_output: ChatStructuredOutput = Field(default_factory=ChatStructuredOutput)
    citations: list[ChatCitation] = Field(default_factory=list)
    created_at: datetime


class ChatSessionDetail(BaseModel):
    session_id: UUID
    finding_id: UUID
    review_run_id: UUID
    contract_id: UUID
    contract_document_id: UUID
    contract_no: str
    contract_name: str
    revision_no: int
    title: str
    finding: ChatFindingSummary
    messages: list[ChatMessageDetail] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ChatTurnResponse(BaseModel):
    session_id: UUID
    user_message: ChatMessageDetail
    assistant_message: ChatMessageDetail


class ModelClauseDraft(BaseModel):
    """模型只选择候选标签；原条款正文和 ID 始终由后端回填。"""

    target_clause_ref: str = Field(min_length=1)
    proposed_text: str = Field(min_length=1)
    change_summary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class ModelChatAnswer(BaseModel):
    """合同问答模型的受控结构化输出。"""

    answer: str = Field(min_length=1)
    contract_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    # 明确的证据不足状态允许模型在没有可靠引用时返回零引用回答，
    # 避免把向量 Top-K 中仅“最接近”但并不相关的候选强行当作依据。
    insufficient_evidence: bool = False
    draft: ModelClauseDraft | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str
