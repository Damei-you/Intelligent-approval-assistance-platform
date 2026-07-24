from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.core.database import open_connection
from app.modules.risk_review.exceptions import RiskReviewError
from app.modules.risk_review.schemas import (
    ModelRiskDecision,
    RiskReviewDetail,
    RiskReviewTrace,
)


REVIEW_CHECK_CODES = (
    "PAYMENT_TERMS",
    "WARRANTY",
    "BREACH_LIABILITY",
    "DISPUTE_RESOLUTION",
)

TRACE_NODE_LABELS = {
    "load_context": "加载合同上下文",
    "payment_terms": "付款条款检查",
    "warranty": "质保条款检查",
    "breach_liability": "违约责任检查",
    "dispute_resolution": "争议解决检查",
    "aggregate": "汇总审查结论",
}


class RiskReviewRepository:
    """风险审查所需的查询、证据链和任务状态持久化。"""

    def list_contracts(self) -> list[dict[str, Any]]:
        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.id AS contract_id,
                    c.contract_no,
                    c.name AS contract_name,
                    ct.code AS contract_type_code,
                    d.id AS document_id,
                    d.revision_no,
                    COUNT(dc.id)::INTEGER AS clause_count,
                    COUNT(dc.embedding)::INTEGER AS vectorized_clause_count,
                    (COUNT(dc.id) > 0 AND COUNT(dc.id) = COUNT(dc.embedding)) AS review_ready,
                    latest_review.id AS latest_review_run_id,
                    latest_review.status AS latest_review_status,
                    latest_review.created_at AS latest_review_created_at,
                    (latest_review.contract_document_id = d.id) AS latest_review_is_current
                FROM contracts c
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN documents d
                  ON d.contract_id = c.id
                 AND d.document_type = 'CONTRACT'
                 AND d.is_current = TRUE
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                LEFT JOIN LATERAL (
                    SELECT rr.id, rr.status, rr.created_at, rr.contract_document_id
                    FROM review_runs rr
                    WHERE rr.contract_id = c.id
                    ORDER BY rr.created_at DESC
                    LIMIT 1
                ) latest_review ON TRUE
                GROUP BY c.id, ct.code, d.id, latest_review.id,
                         latest_review.status, latest_review.created_at,
                         latest_review.contract_document_id
                ORDER BY c.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_review(self, contract_id: UUID) -> dict[str, Any]:
        """固定当前合同版本并在同一事务中创建审查、工作流和异步任务。"""

        review_run_id = uuid4()
        workflow_run_id = uuid4()
        job_id = uuid4()
        celery_task_id = str(uuid4())
        with open_connection() as connection:
            with connection.transaction():
                contract = connection.execute(
                    """
                    SELECT
                        c.id,
                        d.id AS document_id,
                        counts.chunk_count,
                        counts.vectorized_count
                    FROM contracts c
                    JOIN documents d
                      ON d.contract_id = c.id
                     AND d.document_type = 'CONTRACT'
                     AND d.is_current = TRUE
                    JOIN LATERAL (
                        SELECT COUNT(*)::INTEGER AS chunk_count,
                               COUNT(embedding)::INTEGER AS vectorized_count
                        FROM document_chunks
                        WHERE document_id = d.id
                    ) counts ON TRUE
                    WHERE c.id = %s
                    FOR UPDATE OF c, d
                    """,
                    (contract_id,),
                ).fetchone()
                if contract is None:
                    raise RiskReviewError("CONTRACT_NOT_FOUND", "未找到可审查的当前合同版本。", 404)
                if contract["chunk_count"] == 0 or contract["chunk_count"] != contract["vectorized_count"]:
                    raise RiskReviewError(
                        "CONTRACT_NOT_VECTORIZED",
                        "当前合同条款尚未全部向量化，请等待向量化完成后再审查。",
                    )

                policy_count = connection.execute(
                    """
                    SELECT COUNT(dc.id)::INTEGER AS chunk_count
                    FROM documents d
                    JOIN document_chunks dc ON dc.document_id = d.id
                    WHERE d.document_type = 'POLICY'
                      AND d.is_current = TRUE
                      AND dc.chunk_type = 'POLICY_SECTION'
                      AND dc.embedding IS NOT NULL
                    """
                ).fetchone()["chunk_count"]
                if policy_count == 0:
                    raise RiskReviewError(
                        "POLICY_NOT_VECTORIZED",
                        "没有可检索的当前制度向量，请先导入制度并等待向量化完成。",
                    )

                check_count = connection.execute(
                    """
                    SELECT COUNT(*)::INTEGER AS check_count
                    FROM review_check_items rci
                    JOIN review_check_item_scopes scope ON scope.check_item_id = rci.id
                    JOIN contracts c ON c.contract_type_id = scope.contract_type_id
                    WHERE c.id = %s AND rci.enabled = TRUE AND rci.code = ANY(%s)
                    """,
                    (contract_id, list(REVIEW_CHECK_CODES)),
                ).fetchone()["check_count"]
                if check_count != len(REVIEW_CHECK_CODES):
                    raise RiskReviewError(
                        "REVIEW_CHECKS_NOT_READY",
                        "四项风险检查配置不完整，请先执行风险审查数据库迁移。",
                    )

                connection.execute(
                    """
                    INSERT INTO review_runs (id, contract_id, contract_document_id, status)
                    VALUES (%s, %s, %s, 'PENDING')
                    """,
                    (review_run_id, contract_id, contract["document_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_runs (
                        id, run_type, review_run_id, graph_name, graph_version, status
                    ) VALUES (%s, 'RISK_REVIEW', %s, 'contract_risk_review', '1.0', 'PENDING')
                    """,
                    (workflow_run_id, review_run_id),
                )
                connection.execute(
                    """
                    INSERT INTO async_jobs (
                        id, celery_task_id, task_type, resource_type, resource_id, status
                    ) VALUES (%s, %s, 'RISK_REVIEW', 'REVIEW_RUN', %s, 'QUEUED')
                    """,
                    (job_id, celery_task_id, review_run_id),
                )
                connection.execute(
                    "UPDATE contracts SET status = 'REVIEWING' WHERE id = %s",
                    (contract_id,),
                )
        return {
            "review_run_id": review_run_id,
            "workflow_run_id": workflow_run_id,
            "job_id": job_id,
            "celery_task_id": celery_task_id,
            "contract_document_id": contract["document_id"],
        }

    def mark_running(self, review_run_id: UUID, job_id: UUID) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE review_runs
                    SET status = 'RUNNING', started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        error_message = NULL
                    WHERE id = %s
                    """,
                    (review_run_id,),
                )
                connection.execute(
                    """
                    UPDATE workflow_runs
                    SET status = 'RUNNING', started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        error_message = NULL
                    WHERE review_run_id = %s
                    """,
                    (review_run_id,),
                )
                connection.execute(
                    """
                    UPDATE async_jobs
                    SET status = 'RUNNING', progress = 2,
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP), error_message = NULL
                    WHERE id = %s
                    """,
                    (job_id,),
                )

    def get_review_context(self, review_run_id: UUID) -> dict[str, Any]:
        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    rr.id AS review_run_id,
                    rr.contract_id,
                    rr.contract_document_id,
                    c.contract_no,
                    c.name AS contract_name,
                    ct.code AS contract_type_code,
                    wr.id AS workflow_run_id
                FROM review_runs rr
                JOIN contracts c ON c.id = rr.contract_id
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN workflow_runs wr ON wr.review_run_id = rr.id
                WHERE rr.id = %s
                """,
                (review_run_id,),
            ).fetchone()
        if row is None:
            raise RiskReviewError("REVIEW_NOT_FOUND", "风险审查任务不存在。", 404)
        return dict(row)

    def get_check_item(self, contract_id: UUID, code: str) -> dict[str, Any]:
        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT rci.id, rci.code, rci.name, rci.description,
                       rci.prompt_template, rci.default_severity
                FROM review_check_items rci
                JOIN review_check_item_scopes scope ON scope.check_item_id = rci.id
                JOIN contracts c ON c.contract_type_id = scope.contract_type_id
                WHERE c.id = %s AND rci.code = %s AND rci.enabled = TRUE
                """,
                (contract_id, code),
            ).fetchone()
        if row is None:
            raise RiskReviewError("CHECK_ITEM_NOT_FOUND", f"未找到启用的检查项 {code}。")
        return dict(row)

    def create_node_run(
        self,
        workflow_run_id: UUID,
        node_name: str,
        sequence_no: int,
        input_data: dict[str, Any],
    ) -> UUID:
        node_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO workflow_node_runs (
                        id, workflow_run_id, node_name, sequence_no, status,
                        input_data, started_at
                    ) VALUES (%s, %s, %s, %s, 'RUNNING', %s, CURRENT_TIMESTAMP)
                    """,
                    (node_run_id, workflow_run_id, node_name, sequence_no, Jsonb(input_data)),
                )
        return node_run_id

    def finish_node(self, node_run_id: UUID, output_data: dict[str, Any]) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workflow_node_runs
                    SET status = 'SUCCEEDED', output_data = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        latency_ms = (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::INTEGER
                    WHERE id = %s
                    """,
                    (Jsonb(output_data), node_run_id),
                )

    def search_contract_chunks(
        self, document_id: UUID, query_vector: list[float], top_k: int = 3
    ) -> list[dict[str, Any]]:
        vector_literal = _to_vector_literal(query_vector)
        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT dc.id, dc.document_id, d.title AS document_title,
                       dc.clause_no, dc.title, dc.content,
                       (1 - (dc.embedding <=> %s::vector))::DOUBLE PRECISION AS similarity_score
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.document_id = %s
                  AND dc.chunk_type = 'CONTRACT_CLAUSE'
                  AND dc.embedding IS NOT NULL
                ORDER BY dc.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, document_id, vector_literal, top_k),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_policy_chunks(
        self, query_vector: list[float], top_k: int = 5
    ) -> list[dict[str, Any]]:
        vector_literal = _to_vector_literal(query_vector)
        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT dc.id, dc.document_id, d.title AS document_title,
                       dc.clause_no, dc.title, dc.content,
                       (1 - (dc.embedding <=> %s::vector))::DOUBLE PRECISION AS similarity_score
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE d.document_type = 'POLICY'
                  AND d.is_current = TRUE
                  AND dc.chunk_type = 'POLICY_SECTION'
                  AND dc.embedding IS NOT NULL
                ORDER BY dc.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, vector_literal, top_k),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_retrieval(
        self,
        node_run_id: UUID,
        query_text: str,
        filters: dict[str, Any],
        hits: list[dict[str, Any]],
        model_name: str,
        *,
        final_top_k: int | None = None,
        ranking_strategy: str = "VECTOR",
        rerank_model: str | None = None,
        rerank_latency_ms: int | None = None,
        rerank_error: str | None = None,
    ) -> None:
        """保存向量召回与可选重排序结果，区分候选集和实际模型上下文。"""

        retrieval_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO retrieval_runs (
                        id, node_run_id, query_text, query_embedding_model, filters, top_k,
                        final_top_k, ranking_strategy, rerank_model,
                        rerank_latency_ms, rerank_error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        retrieval_run_id,
                        node_run_id,
                        query_text,
                        model_name,
                        Jsonb(filters),
                        max(1, len(hits)),
                        final_top_k,
                        ranking_strategy,
                        rerank_model,
                        rerank_latency_ms,
                        rerank_error,
                    ),
                )
                if hits:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO retrieval_hits (
                                retrieval_run_id, chunk_id, rank_no,
                                similarity_score, rerank_rank_no, rerank_score,
                                selected_for_context
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    retrieval_run_id,
                                    hit["id"],
                                    hit.get("vector_rank_no", rank),
                                    max(-1, min(1, hit["similarity_score"])),
                                    hit.get("rerank_rank_no"),
                                    hit.get("rerank_score"),
                                    hit.get("selected_for_context", True),
                                )
                                for rank, hit in enumerate(hits, 1)
                            ],
                        )

    def record_llm_call(
        self,
        node_run_id: UUID,
        model_name: str,
        check_code: str,
        output_data: dict[str, Any],
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int | None = None,
    ) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO llm_calls (
                        node_run_id, provider, model_name, prompt_name,
                        input_summary, output_data, input_tokens, output_tokens,
                        latency_ms, status
                    ) VALUES (
                        %s, 'DASHSCOPE', %s, 'risk_review_v1', %s, %s,
                        %s, %s, %s, 'SUCCEEDED'
                    )
                    """,
                    (
                        node_run_id,
                        model_name,
                        Jsonb({"check_code": check_code}),
                        Jsonb(output_data),
                        input_tokens,
                        output_tokens,
                        latency_ms,
                    ),
                )

    def save_finding(
        self,
        review_run_id: UUID,
        check_item_id: UUID,
        decision: ModelRiskDecision,
        contract_evidence: list[dict[str, Any]],
        policy_evidence: list[dict[str, Any]],
    ) -> UUID:
        finding_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    INSERT INTO risk_findings (
                        id, review_run_id, check_item_id, status, severity,
                        title, description, suggestion, confidence, structured_output
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (review_run_id, check_item_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        severity = EXCLUDED.severity,
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        suggestion = EXCLUDED.suggestion,
                        confidence = EXCLUDED.confidence,
                        structured_output = EXCLUDED.structured_output
                    RETURNING id
                    """,
                    (
                        finding_id,
                        review_run_id,
                        check_item_id,
                        decision.status,
                        decision.severity,
                        decision.title,
                        decision.description,
                        decision.suggestion,
                        decision.confidence,
                        Jsonb(decision.model_dump()),
                    ),
                ).fetchone()
                finding_id = row["id"]
                connection.execute("DELETE FROM finding_evidence WHERE finding_id = %s", (finding_id,))
                evidence_rows = []
                for evidence_type, chunks in (
                    ("CONTRACT", contract_evidence),
                    ("POLICY", policy_evidence),
                ):
                    evidence_rows.extend(
                        (
                            finding_id,
                            chunk["id"],
                            evidence_type,
                            chunk["similarity_score"],
                            chunk["content"],
                            index,
                        )
                        for index, chunk in enumerate(chunks, 1)
                    )
                if evidence_rows:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO finding_evidence (
                                finding_id, chunk_id, evidence_type,
                                relevance_score, cited_text, sort_order
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            evidence_rows,
                        )
        return finding_id

    def update_progress(self, job_id: UUID, progress: int) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE async_jobs SET progress = GREATEST(progress, %s) WHERE id = %s",
                    (progress, job_id),
                )

    def update_check_progress(self, job_id: UUID, review_run_id: UUID) -> None:
        """按已落库检查项数量计算并行进度，完成顺序不会造成进度倒退。"""

        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE async_jobs
                    SET progress = GREATEST(
                        progress,
                        LEAST(
                            85,
                            5 + 20 * (
                                SELECT COUNT(*)::INTEGER
                                FROM risk_findings
                                WHERE review_run_id = %s
                            )
                        )
                    )
                    WHERE id = %s
                    """,
                    (review_run_id, job_id),
                )

    def complete_review(self, review_run_id: UUID, job_id: UUID) -> dict[str, Any]:
        with open_connection() as connection:
            with connection.transaction():
                findings = connection.execute(
                    """
                    SELECT rf.status, rf.severity, rci.name
                    FROM risk_findings rf
                    JOIN review_check_items rci ON rci.id = rf.check_item_id
                    WHERE rf.review_run_id = %s
                    """,
                    (review_run_id,),
                ).fetchall()
                risk_findings = [row for row in findings if row["status"] == "RISK"]
                insufficient = [
                    row for row in findings if row["status"] == "INSUFFICIENT_INFORMATION"
                ]
                if any(row["severity"] == "HIGH" for row in risk_findings):
                    overall = "HIGH"
                elif risk_findings or insufficient:
                    overall = "MEDIUM"
                else:
                    overall = "LOW"
                suggestion = "APPROVE" if overall == "LOW" else "APPROVE_AFTER_REVISION"
                summary = (
                    f"已完成付款、质保、违约责任和争议解决四项检查；"
                    f"发现 {len(risk_findings)} 项风险，{len(insufficient)} 项信息不足。"
                )
                connection.execute(
                    """
                    UPDATE review_runs
                    SET status = 'SUCCEEDED', overall_risk_level = %s, summary = %s,
                        approval_suggestion = %s, completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (overall, summary, suggestion, review_run_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_runs
                    SET status = 'SUCCEEDED', state_snapshot = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE review_run_id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "overall_risk_level": overall,
                                "risk_count": len(risk_findings),
                                "insufficient_count": len(insufficient),
                            }
                        ),
                        review_run_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE async_jobs
                    SET status = 'SUCCEEDED', progress = 100, result_summary = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (Jsonb({"overall_risk_level": overall}), job_id),
                )
                connection.execute(
                    """
                    UPDATE contracts SET status = 'READY'
                    WHERE id = (SELECT contract_id FROM review_runs WHERE id = %s)
                    """,
                    (review_run_id,),
                )
        return {"overall_risk_level": overall, "approval_suggestion": suggestion}

    def fail_review(self, review_run_id: UUID, job_id: UUID, message: str) -> None:
        with open_connection() as connection:
            with connection.transaction():
                # Worker 异常退出时，正在执行的节点不能永久停留在 RUNNING。
                connection.execute(
                    """
                    UPDATE workflow_node_runs
                    SET status = 'FAILED', error_message = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        latency_ms = (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::INTEGER
                    WHERE workflow_run_id = (
                        SELECT id FROM workflow_runs WHERE review_run_id = %s
                    ) AND status = 'RUNNING'
                    """,
                    (message, review_run_id),
                )
                connection.execute(
                    """
                    UPDATE review_runs SET status = 'FAILED', error_message = %s,
                        completed_at = CURRENT_TIMESTAMP WHERE id = %s
                    """,
                    (message, review_run_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_runs SET status = 'FAILED', error_message = %s,
                        completed_at = CURRENT_TIMESTAMP WHERE review_run_id = %s
                    """,
                    (message, review_run_id),
                )
                connection.execute(
                    """
                    UPDATE async_jobs SET status = 'FAILED', error_message = %s,
                        completed_at = CURRENT_TIMESTAMP WHERE id = %s
                    """,
                    (message, job_id),
                )
                connection.execute(
                    """
                    UPDATE contracts SET status = 'READY'
                    WHERE id = (SELECT contract_id FROM review_runs WHERE id = %s)
                    """,
                    (review_run_id,),
                )

    def get_revision_context(self, review_run_id: UUID) -> dict[str, Any]:
        """读取生成和确认合同修订所需的固定审查版本、风险项与证据。"""

        with open_connection() as connection:
            review = connection.execute(
                """
                SELECT
                    rr.id AS review_run_id,
                    rr.status AS review_status,
                    rr.contract_id,
                    rr.contract_document_id AS source_document_id,
                    c.contract_no,
                    c.name AS contract_name,
                    ct.code AS contract_type_code,
                    c.counterparty,
                    c.amount,
                    c.currency,
                    source_document.title AS document_title,
                    source_document.revision_no AS source_revision_no,
                    source_document.metadata AS source_metadata,
                    current_document.id AS current_document_id
                FROM review_runs rr
                JOIN contracts c ON c.id = rr.contract_id
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN documents source_document
                  ON source_document.id = rr.contract_document_id
                 AND source_document.document_type = 'CONTRACT'
                LEFT JOIN documents current_document
                  ON current_document.contract_id = c.id
                 AND current_document.document_type = 'CONTRACT'
                 AND current_document.is_current = TRUE
                WHERE rr.id = %s
                """,
                (review_run_id,),
            ).fetchone()
            if review is None:
                raise RiskReviewError("REVIEW_NOT_FOUND", "风险审查任务不存在。", 404)

            clause_rows = connection.execute(
                """
                SELECT
                    dc.id,
                    dc.chunk_index,
                    dc.clause_no,
                    dc.title,
                    dc.content,
                    dc.page_no,
                    dc.metadata
                FROM document_chunks dc
                WHERE dc.document_id = %s
                  AND dc.chunk_type = 'CONTRACT_CLAUSE'
                ORDER BY dc.chunk_index
                """,
                (review["source_document_id"],),
            ).fetchall()
            finding_rows = connection.execute(
                """
                SELECT
                    rf.id,
                    rci.code AS check_code,
                    rci.name AS check_name,
                    rf.status,
                    rf.severity,
                    rf.title,
                    rf.description,
                    rf.suggestion
                FROM risk_findings rf
                JOIN review_check_items rci ON rci.id = rf.check_item_id
                WHERE rf.review_run_id = %s
                ORDER BY rci.sort_order
                """,
                (review_run_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                """
                SELECT
                    fe.finding_id,
                    fe.evidence_type,
                    d.title AS document_title,
                    dc.clause_no,
                    dc.title,
                    fe.cited_text
                FROM finding_evidence fe
                JOIN risk_findings rf ON rf.id = fe.finding_id
                JOIN document_chunks dc ON dc.id = fe.chunk_id
                JOIN documents d ON d.id = dc.document_id
                WHERE rf.review_run_id = %s
                ORDER BY fe.finding_id, fe.evidence_type, fe.sort_order
                """,
                (review_run_id,),
            ).fetchall()

        evidence_by_finding: dict[UUID, list[dict[str, Any]]] = {}
        for evidence_row in evidence_rows:
            evidence = dict(evidence_row)
            finding_id = evidence.pop("finding_id")
            evidence_by_finding.setdefault(finding_id, []).append(evidence)

        context = dict(review)
        context["clauses"] = [dict(row) for row in clause_rows]
        context["findings"] = [
            {
                **dict(row),
                "evidence": evidence_by_finding.get(row["id"], []),
            }
            for row in finding_rows
        ]
        return context

    def get_review_detail(self, review_run_id: UUID) -> RiskReviewDetail:
        with open_connection() as connection:
            review = connection.execute(
                """
                SELECT
                    rr.id AS review_run_id, rr.contract_id, c.contract_no,
                    c.name AS contract_name, ct.code AS contract_type_code,
                    rr.contract_document_id, d.revision_no, rr.status,
                    COALESCE(job.progress, CASE WHEN rr.status = 'SUCCEEDED' THEN 100 ELSE 0 END)::INTEGER AS progress,
                    rr.overall_risk_level, rr.summary, rr.approval_suggestion,
                    COALESCE(rr.error_message, job.error_message) AS error_message,
                    rr.created_at, rr.started_at, rr.completed_at
                FROM review_runs rr
                JOIN contracts c ON c.id = rr.contract_id
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN documents d ON d.id = rr.contract_document_id
                LEFT JOIN LATERAL (
                    SELECT progress, error_message FROM async_jobs
                    WHERE resource_type = 'REVIEW_RUN' AND resource_id = rr.id
                    ORDER BY created_at DESC LIMIT 1
                ) job ON TRUE
                WHERE rr.id = %s
                """,
                (review_run_id,),
            ).fetchone()
            if review is None:
                raise RiskReviewError("REVIEW_NOT_FOUND", "风险审查任务不存在。", 404)
            findings = connection.execute(
                """
                SELECT rf.id, rci.code AS check_code, rci.name AS check_name,
                       rf.status, rf.severity, rf.title, rf.description,
                       rf.suggestion, rf.confidence
                FROM risk_findings rf
                JOIN review_check_items rci ON rci.id = rf.check_item_id
                WHERE rf.review_run_id = %s
                ORDER BY rci.sort_order
                """,
                (review_run_id,),
            ).fetchall()
            finding_ids = [row["id"] for row in findings]
            evidence_rows = []
            if finding_ids:
                evidence_rows = connection.execute(
                    """
                    SELECT fe.finding_id, fe.chunk_id, fe.evidence_type,
                           d.title AS document_title, dc.clause_no, dc.title,
                           fe.cited_text, fe.relevance_score
                    FROM finding_evidence fe
                    JOIN document_chunks dc ON dc.id = fe.chunk_id
                    JOIN documents d ON d.id = dc.document_id
                    WHERE fe.finding_id = ANY(%s)
                    ORDER BY fe.finding_id, fe.evidence_type, fe.sort_order
                    """,
                    (finding_ids,),
                ).fetchall()
            # retrieval_runs/retrieval_hits 已经记录每个 LangGraph 分支实际召回的候选。
            # 这里回查候选正文和排名，让前端能区分“检索到”与“最终被模型采纳”。
            retrieval_rows = connection.execute(
                """
                SELECT
                    UPPER(node.node_name) AS check_code,
                    hit.chunk_id,
                    CASE WHEN d.document_type = 'CONTRACT'
                         THEN 'CONTRACT' ELSE 'POLICY' END AS evidence_type,
                    d.title AS document_title, dc.clause_no, dc.title, dc.content,
                    COALESCE((retrieval.filters ->> 'attempt')::INTEGER, 1)
                        AS retrieval_attempt,
                    COALESCE(retrieval.filters ->> 'query_kind', 'INITIAL')
                        AS query_kind,
                    hit.rank_no, hit.similarity_score,
                    hit.rerank_rank_no, hit.rerank_score,
                    hit.selected_for_context,
                    retrieval.ranking_strategy, retrieval.rerank_model,
                    hit.selected_for_context AND EXISTS (
                        SELECT 1
                        FROM finding_evidence selected_evidence
                        JOIN risk_findings selected_finding
                          ON selected_finding.id = selected_evidence.finding_id
                        JOIN review_check_items selected_item
                          ON selected_item.id = selected_finding.check_item_id
                        WHERE selected_evidence.chunk_id = hit.chunk_id
                          AND selected_finding.review_run_id = %s
                          AND LOWER(selected_item.code) = node.node_name
                    ) AS selected_as_evidence
                FROM workflow_runs workflow
                JOIN workflow_node_runs node ON node.workflow_run_id = workflow.id
                JOIN retrieval_runs retrieval ON retrieval.node_run_id = node.id
                JOIN retrieval_hits hit ON hit.retrieval_run_id = retrieval.id
                JOIN document_chunks dc ON dc.id = hit.chunk_id
                JOIN documents d ON d.id = dc.document_id
                WHERE workflow.review_run_id = %s
                ORDER BY node.sequence_no, retrieval_attempt, d.document_type,
                         COALESCE(hit.rerank_rank_no, hit.rank_no)
                """,
                (review_run_id, review_run_id),
            ).fetchall()
        evidence_by_finding: dict[UUID, list[dict[str, Any]]] = {}
        for evidence in evidence_rows:
            evidence_by_finding.setdefault(evidence["finding_id"], []).append(
                {key: value for key, value in dict(evidence).items() if key != "finding_id"}
            )
        retrieval_by_check: dict[str, list[dict[str, Any]]] = {}
        for candidate in retrieval_rows:
            candidate_payload = dict(candidate)
            check_code = candidate_payload.pop("check_code")
            retrieval_by_check.setdefault(check_code, []).append(candidate_payload)
        payload = dict(review)
        payload["findings"] = [
            {
                **dict(finding),
                "evidence": evidence_by_finding.get(finding["id"], []),
                "retrieval_candidates": retrieval_by_check.get(finding["check_code"], []),
            }
            for finding in findings
        ]
        return RiskReviewDetail.model_validate(payload)

    def get_review_trace(self, review_run_id: UUID) -> RiskReviewTrace:
        """把持久化执行记录整理为适合界面展示的脱敏轨迹。"""

        with open_connection() as connection:
            workflow = connection.execute(
                """
                SELECT
                    workflow.review_run_id,
                    workflow.status,
                    workflow.graph_name,
                    workflow.graph_version,
                    workflow.started_at,
                    workflow.completed_at,
                    CASE
                        WHEN workflow.started_at IS NULL THEN NULL
                        ELSE (
                            EXTRACT(
                                EPOCH FROM (
                                    COALESCE(workflow.completed_at, CURRENT_TIMESTAMP)
                                    - workflow.started_at
                                )
                            ) * 1000
                        )::INTEGER
                    END AS total_latency_ms
                FROM workflow_runs workflow
                WHERE workflow.review_run_id = %s
                  AND workflow.run_type = 'RISK_REVIEW'
                ORDER BY workflow.created_at DESC
                LIMIT 1
                """,
                (review_run_id,),
            ).fetchone()
            if workflow is None:
                # 审查记录存在但工作流缺失同样按未找到处理，避免向前端泄露数据库结构状态。
                raise RiskReviewError("REVIEW_TRACE_NOT_FOUND", "风险审查执行轨迹不存在。", 404)

            node_rows = connection.execute(
                """
                SELECT
                    node.id,
                    node.node_name,
                    node.sequence_no,
                    node.status,
                    node.output_data,
                    node.started_at,
                    node.completed_at,
                    node.latency_ms
                FROM workflow_node_runs node
                JOIN workflow_runs workflow ON workflow.id = node.workflow_run_id
                WHERE workflow.review_run_id = %s
                  AND workflow.run_type = 'RISK_REVIEW'
                ORDER BY node.sequence_no
                """,
                (review_run_id,),
            ).fetchall()

            retrieval_rows = connection.execute(
                """
                SELECT
                    retrieval.node_run_id,
                    retrieval.query_text,
                    retrieval.query_embedding_model,
                    retrieval.filters,
                    retrieval.top_k,
                    retrieval.final_top_k,
                    retrieval.ranking_strategy,
                    retrieval.rerank_model,
                    retrieval.rerank_latency_ms,
                    retrieval.rerank_error,
                    retrieval.created_at,
                    (
                        SELECT COUNT(*)::INTEGER
                        FROM retrieval_hits hit
                        WHERE hit.retrieval_run_id = retrieval.id
                    ) AS candidate_count,
                    (
                        SELECT COUNT(*)::INTEGER
                        FROM retrieval_hits hit
                        WHERE hit.retrieval_run_id = retrieval.id
                          AND hit.selected_for_context = TRUE
                    ) AS selected_count
                FROM retrieval_runs retrieval
                JOIN workflow_node_runs node ON node.id = retrieval.node_run_id
                JOIN workflow_runs workflow ON workflow.id = node.workflow_run_id
                WHERE workflow.review_run_id = %s
                  AND workflow.run_type = 'RISK_REVIEW'
                ORDER BY node.sequence_no, retrieval.created_at
                """,
                (review_run_id,),
            ).fetchall()

            model_rows = connection.execute(
                """
                SELECT
                    call.node_run_id,
                    call.provider,
                    call.model_name,
                    call.prompt_name,
                    call.input_tokens,
                    call.output_tokens,
                    call.latency_ms,
                    call.status,
                    call.created_at
                FROM llm_calls call
                JOIN workflow_node_runs node ON node.id = call.node_run_id
                JOIN workflow_runs workflow ON workflow.id = node.workflow_run_id
                WHERE workflow.review_run_id = %s
                  AND workflow.run_type = 'RISK_REVIEW'
                ORDER BY node.sequence_no, call.created_at
                """,
                (review_run_id,),
            ).fetchall()

        retrievals_by_node: dict[UUID, list[dict[str, Any]]] = {}
        for retrieval_row in retrieval_rows:
            retrieval = dict(retrieval_row)
            node_run_id = retrieval.pop("node_run_id")
            filters = retrieval.pop("filters") or {}
            is_contract = filters.get("chunk_type") == "CONTRACT_CLAUSE"
            # 仅投影可解释性字段；document_id 等内部过滤条件不进入 API 响应。
            retrieval.update(
                {
                    "source": "CONTRACT" if is_contract else "POLICY",
                    "retrieval_attempt": int(filters.get("attempt", 1)),
                    "query_kind": filters.get("query_kind", "INITIAL"),
                    "applied_threshold": (
                        filters.get("query_min_score")
                        if is_contract
                        else filters.get("candidate_min_score")
                    ),
                    "query_score": filters.get("query_score") if is_contract else None,
                    "confidence_band": (
                        filters.get("confidence_band") if is_contract else None
                    ),
                    "fallback_reason": (
                        "重排序不可用，已降级为向量排序"
                        if retrieval.pop("rerank_error") is not None
                        else None
                    ),
                }
            )
            retrievals_by_node.setdefault(node_run_id, []).append(retrieval)

        model_calls_by_node: dict[UUID, list[dict[str, Any]]] = {}
        for model_row in model_rows:
            model_call = dict(model_row)
            node_run_id = model_call.pop("node_run_id")
            token_values = [
                model_call.get("input_tokens"),
                model_call.get("output_tokens"),
            ]
            model_call["total_tokens"] = (
                sum(value or 0 for value in token_values)
                if any(value is not None for value in token_values)
                else None
            )
            model_calls_by_node.setdefault(node_run_id, []).append(model_call)

        nodes: list[dict[str, Any]] = []
        for node_row in node_rows:
            node = dict(node_row)
            node_run_id = node.pop("id")
            output = node.pop("output_data") or {}
            nodes.append(
                {
                    **node,
                    "display_name": TRACE_NODE_LABELS.get(
                        node["node_name"], node["node_name"]
                    ),
                    # 失败原因只提供稳定的用户提示，原始异常留在日志和数据库中供开发排查。
                    "error_message": (
                        "节点执行失败，请查看服务日志。"
                        if node["status"] == "FAILED"
                        else None
                    ),
                    "route": output.get("route"),
                    "finding_status": output.get("status"),
                    "severity": output.get("severity"),
                    "retrieval_attempts": output.get("retrieval_attempts"),
                    "initial_missing_sources": output.get(
                        "initial_missing_sources", []
                    ),
                    "retried_sources": output.get("retried_sources", []),
                    "final_missing_sources": output.get("final_missing_sources", []),
                    "contract_evidence_count": output.get(
                        "contract_evidence_count"
                    ),
                    "policy_evidence_count": output.get("policy_evidence_count"),
                    "retrievals": retrievals_by_node.get(node_run_id, []),
                    "model_calls": model_calls_by_node.get(node_run_id, []),
                }
            )

        workflow_payload = dict(workflow)
        workflow_payload["error_message"] = (
            "工作流执行失败，请查看服务日志。"
            if workflow_payload["status"] == "FAILED"
            else None
        )
        workflow_payload["nodes"] = nodes
        return RiskReviewTrace.model_validate(workflow_payload)


def _to_vector_literal(vector: list[float]) -> str:
    """把浮点数组转换成 pgvector 可参数化绑定的文本格式。"""

    return "[" + ",".join(format(value, ".10g") for value in vector) + "]"
