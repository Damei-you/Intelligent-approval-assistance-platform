from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


ContractTypeCode = Literal["PURCHASE", "SALES"]


class ImportFormat(StrEnum):
    PDF = "PDF"
    TXT = "TXT"
    JSON = "JSON"


class ClauseInput(BaseModel):
    """Pydantic 请求模型：自动校验条款字段类型、长度和必填规则。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    clause_no: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1)
    page_no: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContractJsonImportRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    contract_no: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    contract_type_code: ContractTypeCode
    counterparty: str | None = Field(default=None, max_length=255)
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", min_length=1, max_length=16)
    document_title: str | None = Field(default=None, max_length=255)
    clauses: list[ClauseInput] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class ContractFileMetadata(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    contract_no: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    contract_type_code: ContractTypeCode
    counterparty: str | None = Field(default=None, max_length=255)
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", min_length=1, max_length=16)
    document_title: str | None = Field(default=None, max_length=255)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


@dataclass(slots=True)
class ParsedClause:
    """解析器内部使用的数据类，不直接作为 HTTP 请求或响应。"""

    content: str
    clause_no: str | None = None
    title: str | None = None
    page_no: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedContract:
    contract_no: str
    name: str
    contract_type_code: str
    counterparty: str | None
    amount: Decimal | None
    currency: str
    document_title: str
    import_format: ImportFormat
    raw_text: str
    clauses: list[ParsedClause]
    file_name: str | None = None
    mime_type: str | None = None
    file_hash: str | None = None
    storage_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ContractImportResponse(BaseModel):
    contract_id: UUID
    document_id: UUID
    contract_no: str
    revision_no: int
    import_format: ImportFormat
    parse_status: Literal["PARSED"] = "PARSED"
    clause_count: int
    vectorized: Literal[False] = False
    message: str = "合同及条款导入成功，尚未进行向量化。"


class ContractImportPreviewResponse(BaseModel):
    preview_hash: str = Field(min_length=64, max_length=64)
    source_format: ImportFormat
    file_name: str
    file_size: int = Field(ge=0)
    mime_type: str | None
    clause_count: int = Field(ge=1)
    payload: ContractJsonImportRequest
    warnings: list[str] = Field(default_factory=list)
    persisted: Literal[False] = False
    message: str = "解析完成，请确认 JSON 内容后再导入。"


class ContractImportDetail(BaseModel):
    document_id: UUID
    contract_id: UUID
    contract_no: str
    contract_name: str
    contract_type_code: str
    document_title: str
    revision_no: int
    is_current: bool
    file_name: str | None
    mime_type: str | None
    parse_status: str
    clause_count: int
    vectorized_clause_count: int
    created_at: datetime


class ErrorResponse(BaseModel):
    code: str
    message: str
