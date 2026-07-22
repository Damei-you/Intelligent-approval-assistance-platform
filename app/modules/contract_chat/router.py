from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.modules.contract_chat.exceptions import ContractChatError
from app.modules.contract_chat.repository import ContractChatRepository
from app.modules.contract_chat.schemas import (
    ChatMessageCreateRequest,
    ChatSessionDetail,
    ChatTurnResponse,
    ErrorResponse,
)
from app.modules.contract_chat.service import ContractChatService


router = APIRouter(prefix="/api/v1", tags=["合同风险对话"])
repository = ContractChatRepository()
service = ContractChatService(repository=repository)


@router.post(
    "/risk-findings/{finding_id}/chat-sessions",
    response_model=ChatSessionDetail,
    status_code=status.HTTP_201_CREATED,
    summary="创建或恢复风险项对话",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def create_or_get_chat_session(finding_id: UUID) -> ChatSessionDetail:
    """路径参数固定风险项；response_model 防止数据库内部字段意外返回。"""

    try:
        # psycopg 和当前 LangGraph 服务均为同步调用，放入线程池避免阻塞事件循环。
        return await run_in_threadpool(service.create_or_get_session, finding_id)
    except ContractChatError as exc:
        return _error_response(exc)
    except Exception:
        return _unexpected_error_response()


@router.get(
    "/chat-sessions/{session_id}",
    response_model=ChatSessionDetail,
    summary="查询风险项对话历史",
    responses={
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_chat_session(session_id: UUID) -> ChatSessionDetail:
    try:
        return await run_in_threadpool(service.get_session, session_id)
    except ContractChatError as exc:
        return _error_response(exc)
    except Exception:
        return _unexpected_error_response()


@router.post(
    "/chat-sessions/{session_id}/messages",
    response_model=ChatTurnResponse,
    status_code=status.HTTP_201_CREATED,
    summary="就当前风险项继续询问",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def create_chat_message(
    session_id: UUID,
    payload: ChatMessageCreateRequest,
) -> ChatTurnResponse:
    """请求体携带问题、意图和幂等键；同步返回这一轮用户与助手消息。"""

    try:
        # 模型调用耗时不可预测，线程池保证其他 FastAPI async 路由仍可响应。
        return await run_in_threadpool(service.send_message, session_id, payload)
    except ContractChatError as exc:
        return _error_response(exc)
    except Exception:
        return _unexpected_error_response()


def _error_response(exc: ContractChatError) -> JSONResponse:
    """把问答业务异常转换为项目统一的 code/message HTTP 响应。"""

    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


def _unexpected_error_response() -> JSONResponse:
    """兜底隐藏数据库或框架异常细节，并保持统一错误响应结构。"""

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": "CHAT_INTERNAL_ERROR", "message": "对话服务暂时不可用。"},
    )
