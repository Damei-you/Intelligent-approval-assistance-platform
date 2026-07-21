from __future__ import annotations

from uuid import UUID

from app.core.celery_app import celery_app
from app.modules.risk_review.service import run_review_safely


@celery_app.task(name="risk_review.run")
def run_risk_review_task(review_run_id: str, job_id: str) -> dict[str, object]:
    """Celery Worker 消费 Redis 中的任务并执行 LangGraph 风险审查。"""

    return run_review_safely(UUID(review_run_id), UUID(job_id))
