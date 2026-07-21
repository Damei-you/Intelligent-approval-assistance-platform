from __future__ import annotations

import math
from itertools import batched
from typing import Any
from uuid import UUID, uuid4

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr
from psycopg.types.json import Jsonb

from app.core.config import settings
from app.core.database import open_connection
from app.modules.contract_import.schemas import DocumentVectorizationStatus


class VectorizationConfigurationError(RuntimeError):
    """向量模型配置缺失或维度不符合数据库约束。"""


class DashScopeEmbeddingProvider:
    """通过 LangChain 调用百炼的 OpenAI 兼容 Embedding 接口。"""

    def __init__(self) -> None:
        if not settings.api_key:
            raise VectorizationConfigurationError(
                "未配置 api-key 环境变量，无法执行向量化。"
            )
        if settings.embedding_dimension != 1536:
            raise VectorizationConfigurationError(
                "当前数据库列为 VECTOR(1536)，EMBEDDING_DIMENSION 必须为 1536。"
            )

        # SecretStr 会在对象输出时隐藏 API Key；base_url 可切换为工作空间专属地址。
        self.client = OpenAIEmbeddings(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimension,
            api_key=SecretStr(settings.api_key),
            base_url=settings.dashscope_base_url,
            chunk_size=settings.embedding_batch_size,
            check_embedding_ctx_length=False,
            max_retries=2,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """把一批条款文本转换为固定 1536 维浮点向量。"""

        vectors = self.client.embed_documents(texts)
        for vector in vectors:
            if len(vector) != settings.embedding_dimension:
                raise ValueError(
                    f"模型返回 {len(vector)} 维向量，预期为 {settings.embedding_dimension} 维。"
                )
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("模型返回的向量包含非有限数值。")
        return vectors


class VectorizationRepository:
    """持久化 Celery 任务状态、读取条款并写入 pgvector。"""

    def create_job(self, document_id: UUID) -> dict[str, Any]:
        job_id = uuid4()
        celery_task_id = str(uuid4())
        with open_connection() as connection:
            with connection.transaction():
                document = connection.execute(
                    "SELECT id FROM documents WHERE id = %s",
                    (document_id,),
                ).fetchone()
                if document is None:
                    raise ValueError(f"文档 {document_id} 不存在。")
                connection.execute(
                    """
                    INSERT INTO async_jobs (
                        id, celery_task_id, task_type, resource_type,
                        resource_id, status, progress
                    )
                    VALUES (%s, %s, 'DOCUMENT_EMBEDDING', 'DOCUMENT', %s, 'QUEUED', 0)
                    """,
                    (job_id, celery_task_id, document_id),
                )
        return {"job_id": job_id, "celery_task_id": celery_task_id}

    def list_pending_chunks(self, document_id: UUID) -> list[dict[str, Any]]:
        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, chunk_type, clause_no, title, content
                FROM document_chunks
                WHERE document_id = %s AND embedding IS NULL
                ORDER BY chunk_index
                """,
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_chunk_counts(self, document_id: UUID) -> tuple[int, int]:
        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*)::INTEGER AS total,
                    COUNT(embedding)::INTEGER AS vectorized
                FROM document_chunks
                WHERE document_id = %s
                """,
                (document_id,),
            ).fetchone()
        return row["total"], row["vectorized"]

    def update_embeddings(
        self,
        items: list[tuple[UUID, list[float]]],
        model_name: str,
    ) -> None:
        # pgvector 接受形如 "[0.1,0.2]" 的文本并通过 ::vector 转换；
        # 参数仍由 psycopg 绑定，不拼接用户输入。
        rows = [(_to_vector_literal(vector), model_name, chunk_id) for chunk_id, vector in items]
        with open_connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        UPDATE document_chunks
                        SET embedding = %s::vector,
                            embedding_model = %s
                        WHERE id = %s
                        """,
                        rows,
                    )

    def mark_running(self, job_id: UUID) -> None:
        self._execute_job_update(
            """
            UPDATE async_jobs
            SET status = 'RUNNING', progress = GREATEST(progress, 1),
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP), error_message = NULL
            WHERE id = %s
            """,
            (job_id,),
        )

    def mark_progress(self, job_id: UUID, progress: int) -> None:
        self._execute_job_update(
            "UPDATE async_jobs SET status = 'RUNNING', progress = %s WHERE id = %s",
            (progress, job_id),
        )

    def mark_retrying(self, job_id: UUID, error_message: str) -> None:
        self._execute_job_update(
            """
            UPDATE async_jobs
            SET status = 'RETRYING', retry_count = retry_count + 1,
                error_message = %s
            WHERE id = %s
            """,
            (error_message, job_id),
        )

    def mark_failed(self, job_id: UUID, error_message: str) -> None:
        self._execute_job_update(
            """
            UPDATE async_jobs
            SET status = 'FAILED', error_message = %s,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (error_message, job_id),
        )

    def mark_succeeded(self, job_id: UUID, document_id: UUID, count: int) -> None:
        with open_connection() as connection:
            with connection.transaction():
                # JSONB 元数据记录本次模型配置，便于以后判断是否需要重新向量化。
                connection.execute(
                    """
                    UPDATE documents
                    SET metadata = metadata || %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "vectorized": True,
                                "embedding_model": settings.embedding_model,
                                "embedding_dimension": settings.embedding_dimension,
                            }
                        ),
                        document_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE async_jobs
                    SET status = 'SUCCEEDED', progress = 100,
                        result_summary = %s, completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (Jsonb({"vectorized_chunk_count": count}), job_id),
                )

    def get_document_status(self, document_id: UUID) -> DocumentVectorizationStatus:
        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    d.id AS document_id,
                    job.id AS job_id,
                    COALESCE(job.status, 'NOT_STARTED') AS status,
                    COALESCE(job.progress, 0)::INTEGER AS progress,
                    COUNT(dc.id)::INTEGER AS clause_count,
                    COUNT(dc.embedding)::INTEGER AS vectorized_clause_count,
                    MAX(dc.embedding_model) AS model_name,
                    job.error_message
                FROM documents d
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                LEFT JOIN LATERAL (
                    SELECT id, status, progress, error_message
                    FROM async_jobs
                    WHERE resource_type = 'DOCUMENT'
                      AND resource_id = d.id
                      AND task_type = 'DOCUMENT_EMBEDDING'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) job ON TRUE
                WHERE d.id = %s
                GROUP BY d.id, job.id, job.status, job.progress, job.error_message
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"文档 {document_id} 不存在。")
        data = dict(row)
        data["dimension"] = settings.embedding_dimension if data["model_name"] else None
        return DocumentVectorizationStatus.model_validate(data)

    def _execute_job_update(self, sql: str, parameters: tuple[Any, ...]) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(sql, parameters)


class VectorizationService:
    """按批次生成向量，并持续更新可查询的任务进度。"""

    def __init__(
        self,
        repository: VectorizationRepository | None = None,
        provider: DashScopeEmbeddingProvider | None = None,
    ) -> None:
        self.repository = repository or VectorizationRepository()
        self.provider = provider or DashScopeEmbeddingProvider()

    def vectorize_document(self, job_id: UUID, document_id: UUID) -> int:
        self.repository.mark_running(job_id)
        total, already_vectorized = self.repository.get_chunk_counts(document_id)
        pending = self.repository.list_pending_chunks(document_id)
        if total == 0:
            raise ValueError("文档没有可向量化的条款。")

        completed = already_vectorized
        for batch in batched(pending, settings.embedding_batch_size):
            batch_list = list(batch)
            texts = [_build_embedding_text(chunk) for chunk in batch_list]
            vectors = self.provider.embed_documents(texts)
            self.repository.update_embeddings(
                [(chunk["id"], vector) for chunk, vector in zip(batch_list, vectors, strict=True)],
                settings.embedding_model,
            )
            completed += len(batch_list)
            self.repository.mark_progress(job_id, min(99, int(completed / total * 100)))

        self.repository.mark_succeeded(job_id, document_id, completed)
        return completed


def enqueue_document_vectorization(document_id: UUID) -> dict[str, Any]:
    """创建持久化任务记录，并把任务 ID 投递到 Redis。"""

    if not settings.api_key:
        return {"job_id": None, "status": "NOT_CONFIGURED"}

    repository = VectorizationRepository()
    job: dict[str, Any] | None = None
    try:
        job = repository.create_job(document_id)
        # 延迟导入避免 Celery 加载任务模块时产生循环导入。
        from app.modules.vectorization.tasks import vectorize_document_task

        vectorize_document_task.apply_async(
            args=[str(job["job_id"]), str(document_id)],
            task_id=job["celery_task_id"],
        )
    except Exception as exc:
        # 合同导入事务已经提交，任务创建或 Redis 投递失败不能再让接口整体报错。
        # 如果任务记录已经创建，则写入 FAILED；创建记录本身失败时只返回空任务 ID。
        if job is not None:
            try:
                repository.mark_failed(job["job_id"], _safe_error_message(exc))
            except Exception:
                # 状态回写失败也不能改变已经完成的合同导入结果。
                pass
            return {"job_id": job["job_id"], "status": "FAILED"}
        return {"job_id": None, "status": "FAILED"}
    return {"job_id": job["job_id"], "status": "QUEUED"}


def _build_embedding_text(chunk: dict[str, Any]) -> str:
    label = "制度章节" if chunk.get("chunk_type") == "POLICY_SECTION" else "合同条款"
    parts = []
    if chunk.get("clause_no"):
        parts.append(f"{label}编号：{chunk['clause_no']}")
    if chunk.get("title"):
        parts.append(f"{label}标题：{chunk['title']}")
    parts.append(f"{label}内容：{chunk['content']}")
    return "\n".join(parts)


def _to_vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".10g") for value in vector) + "]"


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    if settings.api_key:
        message = message.replace(settings.api_key, "***")
    return message[:2000]
