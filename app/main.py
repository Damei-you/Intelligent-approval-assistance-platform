from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.modules.approval.router import router as approval_router
from app.modules.contract_chat.router import router as contract_chat_router
from app.modules.contract_import.router import router as contract_import_router
from app.modules.policy_import.router import router as policy_import_router
from app.modules.risk_review.router import router as risk_review_router


# FastAPI 应用对象负责汇总路由、中间件和自动生成的 OpenAPI 文档。
app = FastAPI(
    title="智能审批辅助平台 API",
    version="0.1.0",
    description="合同导入、RAG 风险检查、风险追问与审批辅助演示项目。",
)
# CORS 中间件允许 Vue 开发服务器从不同端口访问后端；允许来源由环境变量控制。
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(contract_import_router)
app.include_router(policy_import_router)
app.include_router(risk_review_router)
app.include_router(contract_chat_router)
app.include_router(approval_router)


@app.get("/health", tags=["系统"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
