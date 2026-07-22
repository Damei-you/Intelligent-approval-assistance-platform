from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.modules.approval.exceptions import ApprovalError
from app.modules.approval.repository import ApprovalRepository
from app.modules.approval.schemas import (
    ApprovalActionRequest,
    ApprovalCandidate,
    ApprovalCreateRequest,
    ApprovalDetail,
    ErrorResponse,
)


router = APIRouter(prefix="/api/v1/approvals", tags=["辅助审批"])
repository = ApprovalRepository()


@router.get(
    "/candidates",
    response_model=list[ApprovalCandidate],
    summary="查询最近完成风险审查的待审批合同",
)
async def list_approval_candidates() -> list[ApprovalCandidate]:
    """response_model 会校验列表字段，避免把内部数据库列意外返回给前端。"""

    # psycopg 是同步驱动，通过线程池执行，避免阻塞 FastAPI 的 async 事件循环。
    rows = await run_in_threadpool(repository.list_candidates)
    return [ApprovalCandidate.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=ApprovalDetail,
    status_code=status.HTTP_201_CREATED,
    summary="依据风险审查报告创建固定两级审批",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def create_approval(payload: ApprovalCreateRequest) -> ApprovalDetail:
    try:
        return await run_in_threadpool(repository.create, payload.review_run_id)
    except ApprovalError as exc:
        return _error_response(exc)


@router.get(
    "/{approval_instance_id}",
    response_model=ApprovalDetail,
    summary="查询审批流程、风险摘要和节点意见",
    responses={404: {"model": ErrorResponse}},
)
async def get_approval(approval_instance_id: UUID) -> ApprovalDetail:
    try:
        return await run_in_threadpool(repository.get_detail, approval_instance_id)
    except ApprovalError as exc:
        return _error_response(exc)


@router.post(
    "/{approval_instance_id}/actions",
    response_model=ApprovalDetail,
    summary="处理当前业务或法务审批节点",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def take_approval_action(
    approval_instance_id: UUID,
    payload: ApprovalActionRequest,
) -> ApprovalDetail:
    try:
        return await run_in_threadpool(
            repository.take_action, approval_instance_id, payload
        )
    except ApprovalError as exc:
        return _error_response(exc)


def _error_response(exc: ApprovalError) -> JSONResponse:
    """把审批业务异常转换为项目统一的 code/message 响应。"""

    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
