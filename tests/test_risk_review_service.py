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

    def rerank(self, query, hits, final_top_k, *, instruct=None):
        is_contract = instruct is not None and "current contract" in instruct
        candidates = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": len(hits) - index + 1,
                "rerank_score": 0.9 if is_contract else index / 10,
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
    def rerank(self, query, hits, final_top_k, *, instruct=None):
        raise RuntimeError("模拟重排序服务不可用")


class LowScoreContractRerankProvider(FakeRerankProvider):
    """合同第一名低于查询级门槛，制度侧仍返回正常候选。"""

    def rerank(self, query, hits, final_top_k, *, instruct=None):
        if instruct is None or "current contract" not in instruct:
            return super().rerank(
                query, hits, final_top_k, instruct=instruct
            )
        candidates = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": index,
                "rerank_score": 0.44,
                "selected_for_context": index <= final_top_k,
            }
            for index, hit in enumerate(hits, 1)
        ]
        return PolicyRerankResult(candidates[:final_top_k], candidates, 8)


class RetryRecoversContractRerankProvider(LowScoreContractRerankProvider):
    """首次合同证据低于门槛，固定补充查询可以召回可靠证据。"""

    def rerank(self, query, hits, final_top_k, *, instruct=None):
        is_contract = instruct is not None and "current contract" in instruct
        if is_contract and "补充检索" in query:
            return FakeRerankProvider.rerank(
                self, query, hits, final_top_k, instruct=instruct
            )
        return super().rerank(query, hits, final_top_k, instruct=instruct)


class PolicyThresholdRerankProvider(FakeRerankProvider):
    """制度 Top 5 中只有前两条达到 0.60，用于验证逐条阈值。"""

    def rerank(self, query, hits, final_top_k, *, instruct=None):
        if instruct is not None and "current contract" in instruct:
            return super().rerank(
                query, hits, final_top_k, instruct=instruct
            )
        scores = [0.9, 0.7, 0.59, 0.4, 0.2]
        candidates = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": index,
                "rerank_score": scores[index - 1] if index <= len(scores) else 0.1,
                "selected_for_context": index <= final_top_k,
            }
            for index, hit in enumerate(hits, 1)
        ]
        return PolicyRerankResult(candidates[:final_top_k], candidates, 9)


class NeverCalledModelProvider:
    """查询级门槛拒绝合同时，聊天模型不应被调用。"""

    def judge(self, check_item, contract_chunks, policy_chunks):
        raise AssertionError("合同证据低于门槛时不应调用聊天模型")


class FakeRepository:
    """记录 LangGraph 的调用顺序，用于验证四项检查和最终汇总。"""

    def __init__(self) -> None:
        self.contract_id = uuid4()
        self.document_id = uuid4()
        self.workflow_id = uuid4()
        self.saved_codes: list[str] = []
        self.check_codes: dict[object, str] = {}
        self.lock = Lock()
        self.contract_top_k_values: list[int] = []
        self.policy_top_k_values: list[int] = []
        self.retrieval_records: list[dict[str, Any]] = []
        self.saved_decisions: list[ModelRiskDecision] = []
        self.node_names: dict[object, str] = {}
        self.finished_nodes: list[dict[str, Any]] = []

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
        node_run_id = uuid4()
        with self.lock:
            self.node_names[node_run_id] = node_name
        return node_run_id

    def finish_node(self, node_run_id, output_data):
        with self.lock:
            self.finished_nodes.append(
                {"node_name": self.node_names[node_run_id], "output_data": output_data}
            )
        return None

    def update_progress(self, job_id, progress):
        return None

    def update_check_progress(self, job_id, review_run_id):
        return None

    def search_contract_chunks(self, document_id, query_vector, top_k=3):
        with self.lock:
            self.contract_top_k_values.append(top_k)
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
            self.saved_decisions.append(decision)
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
            self.assertIn("enterprise policy clauses", payload["instruct"])
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
        self.assertEqual([20, 20, 20, 20], sorted(repository.contract_top_k_values))
        self.assertEqual([10, 10, 10, 10], sorted(repository.policy_top_k_values))
        self.assertTrue(
            all(contents[0] == "制度候选10" for contents in model_provider.policy_contents)
        )
        rerank_records = [
            record
            for record in repository.retrieval_records
            if record["kwargs"].get("ranking_strategy") == "RERANK"
        ]
        self.assertEqual(8, len(rerank_records))
        policy_records = [
            record
            for record in rerank_records
            if record["args"][2].get("chunk_type") == "POLICY_SECTION"
        ]
        self.assertEqual(4, len(policy_records))
        self.assertTrue(all(len(record["args"][3]) == 10 for record in policy_records))
        check_outputs = [
            node["output_data"]
            for node in repository.finished_nodes
            if "route" in node["output_data"]
        ]
        self.assertEqual(4, len(check_outputs))
        self.assertTrue(
            all(
                output["route"] == "MODEL_JUDGMENT"
                and output["retrieval_attempts"] == 1
                and output["retried_sources"] == []
                for output in check_outputs
            )
        )

    def test_insufficient_contract_evidence_retries_once_then_stops(self) -> None:
        repository = FakeRepository()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=NeverCalledModelProvider(),
            rerank_provider=LowScoreContractRerankProvider(),
        )

        service.run(uuid4(), uuid4())

        contract_records = [
            record
            for record in repository.retrieval_records
            if record["args"][2].get("chunk_type") == "CONTRACT_CLAUSE"
        ]
        policy_records = [
            record
            for record in repository.retrieval_records
            if record["args"][2].get("chunk_type") == "POLICY_SECTION"
        ]
        self.assertEqual(8, len(contract_records))
        self.assertEqual(4, len(policy_records))
        self.assertEqual(
            [1, 1, 1, 1, 2, 2, 2, 2],
            sorted(record["args"][2]["attempt"] for record in contract_records),
        )
        self.assertTrue(
            all(
                record["args"][2]["query_kind"] == "SUPPLEMENTAL"
                for record in contract_records
                if record["args"][2]["attempt"] == 2
            )
        )
        check_outputs = [
            node["output_data"]
            for node in repository.finished_nodes
            if "route" in node["output_data"]
        ]
        self.assertTrue(
            all(
                output["route"] == "INSUFFICIENT_INFORMATION"
                and output["retrieval_attempts"] == 2
                and output["initial_missing_sources"] == ["CONTRACT"]
                and output["retried_sources"] == ["CONTRACT"]
                and output["final_missing_sources"] == ["CONTRACT"]
                for output in check_outputs
            )
        )

    def test_retry_recovers_missing_contract_evidence_then_calls_model(self) -> None:
        repository = FakeRepository()
        model_provider = FakeModelProvider()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=model_provider,
            rerank_provider=RetryRecoversContractRerankProvider(),
        )

        service.run(uuid4(), uuid4())

        self.assertEqual(8, len(repository.contract_top_k_values))
        self.assertEqual(4, len(repository.policy_top_k_values))
        self.assertTrue(
            all(decision.status == "RISK" for decision in repository.saved_decisions)
        )
        check_outputs = [
            node["output_data"]
            for node in repository.finished_nodes
            if "route" in node["output_data"]
        ]
        self.assertTrue(
            all(
                output["route"] == "MODEL_JUDGMENT"
                and output["retrieval_attempts"] == 2
                and output["initial_missing_sources"] == ["CONTRACT"]
                and output["retried_sources"] == ["CONTRACT"]
                and output["final_missing_sources"] == []
                for output in check_outputs
            )
        )

    def test_contract_query_below_threshold_skips_chat_model(self) -> None:
        repository = FakeRepository()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=NeverCalledModelProvider(),
            rerank_provider=LowScoreContractRerankProvider(),
        )

        service.run(uuid4(), uuid4())

        self.assertEqual(4, len(repository.saved_decisions))
        self.assertTrue(
            all(
                decision.status == "INSUFFICIENT_INFORMATION"
                for decision in repository.saved_decisions
            )
        )
        contract_records = [
            record
            for record in repository.retrieval_records
            if record["args"][2].get("chunk_type") == "CONTRACT_CLAUSE"
        ]
        self.assertTrue(
            all(
                record["args"][2]["confidence_band"] == "REJECTED"
                for record in contract_records
            )
        )
        self.assertTrue(
            all(
                not candidate["selected_for_context"]
                for record in contract_records
                for candidate in record["args"][3]
            )
        )

    def test_policy_threshold_only_keeps_candidates_at_or_above_minimum(self) -> None:
        repository = FakeRepository()
        model_provider = FakeModelProvider()
        service = RiskReviewService(
            repository=repository,
            embedding_provider=FakeEmbeddingProvider(),
            model_provider=model_provider,
            rerank_provider=PolicyThresholdRerankProvider(),
        )

        service.run(uuid4(), uuid4())

        self.assertTrue(
            all(
                contents == ["制度候选1", "制度候选2"]
                for contents in model_provider.policy_contents
            )
        )

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
        self.assertEqual(8, len(fallback_records))
        self.assertTrue(
            all("模拟重排序服务不可用" in record["kwargs"]["rerank_error"] for record in fallback_records)
        )

    def test_reference_selection_rejects_unknown_and_duplicate_labels(self) -> None:
        chunks = [FakeRepository._chunk("证据一"), FakeRepository._chunk("证据二")]

        selected = _select_references(["c1", "C9", "C1", "C2"], chunks, "C")

        self.assertEqual([chunks[0], chunks[1]], selected)


if __name__ == "__main__":
    unittest.main()
