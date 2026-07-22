from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.core.database import open_connection
from app.modules.approval.exceptions import ApprovalError
from app.modules.approval.schemas import ApprovalActionRequest, ApprovalDetail


class ApprovalRepository:
    """在 PostgreSQL 事务中维护固定业务、法务两级审批状态机。"""

    def list_candidates(self) -> list[dict[str, Any]]:
        """每份合同只展示最近一次成功风险审查及其审批状态。"""

        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ON (rr.contract_id)
                    rr.id AS review_run_id, rr.contract_id, c.contract_no,
                    c.name AS contract_name, ct.code AS contract_type_code,
                    rr.contract_document_id, reviewed.revision_no,
                    rr.overall_risk_level, rr.summary AS review_summary,
                    rr.approval_suggestion, rr.completed_at AS review_completed_at,
                    (current_document.id = rr.contract_document_id) AS is_current_revision,
                    ai.id AS approval_instance_id, ai.status AS approval_status,
                    (
                        current_document.id = rr.contract_document_id
                        AND (ai.id IS NULL OR ai.status = 'IN_PROGRESS')
                    ) AS approval_ready
                FROM review_runs rr
                JOIN contracts c ON c.id = rr.contract_id
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN documents reviewed ON reviewed.id = rr.contract_document_id
                LEFT JOIN documents current_document
                  ON current_document.contract_id = rr.contract_id
                 AND current_document.document_type = 'CONTRACT'
                 AND current_document.is_current = TRUE
                LEFT JOIN approval_instances ai ON ai.review_run_id = rr.id
                WHERE rr.status = 'SUCCEEDED'
                ORDER BY rr.contract_id, rr.completed_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, review_run_id: UUID) -> ApprovalDetail:
        """锁定风险审查记录，幂等创建两级审批实例。"""

        approval_instance_id: UUID
        with open_connection() as connection:
            # transaction() 正常结束时提交、异常时回滚，审批实例和两个节点不会只写入一部分。
            with connection.transaction():
                review = connection.execute(
                    """
                    SELECT rr.id, rr.contract_id, rr.contract_document_id, rr.status,
                           current_document.id AS current_document_id
                    FROM review_runs rr
                    LEFT JOIN documents current_document
                      ON current_document.contract_id = rr.contract_id
                     AND current_document.document_type = 'CONTRACT'
                     AND current_document.is_current = TRUE
                    WHERE rr.id = %s
                    FOR UPDATE OF rr
                    """,
                    (review_run_id,),
                ).fetchone()
                if review is None:
                    raise ApprovalError("REVIEW_NOT_FOUND", "风险审查任务不存在。", 404)
                if review["status"] != "SUCCEEDED":
                    raise ApprovalError(
                        "REVIEW_NOT_COMPLETED", "风险审查成功完成后才能发起审批。"
                    )
                if review["current_document_id"] != review["contract_document_id"]:
                    raise ApprovalError(
                        "REVIEW_REVISION_OUTDATED",
                        "该风险报告不是合同当前修订版本，请重新执行风险审查。",
                        409,
                    )

                existing = connection.execute(
                    "SELECT id FROM approval_instances WHERE review_run_id = %s",
                    (review_run_id,),
                ).fetchone()
                if existing:
                    approval_instance_id = existing["id"]
                else:
                    approval_instance_id = uuid4()
                    connection.execute(
                        """
                        INSERT INTO approval_instances (
                            id, contract_id, review_run_id, status, current_step_no
                        ) VALUES (%s, %s, %s, 'IN_PROGRESS', 1)
                        """,
                        (approval_instance_id, review["contract_id"], review_run_id),
                    )
                    # 业务节点立即进入处理中，法务节点必须等待业务节点通过。
                    # psycopg 的 executemany 位于游标上，适合一次写入结构相同的两个审批节点。
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO approval_steps (
                                id, approval_instance_id, step_no,
                                step_type, step_name, status
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    uuid4(),
                                    approval_instance_id,
                                    1,
                                    "BUSINESS",
                                    "业务审批",
                                    "IN_PROGRESS",
                                ),
                                (
                                    uuid4(),
                                    approval_instance_id,
                                    2,
                                    "LEGAL",
                                    "法务审批",
                                    "PENDING",
                                ),
                            ],
                        )
                    connection.execute(
                        "UPDATE contracts SET status = 'PENDING_APPROVAL' WHERE id = %s",
                        (review["contract_id"],),
                    )
        return self.get_detail(approval_instance_id)

    def take_action(
        self, approval_instance_id: UUID, action: ApprovalActionRequest
    ) -> ApprovalDetail:
        """只处理当前节点，并在同一事务中推进或结束整个审批流程。"""

        with open_connection() as connection:
            with connection.transaction():
                instance = connection.execute(
                    """
                    SELECT id, contract_id, status, current_step_no
                    FROM approval_instances
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (approval_instance_id,),
                ).fetchone()
                if instance is None:
                    raise ApprovalError("APPROVAL_NOT_FOUND", "审批实例不存在。", 404)
                if instance["status"] != "IN_PROGRESS":
                    raise ApprovalError(
                        "APPROVAL_ALREADY_COMPLETED", "该审批流程已经结束，不能重复操作。", 409
                    )

                step = connection.execute(
                    """
                    SELECT id, step_no, status
                    FROM approval_steps
                    WHERE approval_instance_id = %s AND step_no = %s
                    FOR UPDATE
                    """,
                    (approval_instance_id, instance["current_step_no"]),
                ).fetchone()
                if step is None or step["status"] != "IN_PROGRESS":
                    raise ApprovalError(
                        "CURRENT_STEP_INVALID", "当前审批节点状态异常，无法执行操作。", 409
                    )

                connection.execute(
                    """
                    UPDATE approval_steps
                    SET status = 'COMPLETED', approver_name = %s, decision = %s,
                        comment = %s, handled_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (action.approver_name, action.decision, action.comment, step["id"]),
                )
                if action.decision == "APPROVED" and step["step_no"] == 1:
                    connection.execute(
                        """
                        UPDATE approval_steps SET status = 'IN_PROGRESS'
                        WHERE approval_instance_id = %s AND step_no = 2
                        """,
                        (approval_instance_id,),
                    )
                    connection.execute(
                        "UPDATE approval_instances SET current_step_no = 2 WHERE id = %s",
                        (approval_instance_id,),
                    )
                else:
                    final_status = action.decision
                    connection.execute(
                        """
                        UPDATE approval_steps SET status = 'SKIPPED'
                        WHERE approval_instance_id = %s AND status = 'PENDING'
                        """,
                        (approval_instance_id,),
                    )
                    connection.execute(
                        """
                        UPDATE approval_instances
                        SET status = %s, final_decision = %s,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (final_status, action.decision, approval_instance_id),
                    )
                    connection.execute(
                        "UPDATE contracts SET status = %s WHERE id = %s",
                        (final_status, instance["contract_id"]),
                    )
        return self.get_detail(approval_instance_id)

    def get_detail(self, approval_instance_id: UUID) -> ApprovalDetail:
        """回查审批流程、风险摘要和四项检查结论。"""

        with open_connection() as connection:
            instance = connection.execute(
                """
                SELECT ai.id AS approval_instance_id, ai.review_run_id,
                       ai.contract_id, c.contract_no, c.name AS contract_name,
                       ct.code AS contract_type_code, rr.contract_document_id,
                       d.revision_no, ai.status, ai.current_step_no,
                       ai.final_decision, rr.overall_risk_level,
                       rr.summary AS review_summary, rr.approval_suggestion,
                       ai.created_at, ai.completed_at
                FROM approval_instances ai
                JOIN contracts c ON c.id = ai.contract_id
                JOIN contract_types ct ON ct.id = c.contract_type_id
                JOIN review_runs rr ON rr.id = ai.review_run_id
                JOIN documents d ON d.id = rr.contract_document_id
                WHERE ai.id = %s
                """,
                (approval_instance_id,),
            ).fetchone()
            if instance is None:
                raise ApprovalError("APPROVAL_NOT_FOUND", "审批实例不存在。", 404)
            steps = connection.execute(
                """
                SELECT id, step_no, step_type, step_name, status,
                       approver_name, decision, comment, handled_at
                FROM approval_steps
                WHERE approval_instance_id = %s
                ORDER BY step_no
                """,
                (approval_instance_id,),
            ).fetchall()
            findings = connection.execute(
                """
                SELECT rci.code AS check_code, rci.name AS check_name,
                       rf.status, rf.severity, rf.title, rf.suggestion
                FROM risk_findings rf
                JOIN review_check_items rci ON rci.id = rf.check_item_id
                WHERE rf.review_run_id = %s
                ORDER BY rci.sort_order
                """,
                (instance["review_run_id"],),
            ).fetchall()
        payload = dict(instance)
        payload["steps"] = [dict(row) for row in steps]
        payload["findings"] = [dict(row) for row in findings]
        return ApprovalDetail.model_validate(payload)
