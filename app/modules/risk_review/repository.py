from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.core.database import open_connection
from app.modules.risk_review.exceptions import RiskReviewError
from app.modules.risk_review.schemas import ModelRiskDecision, RiskReviewDetail


REVIEW_CHECK_CODES = (
    "PAYMENT_TERMS",
    "WARRANTY",
    "BREACH_LIABILITY",
    "DISPUTE_RESOLUTION",
)


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
                    (COUNT(dc.id) > 0 AND COUNT(dc.id) = COUNT(dc.embedding)) AS review_ready
                FROM contracts c
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN documents d
                  ON d.contract_id = c.id
                 AND d.document_type = 'CONTRACT'
                 AND d.is_current = TRUE
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                GROUP BY c.id, ct.code, d.id
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
    ) -> None:
        retrieval_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO retrieval_runs (
                        id, node_run_id, query_text, query_embedding_model, filters, top_k
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        retrieval_run_id,
                        node_run_id,
                        query_text,
                        model_name,
                        Jsonb(filters),
                        max(1, len(hits)),
                    ),
                )
                if hits:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO retrieval_hits (
                                retrieval_run_id, chunk_id, rank_no,
                                similarity_score, selected_for_context
                            ) VALUES (%s, %s, %s, %s, TRUE)
                            """,
                            [
                                (
                                    retrieval_run_id,
                                    hit["id"],
                                    rank,
                                    max(-1, min(1, hit["similarity_score"])),
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
    ) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO llm_calls (
                        node_run_id, provider, model_name, prompt_name,
                        input_summary, output_data, input_tokens, output_tokens, status
                    ) VALUES (%s, 'DASHSCOPE', %s, 'risk_review_v1', %s, %s, %s, %s, 'SUCCEEDED')
                    """,
                    (
                        node_run_id,
                        model_name,
                        Jsonb({"check_code": check_code}),
                        Jsonb(output_data),
                        input_tokens,
                        output_tokens,
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
        evidence_by_finding: dict[UUID, list[dict[str, Any]]] = {}
        for evidence in evidence_rows:
            evidence_by_finding.setdefault(evidence["finding_id"], []).append(
                {key: value for key, value in dict(evidence).items() if key != "finding_id"}
            )
        payload = dict(review)
        payload["findings"] = [
            {**dict(finding), "evidence": evidence_by_finding.get(finding["id"], [])}
            for finding in findings
        ]
        return RiskReviewDetail.model_validate(payload)


def _to_vector_literal(vector: list[float]) -> str:
    """把浮点数组转换成 pgvector 可参数化绑定的文本格式。"""

    return "[" + ",".join(format(value, ".10g") for value in vector) + "]"
