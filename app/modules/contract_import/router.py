from __future__ import annotations

from decimal import Decimal
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
from app.modules.contract_import.repository import ContractImportRepository
from app.modules.contract_import.schemas import (
    ContractFileMetadata,
    ContractImportDetail,
    ContractImportPreviewResponse,
    ContractImportResponse,
    ContractJsonImportRequest,
    DocumentVectorizationStatus,
    ErrorResponse,
)
from app.modules.contract_import.service import ContractImportService
from app.modules.vectorization.service import VectorizationRepository


router = APIRouter(prefix="/api/v1/contracts/imports", tags=["合同导入"])
service = ContractImportService(ContractImportRepository())
vectorization_repository = VectorizationRepository()


@router.post(
    "/preview/file",
    response_model=ContractImportPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="解析 PDF、TXT 或 JSON 文件并返回待确认 JSON",
    responses={
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def preview_contract_file(
    file: Annotated[UploadFile, File(description="PDF、TXT 或 JSON 文件")],
    contract_no: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
    contract_type_code: Annotated[str | None, Form()] = None,
    counterparty: Annotated[str | None, Form()] = None,
    amount: Annotated[Decimal | None, Form(ge=0)] = None,
    currency: Annotated[str, Form()] = "CNY",
    document_title: Annotated[str | None, Form()] = None,
) -> ContractImportPreviewResponse:
    """解析上传文件并返回 JSON 预览，此接口不会写数据库。"""

    # UploadFile 来自 multipart/form-data。多读取 1 字节可以准确判断是否超过上限，
    # 同时避免没有边界地把超大文件读入内存。
    data = await file.read(settings.max_upload_size + 1)
    if len(data) > settings.max_upload_size:
        return _import_error_response(
            FileTooLargeError(
                f"文件超过 {settings.max_upload_size // (1024 * 1024)} MB 限制。"
            )
        )

    try:
        file_metadata = _build_file_metadata(
            contract_no=contract_no,
            name=name,
            contract_type_code=contract_type_code,
            counterparty=counterparty,
            amount=amount,
            currency=currency,
            document_title=document_title,
        )
        # PDF 解析和同步 psycopg 调用属于阻塞操作，放入线程池可避免阻塞 FastAPI 事件循环。
        return await run_in_threadpool(
            service.preview_file,
            file_name=file.filename or "",
            mime_type=file.content_type,
            data=data,
            file_metadata=file_metadata,
        )
    except ContractImportError as exc:
        return _import_error_response(exc)


@router.post(
    "/confirm/file",
    response_model=ContractImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="确认解析结果并导入合同及条款",
    responses={
        409: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def confirm_contract_file(
    file: Annotated[UploadFile, File(description="预览时使用的原始文件")],
    preview_hash: Annotated[str, Form(min_length=64, max_length=64)],
    payload: Annotated[str, Form(description="用户确认或修改后的合同 JSON")],
) -> ContractImportResponse:
    """校验用户确认的 JSON 和原文件，校验通过后才执行正式入库。"""

    data = await file.read(settings.max_upload_size + 1)
    if len(data) > settings.max_upload_size:
        return _import_error_response(
            FileTooLargeError(
                f"文件超过 {settings.max_upload_size // (1024 * 1024)} MB 限制。"
            )
        )
    try:
        # model_validate_json 会同时完成 JSON 解析和 Pydantic 字段校验。
        confirmed_payload = ContractJsonImportRequest.model_validate_json(payload)
        return await run_in_threadpool(
            service.confirm_file,
            file_name=file.filename or "",
            mime_type=file.content_type,
            data=data,
            preview_hash=preview_hash,
            payload=confirmed_payload,
        )
    except ValidationError as exc:
        return _import_error_response(
            DocumentParseError(f"确认 JSON 校验失败：{exc}")
        )
    except ContractImportError as exc:
        return _import_error_response(exc)


@router.post(
    "/json",
    response_model=ContractImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="通过结构化 JSON 导入合同条款",
    responses={422: {"model": ErrorResponse}},
)
async def import_contract_json(
    payload: ContractJsonImportRequest,
) -> ContractImportResponse:
    try:
        return await run_in_threadpool(service.import_json, payload)
    except ContractImportError as exc:
        return _import_error_response(exc)


@router.get(
    "/{document_id}",
    response_model=ContractImportDetail,
    summary="查询合同导入结果",
    responses={404: {"model": ErrorResponse}},
)
async def get_contract_import(document_id: UUID) -> ContractImportDetail:
    try:
        return await run_in_threadpool(service.get_import_detail, str(document_id))
    except ContractImportError as exc:
        return _import_error_response(exc)


@router.get(
    "/{document_id}/vectorization",
    response_model=DocumentVectorizationStatus,
    summary="查询文档向量化进度",
    responses={404: {"model": ErrorResponse}},
)
async def get_document_vectorization(
    document_id: UUID,
) -> DocumentVectorizationStatus:
    """返回最新 Celery 任务状态，以及已写入 pgvector 的条款数量。"""

    try:
        # psycopg 是同步驱动，因此在线程池中执行，避免数据库查询阻塞 FastAPI 事件循环。
        result = await run_in_threadpool(
            vectorization_repository.get_document_status,
            document_id,
        )
        if result.status == "NOT_STARTED" and not settings.api_key:
            return result.model_copy(update={"status": "NOT_CONFIGURED"})
        return result
    except ValueError as exc:
        return _import_error_response(ImportRecordNotFoundError(str(exc)))


def _build_file_metadata(
    *,
    contract_no: str | None,
    name: str | None,
    contract_type_code: str | None,
    counterparty: str | None,
    amount: Decimal | None,
    currency: str,
    document_title: str | None,
) -> ContractFileMetadata | None:
    if contract_no is None or name is None or contract_type_code is None:
        return None
    try:
        return ContractFileMetadata(
            contract_no=contract_no,
            name=name,
            contract_type_code=contract_type_code,
            counterparty=counterparty,
            amount=amount,
            currency=currency,
            document_title=document_title,
        )
    except ValidationError as exc:
        raise DocumentParseError(f"合同元数据校验失败：{exc}") from exc


def _import_error_response(exc: ContractImportError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
