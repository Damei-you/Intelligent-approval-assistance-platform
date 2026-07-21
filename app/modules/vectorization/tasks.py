from __future__ import annotations

from uuid import UUID

from app.core.celery_app import celery_app
from app.modules.vectorization.service import (
    VectorizationRepository,
    VectorizationService,
    _safe_error_message,
)


@celery_app.task(
    bind=True,
    name="vectorization.vectorize_document",
    max_retries=3,
    acks_late=True,
)
def vectorize_document_task(self, job_id: str, document_id: str) -> int:
    """Celery Worker 执行的合同条款向量化任务。"""

    repository = VectorizationRepository()
    try:
        return VectorizationService(repository=repository).vectorize_document(
            UUID(job_id), UUID(document_id)
        )
    except Exception as exc:
        safe_message = _safe_error_message(exc)
        if self.request.retries < self.max_retries:
            repository.mark_retrying(UUID(job_id), safe_message)
            # self.retry 会把同一任务重新放回 Redis；退避时间逐次增加，避免持续冲击模型服务。
            raise self.retry(
                exc=exc,
                countdown=min(60 * (2 ** self.request.retries), 300),
            )
        repository.mark_failed(UUID(job_id), safe_message)
        raise
