"""RAG 检索与风险审查自动评测工具。

默认只校验测试数据，不访问数据库，也不会调用外部模型。检索评测、准备数据和
发起风险审查会调用百炼 API，因此必须显式传入 ``--confirm-external-calls``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID


# 直接执行 ``python tools/evaluate_rag.py`` 时，Python 默认只把 tools 目录加入模块
# 搜索路径。这里补充项目根目录，使脚本可以复用 app 中的真实服务和配置。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import open_connection  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.modules.contract_import.repository import ContractImportRepository  # noqa: E402
from app.modules.contract_import.schemas import ContractJsonImportRequest  # noqa: E402
from app.modules.contract_import.service import ContractImportService  # noqa: E402
from app.modules.policy_import.repository import PolicyImportRepository  # noqa: E402
from app.modules.policy_import.schemas import PolicyJsonImportRequest  # noqa: E402
from app.modules.policy_import.service import PolicyImportService  # noqa: E402
from app.modules.risk_review.repository import RiskReviewRepository  # noqa: E402
from app.modules.risk_review.service import (  # noqa: E402
    CHECK_QUERIES,
    CONTRACT_RERANK_INSTRUCT,
    DashScopeRerankProvider,
    POLICY_RERANK_INSTRUCT,
    RiskReviewService,
)
from app.modules.vectorization.service import (  # noqa: E402
    DashScopeEmbeddingProvider,
    VectorizationRepository,
    enqueue_document_vectorization,
)


CHECK_TYPES = (
    "PAYMENT_TERMS",
    "WARRANTY",
    "BREACH_LIABILITY",
    "DISPUTE_RESOLUTION",
)
TERMINAL_REVIEW_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class EvaluationDataset:
    """内存中的压力测试数据集及其标准答案。"""

    root: Path
    contract: dict[str, Any]
    policy: dict[str, Any]
    expected: dict[str, Any]
    judgements: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON，并将文件路径保留在异常信息中。"""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 文件 {path}: {exc}") from exc


def load_dataset(dataset_dir: Path) -> EvaluationDataset:
    """加载固定命名的 50 条合同、100 条制度压力测试集。"""

    root = dataset_dir.resolve()
    return EvaluationDataset(
        root=root,
        contract=_read_json(root / "contract-50-clauses.json"),
        policy=_read_json(root / "policy-100-sections.json"),
        expected=_read_json(root / "expected-results.json"),
        judgements=_read_json(root / "rerank-judgements.json"),
    )


def validate_dataset(dataset: EvaluationDataset) -> dict[str, Any]:
    """校验数据结构、编号唯一性和标注引用，完全离线运行。"""

    # 复用业务请求模型，使测试数据和真实导入接口遵循同一套 Pydantic 校验规则。
    contract = ContractJsonImportRequest.model_validate(dataset.contract)
    policy = PolicyJsonImportRequest.model_validate(dataset.policy)
    contract_ids = [clause.clause_no for clause in contract.clauses]
    policy_ids = [section.section_no for section in policy.sections]
    errors: list[str] = []

    if len(contract_ids) != 50:
        errors.append(f"合同条款应为 50 条，实际为 {len(contract_ids)} 条")
    if len(policy_ids) != 100:
        errors.append(f"制度条款应为 100 条，实际为 {len(policy_ids)} 条")
    if len(set(contract_ids)) != len(contract_ids):
        errors.append("合同条款编号存在重复")
    if len(set(policy_ids)) != len(policy_ids):
        errors.append("制度条款编号存在重复")

    expected_by_type = dataset.expected.get("expected", {})
    judgement_by_type = {
        item["check_code"]: item for item in dataset.judgements.get("queries", [])
    }
    for check_type in CHECK_TYPES:
        expected = expected_by_type.get(check_type)
        judgement = judgement_by_type.get(check_type)
        if expected is None:
            errors.append(f"expected-results.json 缺少 {check_type}")
            continue
        if judgement is None:
            errors.append(f"rerank-judgements.json 缺少 {check_type}")
            continue
        for clause_no in expected.get("contract_clause_nos", []):
            if clause_no not in contract_ids:
                errors.append(f"{check_type} 引用了不存在的合同条款 {clause_no}")
        for section_no in expected.get("policy_section_nos", []):
            if section_no not in policy_ids:
                errors.append(f"{check_type} 引用了不存在的制度条款 {section_no}")
        for candidate in judgement.get("contract_judgements", []):
            if candidate["clause_no"] not in contract_ids:
                errors.append(
                    f"{check_type} 检索标注引用不存在的合同条款 {candidate['clause_no']}"
                )
        for candidate in judgement.get("policy_judgements", []):
            if candidate["section_no"] not in policy_ids:
                errors.append(
                    f"{check_type} 检索标注引用不存在的制度条款 {candidate['section_no']}"
                )

    return {
        "passed": not errors,
        "contract_no": contract.contract_no,
        "policy_no": policy.policy_no,
        "contract_clause_count": len(contract_ids),
        "policy_section_count": len(policy_ids),
        "check_types": list(CHECK_TYPES),
        "errors": errors,
    }


def _find_resources(dataset: EvaluationDataset) -> dict[str, Any]:
    """查找当前合同与制度文档，并统计向量化完成情况。"""

    contract_no = dataset.contract["contract_no"]
    policy_no = dataset.policy["policy_no"]
    with open_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id AS contract_id, d.id AS document_id,
                       COUNT(dc.id) AS chunk_count,
                       COUNT(dc.embedding) AS vector_count
                FROM contracts c
                JOIN documents d ON d.contract_id = c.id AND d.is_current = TRUE
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                WHERE c.contract_no = %s
                GROUP BY c.id, d.id
                ORDER BY d.revision_no DESC
                LIMIT 1
                """,
                (contract_no,),
            )
            contract_row = cursor.fetchone()
            cursor.execute(
                """
                SELECT d.id AS document_id, COUNT(dc.id) AS chunk_count,
                       COUNT(dc.embedding) AS vector_count
                FROM documents d
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                WHERE d.document_type = 'POLICY'
                  AND d.is_current = TRUE
                  AND d.metadata ->> 'policy_no' = %s
                GROUP BY d.id
                ORDER BY MAX(d.created_at) DESC
                LIMIT 1
                """,
                (policy_no,),
            )
            policy_row = cursor.fetchone()

            contract_matches_dataset = False
            if contract_row:
                cursor.execute(
                    """
                    SELECT clause_no, title, content
                    FROM document_chunks
                    WHERE document_id = %s
                    ORDER BY chunk_index
                    """,
                    (contract_row["document_id"],),
                )
                contract_matches_dataset = _content_fingerprint(cursor.fetchall()) == (
                    _content_fingerprint(dataset.contract["clauses"])
                )
            policy_matches_dataset = False
            if policy_row:
                cursor.execute(
                    """
                    SELECT clause_no, title, content
                    FROM document_chunks
                    WHERE document_id = %s
                    ORDER BY chunk_index
                    """,
                    (policy_row["document_id"],),
                )
                policy_matches_dataset = _content_fingerprint(cursor.fetchall()) == (
                    _content_fingerprint(dataset.policy["sections"], number_key="section_no")
                )

    return {
        "contract_id": contract_row["contract_id"] if contract_row else None,
        "contract_document_id": contract_row["document_id"] if contract_row else None,
        "contract_chunk_count": int(contract_row["chunk_count"]) if contract_row else 0,
        "contract_vector_count": int(contract_row["vector_count"]) if contract_row else 0,
        "contract_matches_dataset": contract_matches_dataset,
        "policy_document_id": policy_row["document_id"] if policy_row else None,
        "policy_chunk_count": int(policy_row["chunk_count"]) if policy_row else 0,
        "policy_vector_count": int(policy_row["vector_count"]) if policy_row else 0,
        "policy_matches_dataset": policy_matches_dataset,
    }


def _content_fingerprint(
    items: Iterable[Any], number_key: str = "clause_no"
) -> str:
    """用编号、标题和正文生成指纹，避免复用同编号但内容不同的旧测试文档。"""

    normalized = []
    for item in items:
        # psycopg Row 和普通 dict 都支持按列名取值；制度 JSON 使用 section_no，
        # 数据库存储时则统一映射成 clause_no。
        stored_number_key = "clause_no" if not isinstance(item, dict) else number_key
        normalized.append(
            {
                "number": item[stored_number_key],
                "title": item["title"],
                "content": item["content"],
            }
        )
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _wait_for_vectors(document_ids: Iterable[UUID], timeout_seconds: int) -> None:
    """轮询 PostgreSQL 中的向量任务状态，Redis/Celery worker 必须已经运行。"""

    repository = VectorizationRepository()
    deadline = time.monotonic() + timeout_seconds
    pending = set(document_ids)
    while pending:
        for document_id in tuple(pending):
            status = repository.get_document_status(document_id)
            if status.status == "SUCCEEDED":
                pending.remove(document_id)
            elif status.status == "FAILED":
                raise RuntimeError(
                    f"文档 {document_id} 向量化失败：{status.error_message or '未记录错误'}"
                )
        if not pending:
            return
        if time.monotonic() >= deadline:
            ids = ", ".join(str(item) for item in pending)
            raise TimeoutError(
                f"等待向量化超时：{ids}。请确认 Redis 和 Celery worker 已启动。"
            )
        time.sleep(1.5)


def prepare_dataset(dataset: EvaluationDataset, timeout_seconds: int) -> dict[str, Any]:
    """按业务服务导入缺失数据，并等待异步向量化完成。"""

    resources = _find_resources(dataset)
    newly_imported: set[UUID] = set()
    if resources["policy_document_id"] is None or not resources["policy_matches_dataset"]:
        result = PolicyImportService(PolicyImportRepository()).import_json(
            PolicyJsonImportRequest.model_validate(dataset.policy)
        )
        newly_imported.add(result.document_id)
    if resources["contract_document_id"] is None or not resources["contract_matches_dataset"]:
        result = ContractImportService(ContractImportRepository()).import_json(
            ContractJsonImportRequest.model_validate(dataset.contract)
        )
        newly_imported.add(result.document_id)

    resources = _find_resources(dataset)
    vectorization_repository = VectorizationRepository()
    for prefix in ("contract", "policy"):
        document_id = resources[f"{prefix}_document_id"]
        if document_id is None:
            raise RuntimeError(f"{prefix} 测试数据导入后仍未找到文档")
        if resources[f"{prefix}_vector_count"] < resources[f"{prefix}_chunk_count"]:
            # 新导入文档已经由导入服务投递任务。复用已有文档时先读取最新任务状态，
            # 避免评测脚本重启后给仍在运行的同一文档重复投递 Celery 任务。
            if document_id not in newly_imported:
                status = vectorization_repository.get_document_status(document_id)
                if status.status not in {"QUEUED", "RUNNING", "RETRYING"}:
                    enqueue_document_vectorization(document_id)
            newly_imported.add(document_id)

    if newly_imported:
        _wait_for_vectors(newly_imported, timeout_seconds)
    resources = _find_resources(dataset)
    _require_vectorized(resources)
    return resources


def _require_vectorized(resources: dict[str, Any]) -> None:
    """在检索前确保两个目标文档存在且每个分块都有向量。"""

    for prefix, label in (("contract", "合同"), ("policy", "制度")):
        if resources[f"{prefix}_document_id"] is None:
            raise RuntimeError(f"未找到测试{label}，请先使用 --prepare")
        if not resources[f"{prefix}_matches_dataset"]:
            raise RuntimeError(
                f"当前测试{label}编号相同但内容与压力集不一致，请使用 --prepare 创建新修订"
            )
        chunks = resources[f"{prefix}_chunk_count"]
        vectors = resources[f"{prefix}_vector_count"]
        if chunks == 0 or vectors != chunks:
            raise RuntimeError(
                f"测试{label}尚未完成向量化（{vectors}/{chunks}），请使用 --prepare，"
                "并确认 Redis 和 Celery worker 已启动"
            )


def ranking_metrics(ranked_ids: list[str], grades: dict[str, int], k: int) -> dict[str, float]:
    """计算严格相关召回、准确率、MRR 和支持分级相关性的 NDCG。"""

    selected = ranked_ids[:k]
    direct_relevant = {item_id for item_id, grade in grades.items() if grade >= 2}
    direct_hits = [item_id for item_id in selected if grades.get(item_id, 0) >= 2]
    recall = len(set(direct_hits)) / len(direct_relevant) if direct_relevant else 1.0
    precision = len(direct_hits) / k if k else 0.0
    first_rank = next(
        (rank for rank, item_id in enumerate(selected, 1) if grades.get(item_id, 0) >= 2),
        None,
    )
    mrr = 1.0 / first_rank if first_rank else 0.0

    def dcg(values: Iterable[int]) -> float:
        return sum(
            (2**grade - 1) / math.log2(rank + 1)
            for rank, grade in enumerate(values, 1)
        )

    actual_dcg = dcg(grades.get(item_id, 0) for item_id in selected)
    ideal_dcg = dcg(sorted(grades.values(), reverse=True)[:k])
    ndcg = actual_dcg / ideal_dcg if ideal_dcg else 1.0
    return {
        "recall_at_k": round(recall, 6),
        "precision_at_k": round(precision, 6),
        "mrr": round(mrr, 6),
        "ndcg_at_k": round(ndcg, 6),
    }


def _grades_for_query(query: dict[str, Any], key: str, id_key: str) -> dict[str, int]:
    return {item[id_key]: int(item["relevance"]) for item in query.get(key, [])}


def evaluate_retrieval(
    dataset: EvaluationDataset,
    resources: dict[str, Any],
    contract_top_k: int,
    policy_top_k: int,
    contract_rerank: bool = False,
    contract_final_top_k: int = 5,
    contract_query_min_score: float = 0.45,
    policy_rerank: bool = False,
    policy_final_top_k: int = 5,
    policy_min_score: float = 0.60,
) -> dict[str, Any]:
    """调用真实检索，并可按正式审查链路重排合同和制度候选。"""

    _require_vectorized(resources)
    provider = DashScopeEmbeddingProvider()
    queries = [CHECK_QUERIES[check_type] for check_type in CHECK_TYPES]
    started = time.perf_counter()
    vectors = provider.embed_documents(queries)
    repository = RiskReviewRepository()
    rerank_provider = (
        DashScopeRerankProvider() if contract_rerank or policy_rerank else None
    )
    judgement_by_type = {
        item["check_code"]: item for item in dataset.judgements["queries"]
    }
    items: list[dict[str, Any]] = []
    for check_type, vector in zip(CHECK_TYPES, vectors, strict=True):
        contract_hits = repository.search_contract_chunks(
            resources["contract_document_id"], vector, top_k=contract_top_k
        )
        contract_candidate_hits = contract_hits
        if contract_rerank and rerank_provider is not None:
            contract_result = rerank_provider.rerank(
                CHECK_QUERIES[check_type],
                contract_candidate_hits,
                contract_final_top_k,
                instruct=CONTRACT_RERANK_INSTRUCT,
            )
            contract_candidate_hits = contract_result.all_hits
            contract_hits = contract_result.selected_hits
            first_score = (
                contract_hits[0].get("rerank_score") if contract_hits else None
            )
            if (
                not isinstance(first_score, (int, float))
                or first_score < contract_query_min_score
            ):
                contract_hits = []
        policy_hits = repository.search_policy_chunks(vector, top_k=policy_top_k)
        policy_candidate_hits = policy_hits
        if policy_rerank and rerank_provider is not None:
            policy_result = rerank_provider.rerank(
                CHECK_QUERIES[check_type],
                policy_candidate_hits,
                policy_final_top_k,
                instruct=POLICY_RERANK_INSTRUCT,
            )
            policy_candidate_hits = policy_result.all_hits
            policy_hits = [
                hit
                for hit in policy_result.selected_hits
                if isinstance(hit.get("rerank_score"), (int, float))
                and hit["rerank_score"] >= policy_min_score
            ]
        judgement = judgement_by_type[check_type]
        contract_grades = _grades_for_query(
            judgement, "contract_judgements", "clause_no"
        )
        policy_grades = _grades_for_query(
            judgement, "policy_judgements", "section_no"
        )
        contract_ranked = [
            _ranked_id(hit, resources["contract_document_id"]) for hit in contract_hits
        ]
        policy_ranked = [
            _ranked_id(hit, resources["policy_document_id"]) for hit in policy_hits
        ]
        contract_metric_k = contract_final_top_k if contract_rerank else contract_top_k
        policy_metric_k = policy_final_top_k if policy_rerank else policy_top_k
        items.append(
            {
                "check_type": check_type,
                "query": CHECK_QUERIES[check_type],
                "contract": {
                    "candidate_k": contract_top_k,
                    "k": contract_metric_k,
                    "metrics": ranking_metrics(
                        contract_ranked, contract_grades, contract_metric_k
                    ),
                    "hits": [
                        _serialize_hit(
                            hit, contract_grades, resources["contract_document_id"]
                        )
                        for hit in contract_hits
                    ],
                    "vector_candidates": [
                        _serialize_hit(
                            hit, contract_grades, resources["contract_document_id"]
                        )
                        for hit in contract_candidate_hits
                    ] if contract_rerank else [],
                },
                "policy": {
                    "candidate_k": policy_top_k,
                    "k": policy_metric_k,
                    "metrics": ranking_metrics(
                        policy_ranked, policy_grades, policy_metric_k
                    ),
                    "hits": [
                        _serialize_hit(hit, policy_grades, resources["policy_document_id"])
                        for hit in policy_hits
                    ],
                    "vector_candidates": [
                        _serialize_hit(
                            hit, policy_grades, resources["policy_document_id"]
                        )
                        for hit in policy_candidate_hits
                    ] if policy_rerank else [],
                },
            }
        )

    aggregate: dict[str, dict[str, float]] = {}
    for source in ("contract", "policy"):
        aggregate[source] = {
            metric: round(
                sum(item[source]["metrics"][metric] for item in items) / len(items), 6
            )
            for metric in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k")
        }
    return {
        "ranking": (
            "contract_policy_rerank"
            if contract_rerank and policy_rerank
            else "contract_rerank"
            if contract_rerank
            else "policy_rerank"
            if policy_rerank
            else "vector_baseline"
        ),
        "embedding_model": "text-embedding-v4",
        "rerank_model": (
            settings.rerank_model if contract_rerank or policy_rerank else None
        ),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "aggregate": aggregate,
        "checks": items,
    }


def _ranked_id(hit: dict[str, Any], expected_document_id: UUID) -> str:
    """只有目标压力集文档中的编号才可命中标准答案。"""

    if hit["document_id"] != expected_document_id:
        return ""
    return hit.get("clause_no") or ""


def _serialize_hit(
    hit: dict[str, Any], grades: dict[str, int], expected_document_id: UUID
) -> dict[str, Any]:
    """只保存排名所需元数据，避免把完整合同正文写进评测报告。"""

    item_id = _ranked_id(hit, expected_document_id)
    payload = {
        "chunk_id": str(hit["id"]),
        "document_id": str(hit["document_id"]),
        "document_title": hit["document_title"],
        "clause_no": hit.get("clause_no") or "",
        "clause_title": hit.get("title"),
        "similarity_score": round(float(hit["similarity_score"]), 6),
        "relevance": grades.get(item_id, 0),
    }
    if hit.get("rerank_rank_no") is not None:
        payload["vector_rank_no"] = hit.get("vector_rank_no")
        payload["rerank_rank_no"] = hit["rerank_rank_no"]
        payload["rerank_score"] = round(float(hit["rerank_score"]), 6)
    return payload


def _latest_successful_review(contract_id: UUID) -> UUID | None:
    with open_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM review_runs
                WHERE contract_id = %s AND status = 'SUCCEEDED'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (contract_id,),
            )
            row = cursor.fetchone()
    return row[0] if row else None


def _wait_for_review(review_run_id: UUID, timeout_seconds: int) -> Any:
    """等待 Celery 中的审查任务结束，过程状态实际持久化在 PostgreSQL。"""

    repository = RiskReviewRepository()
    deadline = time.monotonic() + timeout_seconds
    while True:
        detail = repository.get_review_detail(review_run_id)
        if detail.status in TERMINAL_REVIEW_STATUSES:
            return detail
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"等待审查任务 {review_run_id} 超时，请检查 Celery worker 日志"
            )
        time.sleep(1.5)


def _load_evidence_document_ids(detail: Any) -> dict[UUID, UUID]:
    """补查证据所属文档，防止其他制度中同名编号被误算为正确命中。"""

    chunk_ids = [
        evidence.chunk_id
        for finding in detail.findings
        for evidence in finding.evidence
    ]
    if not chunk_ids:
        return {}
    with open_connection() as connection:
        rows = connection.execute(
            "SELECT id, document_id FROM document_chunks WHERE id = ANY(%s)",
            (chunk_ids,),
        ).fetchall()
    return {row["id"]: row["document_id"] for row in rows}


def _load_review_observability(review_run_id: UUID, detail: Any) -> dict[str, Any]:
    """汇总 LangGraph 节点耗时和 LLM token，便于比较优化前后的成本。"""

    with open_connection() as connection:
        nodes = connection.execute(
            """
            SELECT node.node_name, node.status, node.latency_ms
            FROM workflow_runs workflow
            JOIN workflow_node_runs node ON node.workflow_run_id = workflow.id
            WHERE workflow.review_run_id = %s
            ORDER BY node.sequence_no
            """,
            (review_run_id,),
        ).fetchall()
        tokens = connection.execute(
            """
            SELECT COALESCE(SUM(call.input_tokens), 0)::INTEGER AS input_tokens,
                   COALESCE(SUM(call.output_tokens), 0)::INTEGER AS output_tokens
            FROM workflow_runs workflow
            JOIN workflow_node_runs node ON node.workflow_run_id = workflow.id
            JOIN llm_calls call ON call.node_run_id = node.id
            WHERE workflow.review_run_id = %s
            """,
            (review_run_id,),
        ).fetchone()
    review_duration_ms = None
    if detail.started_at and detail.completed_at:
        review_duration_ms = round(
            (detail.completed_at - detail.started_at).total_seconds() * 1000, 2
        )
    return {
        "review_duration_ms": review_duration_ms,
        "input_tokens": int(tokens["input_tokens"]),
        "output_tokens": int(tokens["output_tokens"]),
        "nodes": [
            {
                "node_name": row["node_name"],
                "status": row["status"],
                "latency_ms": row["latency_ms"],
            }
            for row in nodes
        ],
    }


def evaluate_e2e(
    dataset: EvaluationDataset,
    resources: dict[str, Any],
    review_run_id: UUID | None,
    start_review: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    """将真实审查结论和引用证据与压力集标准答案逐项比较。"""

    contract_id = resources["contract_id"]
    if contract_id is None:
        raise RuntimeError("未找到压力测试合同，请先使用 --prepare")
    if start_review:
        review_run_id = RiskReviewService().create_review(contract_id).review_run_id
    elif review_run_id is None:
        review_run_id = _latest_successful_review(contract_id)
    if review_run_id is None:
        raise RuntimeError("没有可评测的成功审查，请指定 --review-run-id 或 --start-review")

    detail = _wait_for_review(review_run_id, timeout_seconds)
    expected_by_type = dataset.expected["expected"]
    judgement_by_type = {
        item["check_code"]: item for item in dataset.judgements["queries"]
    }
    result_by_type = {item.check_code: item for item in detail.findings}
    evidence_documents = _load_evidence_document_ids(detail)
    checks: list[dict[str, Any]] = []
    for check_type in CHECK_TYPES:
        expected = expected_by_type[check_type]
        actual = result_by_type.get(check_type)
        contract_evidence = {
            evidence.clause_no
            for evidence in (actual.evidence if actual else [])
            if evidence.evidence_type == "CONTRACT"
            and evidence.clause_no
            and evidence_documents.get(evidence.chunk_id)
            == resources["contract_document_id"]
        }
        policy_evidence = {
            evidence.clause_no
            for evidence in (actual.evidence if actual else [])
            if evidence.evidence_type == "POLICY"
            and evidence.clause_no
            and evidence_documents.get(evidence.chunk_id) == resources["policy_document_id"]
        }
        out_of_dataset_evidence = [
            str(evidence.chunk_id)
            for evidence in (actual.evidence if actual else [])
            if evidence_documents.get(evidence.chunk_id)
            not in {resources["contract_document_id"], resources["policy_document_id"]}
        ]
        judgement = judgement_by_type[check_type]
        forbidden_contract = {
            item["clause_no"]
            for item in judgement.get("contract_judgements", [])
            if int(item["relevance"]) == 0
        }
        forbidden_policy = {
            item["section_no"]
            for item in judgement.get("policy_judgements", [])
            if int(item["relevance"]) == 0
        }
        required_contract = set(expected.get("contract_clause_nos", []))
        required_policy = set(expected.get("policy_section_nos", []))
        checks.append(
            {
                "check_type": check_type,
                "expected_status": expected["status"],
                "actual_status": actual.status if actual else "MISSING",
                "status_match": bool(actual and actual.status == expected["status"]),
                "contract_evidence": sorted(contract_evidence),
                "policy_evidence": sorted(policy_evidence),
                "contract_evidence_recall": (
                    len(contract_evidence & required_contract) / len(required_contract)
                    if required_contract
                    else 1.0
                ),
                "policy_evidence_recall": (
                    len(policy_evidence & required_policy) / len(required_policy)
                    if required_policy
                    else 1.0
                ),
                "forbidden_evidence_count": len(contract_evidence & forbidden_contract)
                + len(policy_evidence & forbidden_policy),
                "out_of_dataset_evidence_count": len(out_of_dataset_evidence),
            }
        )

    count = len(checks)
    return {
        "review_run_id": str(review_run_id),
        "review_status": detail.status,
        "observability": _load_review_observability(review_run_id, detail),
        "summary": {
            "status_accuracy": round(
                sum(item["status_match"] for item in checks) / count, 6
            ),
            "contract_evidence_recall": round(
                sum(item["contract_evidence_recall"] for item in checks) / count, 6
            ),
            "policy_evidence_recall": round(
                sum(item["policy_evidence_recall"] for item in checks) / count, 6
            ),
            "forbidden_evidence_count": sum(
                item["forbidden_evidence_count"] for item in checks
            ),
            "out_of_dataset_evidence_count": sum(
                item["out_of_dataset_evidence_count"] for item in checks
            ),
        },
        "checks": checks,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """生成适合提交到评测记录或直接阅读的 Markdown 摘要。"""

    lines = ["# RAG 自动评测报告", "", f"- 生成时间：{report['generated_at']}"]
    validation = report["validation"]
    lines.extend(
        [
            f"- 数据集：`{report['dataset']}`",
            f"- 离线校验：{'通过' if validation['passed'] else '失败'}",
            f"- 数据规模：{validation['contract_clause_count']} 条合同条款 / "
            f"{validation['policy_section_count']} 条制度条款",
            "",
        ]
    )
    if validation["errors"]:
        lines.extend(["## 校验错误", ""])
        lines.extend(f"- {error}" for error in validation["errors"])
        lines.append("")

    retrieval = report.get("retrieval")
    if retrieval:
        ranking_label = {
            "vector_baseline": "向量检索基线",
            "contract_rerank": "合同重排序检索",
            "policy_rerank": "制度重排序检索",
            "contract_policy_rerank": "合同与制度重排序检索",
        }.get(retrieval.get("ranking"), "检索评测")
        lines.extend(
            [
                f"## {ranking_label}",
                "",
                "| 数据源 | Recall@K | Precision@K | MRR | NDCG@K |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for source, label in (("contract", "合同"), ("policy", "制度")):
            metrics = retrieval["aggregate"][source]
            lines.append(
                f"| {label} | {metrics['recall_at_k']:.4f} | "
                f"{metrics['precision_at_k']:.4f} | {metrics['mrr']:.4f} | "
                f"{metrics['ndcg_at_k']:.4f} |"
            )
        lines.extend(
            [
                "",
                "| 检查项 | 合同 Recall | 合同 MRR | 合同 NDCG | 制度 Recall | 制度 MRR | 制度 NDCG |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in retrieval.get("checks", []):
            contract = item["contract"]["metrics"]
            policy = item["policy"]["metrics"]
            lines.append(
                f"| {item['check_type']} | {contract['recall_at_k']:.4f} | "
                f"{contract['mrr']:.4f} | {contract['ndcg_at_k']:.4f} | "
                f"{policy['recall_at_k']:.4f} | {policy['mrr']:.4f} | "
                f"{policy['ndcg_at_k']:.4f} |"
            )
        lines.extend(["", f"检索评测耗时：{retrieval['duration_ms']} ms", ""])

    e2e = report.get("e2e")
    if e2e:
        summary = e2e["summary"]
        lines.extend(
            [
                "## 端到端风险审查",
                "",
                f"- 审查任务：`{e2e['review_run_id']}`",
                f"- 状态准确率：{summary['status_accuracy']:.4f}",
                f"- 合同证据召回：{summary['contract_evidence_recall']:.4f}",
                f"- 制度证据召回：{summary['policy_evidence_recall']:.4f}",
                f"- 明确无关证据引用数：{summary['forbidden_evidence_count']}",
                f"- 压力集之外证据引用数：{summary['out_of_dataset_evidence_count']}",
                f"- 审查耗时：{e2e['observability']['review_duration_ms']} ms",
                f"- Token：输入 {e2e['observability']['input_tokens']} / "
                f"输出 {e2e['observability']['output_tokens']}",
                "",
                "| 检查项 | 期望 | 实际 | 状态匹配 | 合同证据 | 制度证据 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in e2e["checks"]:
            lines.append(
                f"| {item['check_type']} | {item['expected_status']} | "
                f"{item['actual_status']} | {'是' if item['status_match'] else '否'} | "
                f"{', '.join(item['contract_evidence']) or '-'} | "
                f"{', '.join(item['policy_evidence']) or '-'} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_reports(report: dict[str, Any], output_dir: Path, label: str) -> tuple[Path, Path]:
    """同时输出机器可读 JSON 和便于查看的 Markdown 报告。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(char for char in label if char.isalnum() or char in "-_")
    if not safe_label:
        raise ValueError("--label 至少需要包含一个字母、数字、横线或下划线")
    json_path = output_dir / f"{safe_label}.json"
    markdown_path = output_dir / f"{safe_label}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="评测压力集、RAG 检索和风险审查结果")
    parser.add_argument(
        "--mode",
        choices=("validate", "retrieval", "e2e", "all"),
        default="validate",
        help="validate 仅离线校验；retrieval 测检索；e2e 测审查；all 执行后两项",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "examples" / "evaluation" / "stress",
    )
    parser.add_argument("--prepare", action="store_true", help="缺失时导入测试数据并向量化")
    parser.add_argument("--start-review", action="store_true", help="发起一次新的异步风险审查")
    parser.add_argument("--review-run-id", type=UUID, help="评测指定的既有审查任务")
    parser.add_argument(
        "--confirm-external-calls",
        action="store_true",
        help="确认允许测试数据调用百炼 Embedding/大模型 API",
    )
    parser.add_argument("--contract-top-k", type=int, default=20)
    parser.add_argument(
        "--contract-rerank",
        action="store_true",
        help="对合同候选执行与正式审查相同的重排序和查询级门槛",
    )
    parser.add_argument(
        "--contract-final-top-k", type=int, default=settings.contract_final_top_k
    )
    parser.add_argument(
        "--contract-query-min-score",
        type=float,
        default=settings.contract_rerank_query_min_score,
    )
    parser.add_argument("--policy-top-k", type=int, default=30)
    parser.add_argument(
        "--policy-rerank",
        action="store_true",
        help="对制度候选调用项目当前配置的重排序模型和候选阈值",
    )
    parser.add_argument("--policy-final-top-k", type=int, default=5)
    parser.add_argument(
        "--policy-min-score", type=float, default=settings.policy_rerank_min_score
    )
    parser.add_argument("--timeout", type=int, default=600, help="异步任务等待秒数")
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "output" / "evaluation"
    )
    parser.add_argument("--label", default="vector-baseline", help="输出文件名标签")
    parser.add_argument(
        "--fail-on-mismatch", action="store_true", help="有校验错误或审查结论不一致时返回 1"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回值可直接供 CI 判断评测是否通过。"""

    args = build_parser().parse_args(argv)
    if (
        args.contract_top_k <= 0
        or args.contract_final_top_k <= 0
        or args.policy_top_k <= 0
        or args.policy_final_top_k <= 0
    ):
        raise ValueError("top-k 必须大于 0")
    if args.policy_final_top_k > args.policy_top_k:
        raise ValueError("--policy-final-top-k 不能大于 --policy-top-k")
    if args.contract_final_top_k > args.contract_top_k:
        raise ValueError("--contract-final-top-k 不能大于 --contract-top-k")
    needs_external_calls = args.prepare or args.mode in {"retrieval", "all"} or args.start_review
    if args.start_review and args.mode not in {"e2e", "all"}:
        raise ValueError("--start-review 只能和 --mode e2e 或 all 一起使用")
    if args.review_run_id and args.mode not in {"e2e", "all"}:
        raise ValueError("--review-run-id 只能和 --mode e2e 或 all 一起使用")
    if args.start_review and args.review_run_id:
        raise ValueError("--start-review 和 --review-run-id 不能同时使用")
    if needs_external_calls and not args.confirm_external_calls:
        raise ValueError(
            "本次操作会调用外部模型 API；确认测试数据可发送后，请添加 "
            "--confirm-external-calls"
        )
    if needs_external_calls and not settings.api_key:
        raise ValueError("未配置 api-key 环境变量，无法调用百炼模型 API")

    dataset = load_dataset(args.dataset)
    validation = validate_dataset(dataset)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset.root),
        "validation": validation,
    }
    resources: dict[str, Any] | None = None
    if validation["passed"] and (args.prepare or args.mode != "validate"):
        resources = (
            prepare_dataset(dataset, args.timeout)
            if args.prepare
            else _find_resources(dataset)
        )
    if validation["passed"] and args.mode in {"retrieval", "all"}:
        assert resources is not None
        report["retrieval"] = evaluate_retrieval(
            dataset,
            resources,
            args.contract_top_k,
            args.policy_top_k,
            args.contract_rerank,
            args.contract_final_top_k,
            args.contract_query_min_score,
            args.policy_rerank,
            args.policy_final_top_k,
            args.policy_min_score,
        )
    if validation["passed"] and args.mode in {"e2e", "all"}:
        assert resources is not None
        report["e2e"] = evaluate_e2e(
            dataset,
            resources,
            args.review_run_id,
            args.start_review,
            args.timeout,
        )

    json_path, markdown_path = write_reports(report, args.output_dir, args.label)
    print(f"评测完成：{json_path}")
    print(f"摘要报告：{markdown_path}")
    if not args.fail_on_mismatch:
        return 0
    if not validation["passed"]:
        return 1
    e2e = report.get("e2e")
    if e2e and e2e["summary"]["status_accuracy"] < 1.0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"评测失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
