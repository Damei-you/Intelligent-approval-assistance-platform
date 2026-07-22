from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.modules.contract_import.schemas import ContractJsonImportRequest
from app.modules.policy_import.schemas import PolicyJsonImportRequest


DATASET_ROOT = Path(__file__).resolve().parents[1] / "examples" / "evaluation"
CHECK_CODES = {
    "PAYMENT_TERMS",
    "WARRANTY",
    "BREACH_LIABILITY",
    "DISPUTE_RESOLUTION",
}


class EvaluationDatasetTests(unittest.TestCase):
    """验证评测文件格式和标准答案引用，测试过程不会写数据库或调用模型。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts: dict[str, ContractJsonImportRequest] = {}
        for path in sorted((DATASET_ROOT / "contracts").glob("*.json")):
            payload = ContractJsonImportRequest.model_validate(_load_json(path))
            case_id = payload.metadata["evaluation_case_id"]
            cls.contracts[case_id] = payload

        cls.policies = [
            PolicyJsonImportRequest.model_validate(_load_json(path))
            for path in sorted((DATASET_ROOT / "policies").glob("*.json"))
        ]
        cls.expected = _load_json(DATASET_ROOT / "expected-results.json")
        cls.rerank = _load_json(DATASET_ROOT / "rerank-judgements.json")

    def test_all_json_contracts_and_policies_match_import_schemas(self) -> None:
        self.assertEqual(13, len(self.contracts))
        self.assertEqual(2, len(self.policies))
        self.assertEqual(
            {"V1.0", "V2.0"},
            {policy.version for policy in self.policies},
        )
        self.assertEqual(1, len({policy.policy_no for policy in self.policies}))

    def test_expected_results_reference_existing_clauses_and_sections(self) -> None:
        current_policy = next(policy for policy in self.policies if policy.version == "V2.0")
        policy_sections = {section.section_no for section in current_policy.sections}
        expected_case_ids = set()

        for case in self.expected["cases"]:
            case_id = case["case_id"]
            expected_case_ids.add(case_id)
            contract = self.contracts[case_id]
            clause_nos = {clause.clause_no for clause in contract.clauses}
            self.assertEqual(CHECK_CODES, set(case["expected"]))

            for result in case["expected"].values():
                self.assertIn(
                    result["status"],
                    {"PASS", "RISK", "INSUFFICIENT_INFORMATION"},
                )
                self.assertTrue(set(result["contract_clause_nos"]) <= clause_nos)
                self.assertTrue(set(result["policy_section_nos"]) <= policy_sections)
                self.assertTrue(
                    set(result.get("forbidden_contract_clause_nos", [])) <= clause_nos
                )
                self.assertTrue(
                    set(result.get("forbidden_policy_section_nos", [])) <= policy_sections
                )

        self.assertEqual(set(self.contracts), expected_case_ids)

    def test_rerank_judgements_reference_existing_candidates(self) -> None:
        current_policy = next(policy for policy in self.policies if policy.version == "V2.0")
        policy_sections = {section.section_no for section in current_policy.sections}

        for query in self.rerank["queries"]:
            self.assertIn(query["check_code"], CHECK_CODES)
            contract = self.contracts[query["case_id"]]
            clause_nos = {clause.clause_no for clause in contract.clauses}
            contract_relevance = {
                judgement["relevance"] for judgement in query["contract_judgements"]
            }
            policy_relevance = {
                judgement["relevance"] for judgement in query["policy_judgements"]
            }

            self.assertTrue(
                {
                    judgement["clause_no"]
                    for judgement in query["contract_judgements"]
                }
                <= clause_nos
            )
            self.assertTrue(
                {
                    judgement["section_no"]
                    for judgement in query["policy_judgements"]
                }
                <= policy_sections
            )
            # 每组重排序样本都必须同时包含正样本和困难负样本，否则无法体现排序提升。
            self.assertIn(2, contract_relevance)
            self.assertIn(0, contract_relevance)
            self.assertIn(2, policy_relevance)
            self.assertIn(0, policy_relevance)

    def test_stress_dataset_has_expected_scale_and_valid_judgements(self) -> None:
        stress_root = DATASET_ROOT / "stress"
        contract = ContractJsonImportRequest.model_validate(
            _load_json(stress_root / "contract-50-clauses.json")
        )
        policy = PolicyJsonImportRequest.model_validate(
            _load_json(stress_root / "policy-100-sections.json")
        )
        expected = _load_json(stress_root / "expected-results.json")
        rerank = _load_json(stress_root / "rerank-judgements.json")
        clause_nos = {clause.clause_no for clause in contract.clauses}
        section_nos = {section.section_no for section in policy.sections}

        self.assertEqual(50, len(contract.clauses))
        self.assertEqual(100, len(policy.sections))
        self.assertEqual(CHECK_CODES, set(expected["expected"]))
        self.assertEqual(CHECK_CODES, {query["check_code"] for query in rerank["queries"]})

        for result in expected["expected"].values():
            self.assertTrue(set(result["contract_clause_nos"]) <= clause_nos)
            self.assertTrue(set(result["policy_section_nos"]) <= section_nos)

        for query in rerank["queries"]:
            contract_judgements = query["contract_judgements"]
            policy_judgements = query["policy_judgements"]
            self.assertTrue(
                {item["clause_no"] for item in contract_judgements} <= clause_nos
            )
            self.assertTrue(
                {item["section_no"] for item in policy_judgements} <= section_nos
            )
            self.assertIn(2, {item["relevance"] for item in contract_judgements})
            self.assertIn(0, {item["relevance"] for item in contract_judgements})
            self.assertIn(2, {item["relevance"] for item in policy_judgements})
            self.assertIn(0, {item["relevance"] for item in policy_judgements})


def _load_json(path: Path) -> dict:
    """统一按 UTF-8 读取测试数据，避免 Windows 默认编码影响中文。"""

    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
