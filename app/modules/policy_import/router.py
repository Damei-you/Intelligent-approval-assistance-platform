from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.modules.contract_import.exceptions import (
    ContractImportError,
    DocumentParseError,
    FileTooLargeError,
    ImportRecordNotFoundError,
)
from app.modules.contract_import.schemas import DocumentVectorizationStatus, ErrorResponse
from app.modules.policy_import.repository import PolicyImportRepository
from app.modules.policy_import.schemas import (
    PolicyFileMetadata,
    PolicyImportDetail,
    PolicyImportPreviewResponse,
    PolicyImportResponse,
    PolicyJsonImportRequest,
)
from app.modules.policy_import.service import PolicyImportService
from app.modules.vectorization.service import VectorizationRepository


router = APIRouter(prefix="/api/v1/policies/imports", tags=["制度依据导入"])
service = PolicyImportService(PolicyImportRepository())
vectorization_repository = VectorizationRepository()


@router.post(
    "/preview/file",
    response_model=PolicyImportPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="解析制度 PDF、TXT 或 JSON 并返回待确认 JSON",
    responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def preview_policy_file(
    file: Annotated[UploadFile, File(description="制度 PDF、TXT 或 JSON 文件")],
    policy_no: Annotated[str | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
    version: Annotated[str, Form()] = "V1.0",
    issuer: Annotated[str | None, Form()] = None,
    effective_date: Annotated[date | None, Form()] = None,
) -> PolicyImportPreviewResponse:
    """FastAPI 接收 multipart 文件；同步解析放入线程池避免阻塞事件循环。"""

    data = await file.read(settings.max_upload_size + 1)
    if len(data) > settings.max_upload_size:
        return _error_response(
            FileTooLargeError(
                f"文件超过 {settings.max_upload_size // (1024 * 1024)} MB 限制。"
            )
        )
    try:
        metadata = _build_file_metadata(
            policy_no=policy_no,
            title=title,
            version=version,
            issuer=issuer,
            effective_date=effective_date,
        )
        return await run_in_threadpool(
            service.preview_file,
            file_name=file.filename or "",
            mime_type=file.content_type,
            data=data,
            file_metadata=metadata,
        )
    except ContractImportError as exc:
        return _error_response(exc)


@router.post(
    "/confirm/file",
    response_model=PolicyImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="确认解析结果并导入制度依据",
    responses={409: {"model": ErrorResponse}, 413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def confirm_policy_file(
    file: Annotated[UploadFile, File(description="预览时使用的原始制度文件")],
    preview_hash: Annotated[str, Form(min_length=64, max_length=64)],
    payload: Annotated[str, Form(description="用户确认或修改后的制度 JSON")],
) -> PolicyImportResponse:
    data = await file.read(settings.max_upload_size + 1)
    if len(data) > settings.max_upload_size:
        return _error_response(
            FileTooLargeError(
                f"文件超过 {settings.max_upload_size // (1024 * 1024)} MB 限制。"
            )
        )
    try:
        # model_validate_json 同时完成 JSON 反序列化和字段约束校验。
        confirmed_payload = PolicyJsonImportRequest.model_validate_json(payload)
        return await run_in_threadpool(
            service.confirm_file,
            file_name=file.filename or "",
            mime_type=file.content_type,
            data=data,
            preview_hash=preview_hash,
            payload=confirmed_payload,
        )
    except ValidationError as exc:
        return _error_response(DocumentParseError(f"确认 JSON 校验失败：{exc}"))
    except ContractImportError as exc:
        return _error_response(exc)


@router.post(
    "/json",
    response_model=PolicyImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="通过结构化 JSON 导入制度章节",
    responses={422: {"model": ErrorResponse}},
)
async def import_policy_json(payload: PolicyJsonImportRequest) -> PolicyImportResponse:
    try:
        return await run_in_threadpool(service.import_json, payload)
    except ContractImportError as exc:
        return _error_response(exc)


@router.get(
    "/{document_id}",
    response_model=PolicyImportDetail,
    summary="查询制度导入结果",
    responses={404: {"model": ErrorResponse}},
)
async def get_policy_import(document_id: UUID) -> PolicyImportDetail:
    try:
        return await run_in_threadpool(service.get_import_detail, str(document_id))
    except ContractImportError as exc:
        return _error_response(exc)


@router.get(
    "/{document_id}/vectorization",
    response_model=DocumentVectorizationStatus,
    summary="查询制度向量化进度",
    responses={404: {"model": ErrorResponse}},
)
async def get_policy_vectorization(document_id: UUID) -> DocumentVectorizationStatus:
    try:
        result = await run_in_threadpool(
            vectorization_repository.get_document_status,
            document_id,
        )
        if result.status == "NOT_STARTED" and not settings.api_key:
            return result.model_copy(update={"status": "NOT_CONFIGURED"})
        return result
    except ValueError as exc:
        return _error_response(ImportRecordNotFoundError(str(exc)))


def _build_file_metadata(
    *,
    policy_no: str | None,
    title: str | None,
    version: str,
    issuer: str | None,
    effective_date: date | None,
) -> PolicyFileMetadata | None:
    if policy_no is None or title is None:
        return None
    try:
        return PolicyFileMetadata(
            policy_no=policy_no,
            title=title,
            version=version,
            issuer=issuer,
            effective_date=effective_date,
        )
    except ValidationError as exc:
        raise DocumentParseError(f"制度元数据校验失败：{exc}") from exc


def _error_response(exc: ContractImportError) -> JSONResponse:
    """把领域异常转换成统一的 HTTP 错误结构。"""

    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
