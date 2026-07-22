from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from tools.evaluate_rag import (
    PROJECT_ROOT,
    load_dataset,
    main,
    ranking_metrics,
    render_markdown,
    prepare_dataset,
    validate_dataset,
)


STRESS_DATASET = PROJECT_ROOT / "examples" / "evaluation" / "stress"


class EvaluationToolTests(unittest.TestCase):
    """验证评测工具的离线逻辑，不连接数据库，也不调用外部模型。"""

    def test_stress_dataset_passes_validation(self) -> None:
        """压力集应满足 50/100 规模且四项标注引用都存在。"""

        result = validate_dataset(load_dataset(STRESS_DATASET))

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["contract_clause_count"], 50)
        self.assertEqual(result["policy_section_count"], 100)

    def test_ranking_metrics_support_graded_relevance(self) -> None:
        """MRR 使用直接相关项，NDCG 同时区分部分相关和无关项。"""

        metrics = ranking_metrics(
            ranked_ids=["C", "B", "A"],
            grades={"A": 2, "B": 1, "C": 0},
            k=3,
        )

        self.assertEqual(metrics["recall_at_k"], 1.0)
        self.assertAlmostEqual(metrics["precision_at_k"], 1 / 3, places=5)
        self.assertAlmostEqual(metrics["mrr"], 1 / 3, places=5)
        self.assertGreater(metrics["ndcg_at_k"], 0)
        self.assertLess(metrics["ndcg_at_k"], 1)

    def test_validate_mode_writes_json_and_markdown(self) -> None:
        """默认安全模式应输出 JSON 与 Markdown，且无需 API Key。"""

        output_root = PROJECT_ROOT / "output"
        output_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as directory:
            exit_code = main(
                [
                    "--mode",
                    "validate",
                    "--dataset",
                    str(STRESS_DATASET),
                    "--output-dir",
                    directory,
                    "--label",
                    "offline-test",
                ]
            )
            json_path = Path(directory) / "offline-test.json"
            markdown_path = Path(directory) / "offline-test.md"

            self.assertEqual(exit_code, 0)
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["validation"]["passed"])
            self.assertIn("RAG 自动评测报告", markdown_path.read_text(encoding="utf-8"))

    def test_markdown_contains_retrieval_summary(self) -> None:
        """检索报告应显示四个核心排序指标。"""

        metrics = {
            "recall_at_k": 1.0,
            "precision_at_k": 0.25,
            "mrr": 0.5,
            "ndcg_at_k": 0.75,
        }
        report = {
            "generated_at": "2026-07-22T00:00:00+00:00",
            "dataset": str(STRESS_DATASET),
            "validation": {
                "passed": True,
                "contract_clause_count": 50,
                "policy_section_count": 100,
                "errors": [],
            },
            "retrieval": {
                "aggregate": {"contract": metrics, "policy": metrics},
                "duration_ms": 123.4,
            },
        }

        markdown = render_markdown(report)

        self.assertIn("Recall@K", markdown)
        self.assertIn("NDCG@K", markdown)
        self.assertIn("123.4 ms", markdown)

    def test_prepare_injects_repositories_into_import_services(self) -> None:
        """准备数据时应按业务 Service 的构造要求显式传入 Repository。"""

        dataset = load_dataset(STRESS_DATASET)
        contract_document_id = uuid4()
        policy_document_id = uuid4()
        missing = {
            "contract_id": None,
            "contract_document_id": None,
            "contract_chunk_count": 0,
            "contract_vector_count": 0,
            "contract_matches_dataset": False,
            "policy_document_id": None,
            "policy_chunk_count": 0,
            "policy_vector_count": 0,
            "policy_matches_dataset": False,
        }
        ready = {
            "contract_id": uuid4(),
            "contract_document_id": contract_document_id,
            "contract_chunk_count": 50,
            "contract_vector_count": 50,
            "contract_matches_dataset": True,
            "policy_document_id": policy_document_id,
            "policy_chunk_count": 100,
            "policy_vector_count": 100,
            "policy_matches_dataset": True,
        }
        with (
            patch("tools.evaluate_rag._find_resources", side_effect=[missing, ready, ready]),
            patch("tools.evaluate_rag._wait_for_vectors"),
            patch("tools.evaluate_rag.PolicyImportService") as policy_service,
            patch("tools.evaluate_rag.ContractImportService") as contract_service,
        ):
            policy_service.return_value.import_json.return_value = SimpleNamespace(
                document_id=policy_document_id
            )
            contract_service.return_value.import_json.return_value = SimpleNamespace(
                document_id=contract_document_id
            )

            result = prepare_dataset(dataset, timeout_seconds=1)

        self.assertEqual(ready, result)
        self.assertEqual(1, len(policy_service.call_args.args))
        self.assertEqual(1, len(contract_service.call_args.args))


if __name__ == "__main__":
    unittest.main()
