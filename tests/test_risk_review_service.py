from __future__ import annotations

import unittest
from threading import Barrier, Lock
from typing import Any
from uuid import uuid4

from app.modules.risk_review.repository import REVIEW_CHECK_CODES
from app.modules.risk_review.schemas import ModelRiskDecision
from app.modules.risk_review.service import RiskReviewService, _select_references


class FakeEmbeddingProvider:
    """测试不调用外部模型，只返回满足接口约定的固定向量。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]


class FakeModelProvider:
    def __init__(self) -> None:
        self.barrier = Barrier(4)
        self.lock = Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def judge(self, check_item, contract_chunks, policy_chunks):
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            # 四个调用必须同时到达屏障；如果图仍是串行，第一个调用会超时并让测试失败。
            self.barrier.wait(timeout=5)
            return (
                ModelRiskDecision(
                    status="RISK",
                    severity=check_item["default_severity"],
                    title=f"{check_item['name']}存在演示风险",
                    description="合同约定与制度依据不一致。",
                    suggestion="建议修改合同约定。",
                    confidence=0.9,
                    contract_refs=["C1"],
                    policy_refs=["P1"],
                ),
                {"input_tokens": 10, "output_tokens": 5},
            )
        finally:
            with self.lock:
                self.active_calls -= 1


class FakeRepository:
    """记录 LangGraph 的调用顺序，用于验证四项检查和最终汇总。"""

    def __init__(self) -> None:
        self.contract_id = uuid4()
        self.document_id = uuid4()
        self.workflow_id = uuid4()
        self.saved_codes: list[str] = []
        self.check_codes: dict[object, str] = {}
        self.lock = Lock()

    def mark_running(self, review_run_id, job_id):
        return None

    def get_review_context(self, review_run_id):
        return {
            "review_run_id": review_run_id,
            "contract_id": self.contract_id,
            "contract_document_id": self.document_id,
            "contract_no": "TEST-001",
            "contract_name": "测试合同",
            "contract_type_code": "PURCHASE",
            "workflow_run_id": self.workflow_id,
        }

    def get_check_item(self, contract_id, code):
        check_item_id = uuid4()
        with self.lock:
            self.check_codes[check_item_id] = code
        return {
            "id": check_item_id,
            "code": code,
            "name": code,
            "description": code,
            "prompt_template": code,
            "default_severity": "HIGH" if code in {"PAYMENT_TERMS", "BREACH_LIABILITY"} else "MEDIUM",
        }

    def create_node_run(self, workflow_run_id, node_name, sequence_no, input_data):
        return uuid4()

    def finish_node(self, node_run_id, output_data):
        return None

    def update_progress(self, job_id, progress):
        return None

    def update_check_progress(self, job_id, review_run_id):
        return None

    def search_contract_chunks(self, document_id, query_vector, top_k=3):
        return [self._chunk("合同条款")]

    def search_policy_chunks(self, query_vector, top_k=5):
        return [self._chunk("制度依据")]

    def record_retrieval(self, *args, **kwargs):
        return None

    def record_llm_call(self, *args, **kwargs):
        return None

    def save_finding(self, review_run_id, check_item_id, decision, contract_evidence, policy_evidence):
        with self.lock:
            self.saved_codes.append(self.check_codes[check_item_id])
        return uuid4()

    def complete_review(self, review_run_id, job_id):
        return {"overall_risk_level": "HIGH", "approval_suggestion": "APPROVE_AFTER_REVISION"}

    @staticmethod
    def _chunk(content: str) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "document_id": uuid4(),
            "document_title": "测试文档",
            "clause_no": "第一条",
            "title": "测试",
            "content": content,
            "similarity_score": 0.9,
        }


class RiskReviewServiceTests(unittest.TestCase):
    def test_langgraph_runs_four_checks_in_parallel(self) -> None:
        repository = FakeRepository()
        model_provider = FakeModelProvider()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=model_provider,
        )

        result = service.run(uuid4(), uuid4())

        self.assertCountEqual(REVIEW_CHECK_CODES, repository.saved_codes)
        self.assertEqual(4, model_provider.max_active_calls)
        self.assertEqual("HIGH", result["overall_risk_level"])

    def test_reference_selection_rejects_unknown_and_duplicate_labels(self) -> None:
        chunks = [FakeRepository._chunk("证据一"), FakeRepository._chunk("证据二")]

        selected = _select_references(["c1", "C9", "C1", "C2"], chunks, "C")

        self.assertEqual([chunks[0], chunks[1]], selected)


if __name__ == "__main__":
    unittest.main()
