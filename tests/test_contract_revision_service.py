from __future__ import annotations

import unittest
from types import SimpleNamespace
from uuid import uuid4

from app.modules.risk_review.exceptions import RiskReviewError
from app.modules.risk_review.revision_service import ContractRevisionService
from app.modules.risk_review.schemas import (
    ContractRevisionApplyChange,
    ContractRevisionCreateRequest,
    ModelContractRevisionChange,
    ModelContractRevisionPlan,
)


class FakeRevisionRepository:
    def __init__(self) -> None:
        self.contract_id = uuid4()
        self.document_id = uuid4()
        self.clause_one_id = uuid4()
        self.clause_two_id = uuid4()
        self.payment_finding_id = uuid4()
        self.warranty_finding_id = uuid4()
        self.current_document_id = self.document_id

    def get_revision_context(self, review_run_id):
        return {
            "review_run_id": review_run_id,
            "review_status": "SUCCEEDED",
            "contract_id": self.contract_id,
            "source_document_id": self.document_id,
            "current_document_id": self.current_document_id,
            "contract_no": "REVISION-UNIT-001",
            "contract_name": "修订服务单元测试合同",
            "contract_type_code": "PURCHASE",
            "counterparty": "虚构供应商",
            "amount": 10000,
            "currency": "CNY",
            "document_title": "修订服务单元测试合同",
            "source_revision_no": 1,
            "source_metadata": {"evaluation_case_id": "revision-unit"},
            "clauses": [
                {
                    "id": self.clause_one_id,
                    "chunk_index": 0,
                    "clause_no": "第一条",
                    "title": "付款",
                    "content": "合同签订后支付全部价款。",
                    "page_no": None,
                    "metadata": {},
                },
                {
                    "id": self.clause_two_id,
                    "chunk_index": 1,
                    "clause_no": "第二条",
                    "title": "交付",
                    "content": "乙方应按期交付。",
                    "page_no": None,
                    "metadata": {},
                },
            ],
            "findings": [
                {
                    "id": self.payment_finding_id,
                    "check_code": "PAYMENT_TERMS",
                    "check_name": "付款条款检查",
                    "status": "RISK",
                    "severity": "HIGH",
                    "title": "预付款比例过高",
                    "description": "付款约定与制度冲突。",
                    "suggestion": "降低预付款比例。",
                    "evidence": [],
                },
                {
                    "id": self.warranty_finding_id,
                    "check_code": "WARRANTY",
                    "check_name": "质保条款检查",
                    "status": "INSUFFICIENT_INFORMATION",
                    "severity": "MEDIUM",
                    "title": "缺少质保条款",
                    "description": "合同没有明确质保安排。",
                    "suggestion": "补充质保期限和起算点。",
                    "evidence": [],
                },
            ],
        }


class FakeRevisionModelProvider:
    def generate(self, context, actionable_findings):
        return (
            ModelContractRevisionPlan(
                summary="修改付款条款并新增质保条款。",
                changes=[
                    ModelContractRevisionChange(
                        check_code="PAYMENT_TERMS",
                        action="REPLACE",
                        target_clause_ref="C1",
                        proposed_clause_no="第一条",
                        proposed_title="付款",
                        proposed_content="验收合格后支付全部价款。",
                        change_summary="调整付款节点。",
                        rationale="对应付款风险建议。",
                    ),
                    ModelContractRevisionChange(
                        check_code="WARRANTY",
                        action="ADD",
                        target_clause_ref="C2",
                        proposed_clause_no="第三条",
                        proposed_title="质量保证",
                        proposed_content="质保期自验收合格之日起计算。",
                        change_summary="补充质保约定。",
                        rationale="对应质保信息不足。",
                    ),
                ],
            ),
            {"input_tokens": 100, "output_tokens": 50},
            120,
        )


class FakeRevisionImportService:
    def __init__(self) -> None:
        self.payload = None
        self.expected_current_document_id = None
        self.idempotency_key = None
        self.created_document_id = uuid4()

    def import_revision_json(
        self,
        payload,
        *,
        expected_current_document_id,
        idempotency_key,
    ):
        self.payload = payload
        self.expected_current_document_id = expected_current_document_id
        self.idempotency_key = idempotency_key
        return SimpleNamespace(
            contract_id=uuid4(),
            document_id=self.created_document_id,
            contract_no=payload.contract_no,
            revision_no=2,
            clause_count=len(payload.clauses),
            vectorization_job_id=None,
            vectorization_status="NOT_CONFIGURED",
            message="合同及条款导入成功。",
        )


class ContractRevisionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRevisionRepository()
        self.import_service = FakeRevisionImportService()
        self.service = ContractRevisionService(
            repository=self.repository,
            import_service=self.import_service,
            model_provider=FakeRevisionModelProvider(),
        )
        self.review_run_id = uuid4()

    def test_generate_draft_maps_model_labels_to_backend_ids(self) -> None:
        """模型只能返回 C 标签，接口中的条款 ID 与原文必须由后端回填。"""

        draft = self.service.generate_draft(self.review_run_id)

        self.assertEqual(2, len(draft.changes))
        self.assertEqual(self.repository.clause_one_id, draft.changes[0].target_clause_id)
        self.assertEqual(
            "合同签订后支付全部价款。",
            draft.changes[0].original_content,
        )
        self.assertEqual("ADD", draft.changes[1].action)
        self.assertEqual(self.repository.clause_two_id, draft.changes[1].target_clause_id)
        self.assertEqual(2, draft.target_revision_no)

    def test_create_revision_only_applies_confirmed_changes_and_records_source(self) -> None:
        """确认请求只合并用户采用项，并写入来源审查与来源条款元数据。"""

        client_request_id = uuid4()
        request = ContractRevisionCreateRequest(
            source_document_id=self.repository.document_id,
            client_request_id=client_request_id,
            changes=[
                ContractRevisionApplyChange(
                    finding_id=self.repository.payment_finding_id,
                    action="REPLACE",
                    target_clause_id=self.repository.clause_one_id,
                    proposed_clause_no="第一条",
                    proposed_title="付款",
                    proposed_content="验收合格后支付全部价款。",
                ),
                ContractRevisionApplyChange(
                    finding_id=self.repository.warranty_finding_id,
                    action="ADD",
                    target_clause_id=self.repository.clause_two_id,
                    proposed_clause_no="第三条",
                    proposed_title="质量保证",
                    proposed_content="质保期自验收合格之日起计算。",
                ),
            ],
        )

        result = self.service.create_revision(self.review_run_id, request)

        self.assertEqual(2, result.revision_no)
        self.assertEqual(3, len(self.import_service.payload.clauses))
        self.assertEqual(
            "验收合格后支付全部价款。",
            self.import_service.payload.clauses[0].content,
        )
        self.assertEqual(
            "乙方应按期交付。",
            self.import_service.payload.clauses[1].content,
        )
        self.assertEqual(
            "质保期自验收合格之日起计算。",
            self.import_service.payload.clauses[2].content,
        )
        self.assertEqual(
            str(self.review_run_id),
            self.import_service.payload.metadata["source_review_run_id"],
        )
        self.assertEqual(
            str(client_request_id),
            self.import_service.payload.metadata["revision_client_request_id"],
        )

    def test_outdated_review_cannot_generate_new_draft(self) -> None:
        """旧风险报告不能继续生成草案，避免覆盖已存在的新修订内容。"""

        self.repository.current_document_id = uuid4()

        with self.assertRaises(RiskReviewError) as raised:
            self.service.generate_draft(self.review_run_id)

        self.assertEqual("REVISION_SOURCE_OUTDATED", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
