from __future__ import annotations

import json
import unittest
from threading import Barrier, Lock
from typing import Any
from uuid import uuid4

import httpx

from app.modules.risk_review.repository import REVIEW_CHECK_CODES
from app.modules.risk_review.schemas import ModelRiskDecision
from app.modules.risk_review.service import (
    DashScopeRerankProvider,
    PolicyRerankResult,
    RiskReviewService,
    _select_references,
)


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
        self.policy_contents: list[list[str]] = []

    def judge(self, check_item, contract_chunks, policy_chunks):
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            self.policy_contents.append([chunk["content"] for chunk in policy_chunks])
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


class FakeRerankProvider:
    """用倒序模拟重排序，证明模型收到的不是原向量前五。"""

    def rerank(self, query, hits, final_top_k):
        candidates = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": len(hits) - index + 1,
                "rerank_score": index / 10,
                "selected_for_context": index > len(hits) - final_top_k,
            }
            for index, hit in enumerate(hits, 1)
        ]
        return PolicyRerankResult(
            selected_hits=list(reversed(candidates))[:final_top_k],
            all_hits=candidates,
            latency_ms=12,
        )


class FailingRerankProvider:
    def rerank(self, query, hits, final_top_k):
        raise RuntimeError("模拟重排序服务不可用")


class FakeRepository:
    """记录 LangGraph 的调用顺序，用于验证四项检查和最终汇总。"""

    def __init__(self) -> None:
        self.contract_id = uuid4()
        self.document_id = uuid4()
        self.workflow_id = uuid4()
        self.saved_codes: list[str] = []
        self.check_codes: dict[object, str] = {}
        self.lock = Lock()
        self.policy_top_k_values: list[int] = []
        self.retrieval_records: list[dict[str, Any]] = []

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
        with self.lock:
            self.policy_top_k_values.append(top_k)
        return [self._chunk(f"制度候选{index}") for index in range(1, top_k + 1)]

    def record_retrieval(self, *args, **kwargs):
        with self.lock:
            self.retrieval_records.append({"args": args, "kwargs": kwargs})

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
    def test_qwen3_rerank_request_and_response_mapping(self) -> None:
        """验证 qwen3-rerank 使用顶层参数，并按返回索引重排候选。"""

        def handle_request(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual("qwen3-rerank", payload["model"])
            self.assertEqual("查询", payload["query"])
            self.assertEqual(2, payload["top_n"])
            self.assertEqual("Bearer test-key", request.headers["Authorization"])
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.92},
                        {"index": 0, "relevance_score": 0.31},
                    ]
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handle_request))
        provider = DashScopeRerankProvider(client=client, api_key="test-key")
        hits = [FakeRepository._chunk("制度一"), FakeRepository._chunk("制度二")]

        result = provider.rerank("查询", hits, final_top_k=1)

        self.assertEqual("制度二", result.selected_hits[0]["content"])
        self.assertEqual(2, result.all_hits[0]["rerank_rank_no"])
        self.assertEqual(1, result.all_hits[1]["rerank_rank_no"])

    def test_langgraph_runs_four_checks_in_parallel(self) -> None:
        repository = FakeRepository()
        model_provider = FakeModelProvider()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=model_provider,
            rerank_provider=FakeRerankProvider(),
        )

        result = service.run(uuid4(), uuid4())

        self.assertCountEqual(REVIEW_CHECK_CODES, repository.saved_codes)
        self.assertEqual(4, model_provider.max_active_calls)
        self.assertEqual("HIGH", result["overall_risk_level"])
        self.assertEqual([10, 10, 10, 10], sorted(repository.policy_top_k_values))
        self.assertTrue(
            all(contents[0] == "制度候选10" for contents in model_provider.policy_contents)
        )
        policy_records = [
            record
            for record in repository.retrieval_records
            if record["kwargs"].get("ranking_strategy") == "RERANK"
        ]
        self.assertEqual(4, len(policy_records))
        self.assertTrue(all(len(record["args"][3]) == 10 for record in policy_records))

    def test_rerank_failure_falls_back_to_vector_top_five(self) -> None:
        repository = FakeRepository()
        model_provider = FakeModelProvider()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=model_provider,
            rerank_provider=FailingRerankProvider(),
        )

        result = service.run(uuid4(), uuid4())

        self.assertEqual("HIGH", result["overall_risk_level"])
        self.assertTrue(
            all(
                contents == [f"制度候选{index}" for index in range(1, 6)]
                for contents in model_provider.policy_contents
            )
        )
        fallback_records = [
            record
            for record in repository.retrieval_records
            if record["kwargs"].get("ranking_strategy") == "RERANK_FALLBACK"
        ]
        self.assertEqual(4, len(fallback_records))
        self.assertTrue(
            all("模拟重排序服务不可用" in record["kwargs"]["rerank_error"] for record in fallback_records)
        )

    def test_reference_selection_rejects_unknown_and_duplicate_labels(self) -> None:
        chunks = [FakeRepository._chunk("证据一"), FakeRepository._chunk("证据二")]

        selected = _select_references(["c1", "C9", "C1", "C2"], chunks, "C")

        self.assertEqual([chunks[0], chunks[1]], selected)


if __name__ == "__main__":
    unittest.main()
