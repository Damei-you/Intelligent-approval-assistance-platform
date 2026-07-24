from __future__ import annotations

import asyncio
import json
import os
import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError
from psycopg.types.json import Jsonb

from app.core.config import settings
from app.core.database import open_connection
from app.main import app
from app.modules.approval.exceptions import ApprovalError
from app.modules.approval.repository import ApprovalRepository
from app.modules.approval.schemas import ApprovalActionRequest
from app.modules.contract_import.repository import ContractImportRepository
from app.modules.contract_import.schemas import ContractJsonImportRequest
from app.modules.contract_import.service import ContractImportService
from app.modules.risk_review.repository import RiskReviewRepository


@unittest.skipUnless(
    os.getenv("RUN_INTEGRATION_TESTS") == "1",
    "Set RUN_INTEGRATION_TESTS=1 with PostgreSQL running to execute integration tests.",
)
class ApprovalIntegrationTests(unittest.TestCase):
    """使用真实 PostgreSQL 验证两级审批状态推进和 HTTP 接口。"""

    def setUp(self) -> None:
        self.contract_no = f"APPROVAL-IT-{uuid4().hex[:10]}"
        self.original_api_key = settings.api_key
        # 合同导入会尝试投递向量任务；测试审批不需要外部模型，因此临时关闭 API Key。
        object.__setattr__(settings, "api_key", "")
        imported = ContractImportService(ContractImportRepository()).import_json(
            ContractJsonImportRequest(
                contract_no=self.contract_no,
                name="审批集成测试采购合同",
                contract_type_code="PURCHASE",
                clauses=[{"clause_no": "第一条", "content": "验收后付款。"}],
            )
        )
        self.contract_id = imported.contract_id
        self.document_id = imported.document_id
        self.review_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO review_runs (
                        id, contract_id, contract_document_id, status,
                        overall_risk_level, summary, approval_suggestion,
                        started_at, completed_at
                    ) VALUES (
                        %s, %s, %s, 'SUCCEEDED', 'MEDIUM',
                        '发现一项付款风险。', 'APPROVE_AFTER_REVISION',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """,
                    (self.review_run_id, self.contract_id, self.document_id),
                )
                check_item = connection.execute(
                    "SELECT id FROM review_check_items WHERE code = 'PAYMENT_TERMS'"
                ).fetchone()
                finding = connection.execute(
                    """
                    INSERT INTO risk_findings (
                        review_run_id, check_item_id, status, severity,
                        title, description, suggestion
                    ) VALUES (%s, %s, 'RISK', 'MEDIUM', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        self.review_run_id,
                        check_item["id"],
                        "预付款比例偏高",
                        "合同付款约定与制度存在偏差。",
                        "调整付款节点后再审批。",
                    ),
                ).fetchone()
                chunk = connection.execute(
                    "SELECT id, content FROM document_chunks WHERE document_id = %s",
                    (self.document_id,),
                ).fetchone()
                workflow_run_id = uuid4()
                node_run_id = uuid4()
                retrieval_run_id = uuid4()
                connection.execute(
                    """
                    INSERT INTO workflow_runs (
                        id, run_type, review_run_id, graph_name,
                        graph_version, status, started_at, completed_at
                    ) VALUES (
                        %s, 'RISK_REVIEW', %s, 'contract_risk_review', '1.0',
                        'SUCCEEDED', CURRENT_TIMESTAMP - INTERVAL '1 second',
                        CURRENT_TIMESTAMP
                    )
                    """,
                    (workflow_run_id, self.review_run_id),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_node_runs (
                        id, workflow_run_id, node_name, sequence_no, status,
                        output_data, latency_ms, started_at, completed_at
                    ) VALUES (
                        %s, %s, 'payment_terms', 1, 'SUCCEEDED',
                        %s, 850, CURRENT_TIMESTAMP - INTERVAL '900 milliseconds',
                        CURRENT_TIMESTAMP - INTERVAL '50 milliseconds'
                    )
                    """,
                    (
                        node_run_id,
                        workflow_run_id,
                        Jsonb(
                            {
                                "status": "RISK",
                                "severity": "MEDIUM",
                                "route": "MODEL_JUDGMENT",
                                "retrieval_attempts": 1,
                                "initial_missing_sources": [],
                                "retried_sources": [],
                                "final_missing_sources": [],
                                "contract_evidence_count": 1,
                                "policy_evidence_count": 1,
                            }
                        ),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO retrieval_runs (
                        id, node_run_id, query_text, query_embedding_model,
                        filters, top_k, final_top_k, ranking_strategy,
                        rerank_model, rerank_latency_ms
                    ) VALUES (
                        %s, %s, '付款条款', 'text-embedding-v4', %s,
                        1, 1, 'RERANK', 'qwen3-rerank', 35
                    )
                    """,
                    (
                        retrieval_run_id,
                        node_run_id,
                        Jsonb(
                            {
                                "attempt": 1,
                                "query_kind": "INITIAL",
                                "chunk_type": "CONTRACT_CLAUSE",
                                "query_min_score": 0.45,
                                "query_score": 0.82,
                                "confidence_band": "NORMAL",
                            }
                        ),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO retrieval_hits (
                        retrieval_run_id, chunk_id, rank_no,
                        similarity_score, selected_for_context
                    ) VALUES (%s, %s, 1, 0.88, TRUE)
                    """,
                    (retrieval_run_id, chunk["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO llm_calls (
                        node_run_id, provider, model_name, prompt_name,
                        input_tokens, output_tokens, latency_ms, status
                    ) VALUES (
                        %s, 'DASHSCOPE', 'qwen-plus', 'risk_review_v1',
                        120, 38, 210, 'SUCCEEDED'
                    )
                    """,
                    (node_run_id,),
                )
                connection.execute(
                    """
                    INSERT INTO finding_evidence (
                        finding_id, chunk_id, evidence_type,
                        relevance_score, cited_text, sort_order
                    ) VALUES (%s, %s, 'CONTRACT', 0.88, %s, 1)
                    """,
                    (finding["id"], chunk["id"], chunk["content"]),
                )

    def tearDown(self) -> None:
        object.__setattr__(settings, "api_key", self.original_api_key)
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM async_jobs WHERE resource_id = %s",
                    (self.document_id,),
                )
                # finding_evidence 和 retrieval_hits 同时引用条款与审查链路。先删除审批、
                # 再删除审查，可让审查侧级联完整结束，最后再删除合同及其条款。
                connection.execute(
                    "DELETE FROM approval_instances WHERE contract_id = %s",
                    (self.contract_id,),
                )
                connection.execute(
                    "DELETE FROM review_runs WHERE contract_id = %s",
                    (self.contract_id,),
                )
                connection.execute(
                    "DELETE FROM contracts WHERE id = %s",
                    (self.contract_id,),
                )

    def test_business_then_legal_approval_completes_contract(self) -> None:
        """业务通过后进入法务，法务通过后合同状态应为 APPROVED。"""

        repository = ApprovalRepository()
        created = repository.create(self.review_run_id)

        self.assertEqual("IN_PROGRESS", created.status)
        self.assertEqual(["IN_PROGRESS", "PENDING"], [step.status for step in created.steps])

        after_business = repository.take_action(
            created.approval_instance_id,
            ApprovalActionRequest(
                approver_name="业务审批人",
                decision="APPROVED",
                comment="业务条件可以接受。",
            ),
        )
        self.assertEqual(2, after_business.current_step_no)
        self.assertEqual(["COMPLETED", "IN_PROGRESS"], [step.status for step in after_business.steps])

        completed = repository.take_action(
            created.approval_instance_id,
            ApprovalActionRequest(
                approver_name="法务审批人",
                decision="APPROVED",
                comment="风险已充分披露，同意审批。",
            ),
        )
        self.assertEqual("APPROVED", completed.status)
        self.assertEqual("APPROVED", completed.final_decision)
        with open_connection() as connection:
            contract_status = connection.execute(
                "SELECT status FROM contracts WHERE id = %s", (self.contract_id,)
            ).fetchone()["status"]
        self.assertEqual("APPROVED", contract_status)

    def test_return_ends_flow_and_skips_legal_step(self) -> None:
        """业务退回会立即结束实例，尚未开始的法务节点标记为跳过。"""

        repository = ApprovalRepository()
        created = repository.create(self.review_run_id)
        returned = repository.take_action(
            created.approval_instance_id,
            ApprovalActionRequest(
                approver_name="业务审批人",
                decision="RETURNED",
                comment="请先按照风险建议修改付款条款。",
            ),
        )

        self.assertEqual("RETURNED", returned.status)
        self.assertEqual(["COMPLETED", "SKIPPED"], [step.status for step in returned.steps])
        with self.assertRaises(ApprovalError) as context:
            repository.take_action(
                created.approval_instance_id,
                ApprovalActionRequest(approver_name="重复操作", decision="APPROVED"),
            )
        self.assertEqual("APPROVAL_ALREADY_COMPLETED", context.exception.code)

    def test_outdated_review_cannot_start_approval(self) -> None:
        """合同产生新修订后，旧风险报告不能继续发起审批。"""

        ContractImportService(ContractImportRepository()).import_json(
            ContractJsonImportRequest(
                contract_no=self.contract_no,
                name="审批集成测试采购合同修订版",
                contract_type_code="PURCHASE",
                clauses=[{"clause_no": "第一条", "content": "验收后十五日内付款。"}],
            )
        )

        with self.assertRaises(ApprovalError) as context:
            ApprovalRepository().create(self.review_run_id)
        self.assertEqual("REVIEW_REVISION_OUTDATED", context.exception.code)

    def test_http_endpoints_create_and_take_action(self) -> None:
        """前端使用的候选、创建和操作接口应返回完整审批详情。"""

        status_code, candidates = asyncio.run(
            _asgi_request("GET", "/api/v1/approvals/candidates")
        )
        self.assertEqual(200, status_code)
        self.assertIn(str(self.review_run_id), [item["review_run_id"] for item in candidates])

        status_code, created = asyncio.run(
            _asgi_request(
                "POST",
                "/api/v1/approvals",
                {"review_run_id": str(self.review_run_id)},
            )
        )
        self.assertEqual(201, status_code)
        self.assertEqual(2, len(created["steps"]))

        status_code, updated = asyncio.run(
            _asgi_request(
                "POST",
                f"/api/v1/approvals/{created['approval_instance_id']}/actions",
                {
                    "approver_name": "HTTP 业务审批人",
                    "decision": "APPROVED",
                    "comment": "同意进入法务审批。",
                },
            )
        )
        self.assertEqual(200, status_code)
        self.assertEqual(2, updated["current_step_no"])

    def test_negative_decision_requires_comment(self) -> None:
        """Pydantic 在请求进入仓储前阻止没有原因的驳回和退回。"""

        with self.assertRaises(ValidationError):
            ApprovalActionRequest(approver_name="业务审批人", decision="REJECTED")

    def test_review_detail_restores_retrieval_candidates_and_latest_review(self) -> None:
        """历史审查应恢复检索候选，并在合同列表中标记为最近一次审查。"""

        repository = RiskReviewRepository()
        detail = repository.get_review_detail(self.review_run_id)
        payment = next(
            finding for finding in detail.findings if finding.check_code == "PAYMENT_TERMS"
        )

        self.assertEqual(1, len(payment.retrieval_candidates))
        self.assertEqual("CONTRACT", payment.retrieval_candidates[0].evidence_type)
        self.assertEqual(1, payment.retrieval_candidates[0].retrieval_attempt)
        self.assertEqual("INITIAL", payment.retrieval_candidates[0].query_kind)
        self.assertTrue(payment.retrieval_candidates[0].selected_as_evidence)
        self.assertEqual("验收后付款。", payment.retrieval_candidates[0].content)

        contract = next(
            row for row in repository.list_contracts() if row["contract_id"] == self.contract_id
        )
        self.assertEqual(self.review_run_id, contract["latest_review_run_id"])
        self.assertEqual("SUCCEEDED", contract["latest_review_status"])
        self.assertTrue(contract["latest_review_is_current"])

    def test_review_trace_exposes_sanitized_execution_summary(self) -> None:
        """轨迹接口应返回节点、检索与模型指标，但不暴露原始输入和模型输出。"""

        repository = RiskReviewRepository()
        trace = repository.get_review_trace(self.review_run_id)

        self.assertEqual("SUCCEEDED", trace.status)
        self.assertEqual("contract_risk_review", trace.graph_name)
        self.assertEqual(1, len(trace.nodes))
        payment = trace.nodes[0]
        self.assertEqual("付款条款检查", payment.display_name)
        self.assertEqual("MODEL_JUDGMENT", payment.route)
        self.assertEqual("RISK", payment.finding_status)
        self.assertEqual(1, payment.retrieval_attempts)
        self.assertEqual(1, payment.retrievals[0].candidate_count)
        self.assertEqual(1, payment.retrievals[0].selected_count)
        self.assertEqual("RERANK", payment.retrievals[0].ranking_strategy)
        self.assertEqual(158, payment.model_calls[0].total_tokens)
        self.assertEqual(210, payment.model_calls[0].latency_ms)

        status_code, payload = asyncio.run(
            _asgi_request(
                "GET",
                f"/api/v1/risk-reviews/{self.review_run_id}/trace",
            )
        )
        self.assertEqual(200, status_code)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("input_data", serialized)
        self.assertNotIn("output_data", serialized)
        self.assertNotIn("contract_refs", serialized)


async def _asgi_request(
    method: str,
    path: str,
    json_body: dict | None = None,
) -> tuple[int, dict | list]:
    """直接调用 ASGI 应用，避免测试依赖真实 HTTP 端口。"""

    body = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body else b""
    messages: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    headers = [(b"content-type", b"application/json")] if json_body else []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "extensions": {},
    }
    await app(scope, receive, send)
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return response_start["status"], json.loads(response_body)


if __name__ == "__main__":
    unittest.main()
