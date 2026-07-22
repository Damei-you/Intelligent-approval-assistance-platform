from __future__ import annotations

import asyncio
import json
import os
import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.core.config import settings
from app.core.database import open_connection
from app.main import app
from app.modules.approval.exceptions import ApprovalError
from app.modules.approval.repository import ApprovalRepository
from app.modules.approval.schemas import ApprovalActionRequest
from app.modules.contract_import.repository import ContractImportRepository
from app.modules.contract_import.schemas import ContractJsonImportRequest
from app.modules.contract_import.service import ContractImportService


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
                connection.execute(
                    """
                    INSERT INTO risk_findings (
                        review_run_id, check_item_id, status, severity,
                        title, description, suggestion
                    ) VALUES (%s, %s, 'RISK', 'MEDIUM', %s, %s, %s)
                    """,
                    (
                        self.review_run_id,
                        check_item["id"],
                        "预付款比例偏高",
                        "合同付款约定与制度存在偏差。",
                        "调整付款节点后再审批。",
                    ),
                )

    def tearDown(self) -> None:
        object.__setattr__(settings, "api_key", self.original_api_key)
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM async_jobs WHERE resource_id = %s",
                    (self.document_id,),
                )
                # contracts 的级联外键会一并清理测试创建的审查、审批和节点记录。
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
