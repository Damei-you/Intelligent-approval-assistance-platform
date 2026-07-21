from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.contract_import.schemas import (
    ImportFormat,
    ParsedClause,
    VectorizationStatus,
)


class PolicySectionInput(BaseModel):
    """制度章节请求模型，由 Pydantic 校验内容、页码和字段长度。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    section_no: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1)
    page_no: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyJsonImportRequest(BaseModel):
    """前端可编辑并确认的标准制度 JSON。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    policy_no: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    version: str = Field(default="V1.0", min_length=1, max_length=32)
    issuer: str | None = Field(default=None, max_length=255)
    effective_date: date | None = None
    sections: list[PolicySectionInput] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyFileMetadata(BaseModel):
    """PDF/TXT 预览时由表单补充的制度基础信息。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    policy_no: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    version: str = Field(default="V1.0", min_length=1, max_length=32)
    issuer: str | None = Field(default=None, max_length=255)
    effective_date: date | None = None


@dataclass(slots=True)
class ParsedPolicy:
    """解析器与持久化层之间使用的制度领域对象。"""

    policy_no: str
    title: str
    version: str
    issuer: str | None
    effective_date: date | None
    import_format: ImportFormat
    raw_text: str
    sections: list[ParsedClause]
    file_name: str | None = None
    mime_type: str | None = None
    file_hash: str | None = None
    storage_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PolicyImportResponse(BaseModel):
    document_id: UUID
    policy_no: str
    title: str
    revision_no: int
    import_format: ImportFormat
    parse_status: Literal["PARSED"] = "PARSED"
    section_count: int
    vectorized: Literal[False] = False
    vectorization_job_id: UUID | None = None
    vectorization_status: VectorizationStatus = "NOT_CONFIGURED"
    message: str = "制度依据导入成功。"


class PolicyImportPreviewResponse(BaseModel):
    preview_hash: str = Field(min_length=64, max_length=64)
    source_format: ImportFormat
    file_name: str
    file_size: int = Field(ge=0)
    mime_type: str | None
    section_count: int = Field(ge=1)
    payload: PolicyJsonImportRequest
    warnings: list[str] = Field(default_factory=list)
    persisted: Literal[False] = False
    message: str = "制度解析完成，请确认 JSON 内容后再导入。"


class PolicyImportDetail(BaseModel):
    document_id: UUID
    policy_no: str
    title: str
    version: str
    issuer: str | None
    effective_date: date | None
    revision_no: int
    is_current: bool
    file_name: str | None
    mime_type: str | None
    parse_status: str
    section_count: int
    vectorized_section_count: int
    created_at: datetime
