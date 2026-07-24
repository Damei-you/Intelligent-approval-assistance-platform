from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.modules.risk_review.exceptions import RiskReviewError
from app.modules.risk_review.repository import RiskReviewRepository
from app.modules.risk_review.schemas import (
    ErrorResponse,
    ReviewContractOption,
    RiskReviewCreateRequest,
    RiskReviewCreateResponse,
    RiskReviewDetail,
    RiskReviewTrace,
)
from app.modules.risk_review.service import RiskReviewService


router = APIRouter(prefix="/api/v1/risk-reviews", tags=["风险审查"])
repository = RiskReviewRepository()
service = RiskReviewService(repository=repository)


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


def _error_response(exc: RiskReviewError) -> JSONResponse:
    """把业务异常转换为项目统一的 code/message HTTP 错误结构。"""

    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
