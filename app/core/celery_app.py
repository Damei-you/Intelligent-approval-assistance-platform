from __future__ import annotations

from celery import Celery

from app.core.config import settings


# Celery 是后台任务队列：FastAPI 只负责投递任务，Worker 从 Redis 领取并执行。
# include 显式列出任务模块，避免依赖自动扫描导致任务没有注册。
celery_app = Celery(
    "intelligent_approval_platform",
    broker=settings.redis_url,
    backend=settings.celery_result_backend,
    include=["app.modules.vectorization.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Hong_Kong",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)
