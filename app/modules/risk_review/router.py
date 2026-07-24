from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.modules.risk_review.exceptions import RiskReviewError
from app.modules.risk_review.repository import RiskReviewRepository
from app.modules.risk_review.schemas import (
    ContractRevisionCreateRequest,
    ContractRevisionCreateResponse,
    ContractRevisionDraftResponse,
    ErrorResponse,
    ReviewContractOption,
    RiskReviewCreateRequest,
    RiskReviewCreateResponse,
    RiskReviewDetail,
    RiskReviewTrace,
)
from app.modules.risk_review.revision_service import ContractRevisionService
from app.modules.risk_review.service import RiskReviewService


router = APIRouter(prefix="/api/v1/risk-reviews", tags=["风险审查"])
repository = RiskReviewRepository()
service = RiskReviewService(repository=repository)
revision_service = ContractRevisionService(repository=repository)


@router.get(
    "/contracts",
    response_model=list[ReviewContractOption],
    summary="查询可发起风险审查的当前合同",
)
async def list_review_contracts() -> list[ReviewContractOption]:
    """response_model 会校验并裁剪返回字段，避免意外暴露数据库内部数据。"""

    # psycopg 是同步数据库驱动，放入线程池避免阻塞 FastAPI 的 async 事件循环。
    rows = await run_in_threadpool(repository.list_contracts)
    return [ReviewContractOption.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=RiskReviewCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建四项合同风险审查任务",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def create_risk_review(
    payload: RiskReviewCreateRequest,
) -> RiskReviewCreateResponse:
    try:
        return await run_in_threadpool(service.create_review, payload.contract_id)
    except RiskReviewError as exc:
        return _error_response(exc)


@router.get(
    "/{review_run_id}",
    response_model=RiskReviewDetail,
    summary="查询风险审查进度、结论和证据",
    responses={404: {"model": ErrorResponse}},
)
async def get_risk_review(review_run_id: UUID) -> RiskReviewDetail:
    try:
        return await run_in_threadpool(repository.get_review_detail, review_run_id)
    except RiskReviewError as exc:
        return _error_response(exc)


@router.get(
    "/{review_run_id}/trace",
    response_model=RiskReviewTrace,
    summary="查询风险审查 Agent 的脱敏执行轨迹",
    responses={404: {"model": ErrorResponse}},
)
async def get_risk_review_trace(review_run_id: UUID) -> RiskReviewTrace:
    """在线程池查询同步 psycopg 记录，并由 response_model 限定可展示字段。"""

    try:
        return await run_in_threadpool(repository.get_review_trace, review_run_id)
    except RiskReviewError as exc:
        return _error_response(exc)


@router.post(
    "/{review_run_id}/revision-draft",
    response_model=ContractRevisionDraftResponse,
    summary="根据风险建议生成可编辑的合同修订草案",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def generate_contract_revision_draft(
    review_run_id: UUID,
) -> ContractRevisionDraftResponse:
    """模型调用和同步数据库读取放入线程池，避免阻塞 FastAPI 的 async 事件循环。"""

    try:
        return await run_in_threadpool(revision_service.generate_draft, review_run_id)
    except RiskReviewError as exc:
        return _error_response(exc)


@router.post(
    "/{review_run_id}/revisions",
    response_model=ContractRevisionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="确认采用修改并创建合同新修订版本",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def create_contract_revision(
    review_run_id: UUID,
    payload: ContractRevisionCreateRequest,
) -> ContractRevisionCreateResponse:
    """请求模型由 Pydantic 校验，业务层只写入用户明确采用的修改。"""

    try:
        return await run_in_threadpool(
            revision_service.create_revision,
            review_run_id,
            payload,
        )
    except RiskReviewError as exc:
        return _error_response(exc)


def _error_response(exc: RiskReviewError) -> JSONResponse:
    """把业务异常转换为项目统一的 code/message HTTP 错误结构。"""

    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
