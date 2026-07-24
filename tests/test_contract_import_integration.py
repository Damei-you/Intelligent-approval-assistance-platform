from __future__ import annotations

import asyncio
import json
import os
import unittest
from uuid import uuid4

from app.core.config import PROJECT_ROOT, settings
from app.core.database import open_connection
from app.main import app
from app.modules.contract_import.repository import ContractImportRepository
from app.modules.contract_import.schemas import (
    ContractFileMetadata,
    ContractJsonImportRequest,
)
from app.modules.contract_import.service import ContractImportService
from app.modules.vectorization.service import VectorizationRepository, VectorizationService


class FakeEmbeddingProvider:
    """测试替身：不访问外部 API，稳定返回 1536 维向量。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.001] * 1536 for _ in texts]


@unittest.skipUnless(
    os.getenv("RUN_INTEGRATION_TESTS") == "1",
    "Set RUN_INTEGRATION_TESTS=1 with PostgreSQL running to execute integration tests.",
)
class ContractImportIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract_no = f"IT-{uuid4().hex[:12]}"
        self.original_api_key = settings.api_key
        # 集成测试不应意外调用真实模型；向量写入由 FakeEmbeddingProvider 单独验证。
        object.__setattr__(settings, "api_key", "")

    def tearDown(self) -> None:
        object.__setattr__(
            settings,
            "api_key",
            self.original_api_key,
        )
        with open_connection() as connection:
            stored_files = connection.execute(
                """
                SELECT d.storage_uri
                FROM documents d
                JOIN contracts c ON c.id = d.contract_id
                WHERE c.contract_no = %s AND d.storage_uri IS NOT NULL
                """,
                (self.contract_no,),
            ).fetchall()
        ContractImportRepository().delete_contract_data(self.contract_no)
        for row in stored_files:
            candidate = (PROJECT_ROOT / row["storage_uri"]).resolve()
            if candidate.is_relative_to(settings.upload_dir.resolve()):
                candidate.unlink(missing_ok=True)

    def test_json_import_persists_clauses_without_embeddings(self) -> None:
        service = ContractImportService(ContractImportRepository())
        request = ContractJsonImportRequest(
            contract_no=self.contract_no,
            name="集成测试采购合同",
            contract_type_code="PURCHASE",
            clauses=[
                {
                    "clause_no": "第一条",
                    "title": "付款方式",
                    "content": "验收后付款。",
                },
                {
                    "clause_no": "第二条",
                    "title": "交付方式",
                    "content": "供应方负责交付。",
                },
            ],
        )

        result = service.import_json(request)
        detail = service.get_import_detail(str(result.document_id))

        self.assertEqual(2, result.clause_count)
        self.assertFalse(result.vectorized)
        self.assertEqual(2, detail.clause_count)
        self.assertEqual(0, detail.vectorized_clause_count)

    def test_vectorization_writes_1536_dimension_embeddings(self) -> None:
        service = ContractImportService(ContractImportRepository())
        result = service.import_json(
            ContractJsonImportRequest(
                contract_no=self.contract_no,
                name="向量化集成测试合同",
                contract_type_code="PURCHASE",
                clauses=[
                    {"clause_no": "第一条", "content": "验收后付款。"},
                    {"clause_no": "第二条", "content": "供应方负责交付。"},
                ],
            )
        )
        repository = VectorizationRepository()
        job = repository.create_job(result.document_id)

        count = VectorizationService(
            repository=repository,
            provider=FakeEmbeddingProvider(),
        ).vectorize_document(job["job_id"], result.document_id)
        status = repository.get_document_status(result.document_id)

        self.assertEqual(2, count)
        self.assertEqual("SUCCEEDED", status.status)
        self.assertEqual(2, status.vectorized_clause_count)
        self.assertEqual("text-embedding-v4", status.model_name)
        self.assertEqual(1536, status.dimension)

    def test_json_http_endpoint_and_detail_endpoint(self) -> None:
        payload = {
            "contract_no": self.contract_no,
            "name": "HTTP 集成测试销售合同",
            "contract_type_code": "SALES",
            "clauses": [
                {
                    "clause_no": "第一条",
                    "title": "交付方式",
                    "content": "销售方按期交付。",
                }
            ],
        }

        status_code, response = asyncio.run(
            _asgi_request(
                "POST",
                "/api/v1/contracts/imports/json",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                [(b"content-type", b"application/json")],
            )
        )
        self.assertEqual(201, status_code)
        self.assertFalse(response["vectorized"])

        status_code, detail = asyncio.run(
            _asgi_request(
                "GET",
                f"/api/v1/contracts/imports/{response['document_id']}",
            )
        )
        self.assertEqual(200, status_code)
        self.assertEqual(1, detail["clause_count"])
        self.assertEqual(0, detail["vectorized_clause_count"])

        status_code, vectorization = asyncio.run(
            _asgi_request(
                "GET",
                f"/api/v1/contracts/imports/{response['document_id']}/vectorization",
            )
        )
        self.assertEqual(200, status_code)
        self.assertEqual("NOT_CONFIGURED", vectorization["status"])

    def test_file_preview_does_not_persist_until_confirmation(self) -> None:
        service = ContractImportService(ContractImportRepository())
        source = (
            "第一条 合同标的\n供应方提供办公设备。\n\n"
            "第二条 付款方式\n验收合格后付款。"
        ).encode("utf-8")
        metadata = ContractFileMetadata(
            contract_no=self.contract_no,
            name="预览确认测试合同",
            contract_type_code="PURCHASE",
        )

        preview = service.preview_file(
            file_name="preview-contract.txt",
            mime_type="text/plain",
            data=source,
            file_metadata=metadata,
        )

        with open_connection() as connection:
            persisted_count = connection.execute(
                "SELECT COUNT(*) AS count FROM contracts WHERE contract_no = %s",
                (self.contract_no,),
            ).fetchone()["count"]
        self.assertEqual(0, persisted_count)
        self.assertFalse(preview.persisted)
        self.assertEqual(2, preview.clause_count)

        preview.payload.clauses[1].content = "验收合格后十五个工作日内付款。"
        result = service.confirm_file(
            file_name="preview-contract.txt",
            mime_type="text/plain",
            data=source,
            preview_hash=preview.preview_hash,
            payload=preview.payload,
        )
        detail = service.get_import_detail(str(result.document_id))

        self.assertEqual(2, detail.clause_count)
        self.assertEqual(0, detail.vectorized_clause_count)

    def test_delete_contract_data_cascades_all_business_records(self) -> None:
        """清理测试自身创建的合同，并验证无外键记录或异步任务残留。"""

        repository = ContractImportRepository()
        service = ContractImportService(repository)
        result = service.import_json(
            ContractJsonImportRequest(
                contract_no=self.contract_no,
                name="待清理的集成测试合同",
                contract_type_code="PURCHASE",
                clauses=[
                    {"clause_no": "第一条", "content": "验收后付款。"},
                    {"clause_no": "第二条", "content": "供应方负责交付。"},
                ],
            )
        )
        document_job = VectorizationRepository().create_job(result.document_id)
        review_run_id = uuid4()
        finding_id = uuid4()
        approval_id = uuid4()
        chat_session_id = uuid4()
        chat_message_id = uuid4()
        workflow_run_id = uuid4()
        node_run_id = uuid4()
        retrieval_run_id = uuid4()
        review_job_id = uuid4()

        with open_connection() as connection:
            with connection.transaction():
                chunk = connection.execute(
                    """
                    SELECT id
                    FROM document_chunks
                    WHERE document_id = %s
                    ORDER BY chunk_index
                    LIMIT 1
                    """,
                    (result.document_id,),
                ).fetchone()
                check_item = connection.execute(
                    """
                    SELECT id
                    FROM review_check_items
                    WHERE enabled = TRUE
                    ORDER BY sort_order
                    LIMIT 1
                    """
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO review_runs (
                        id, contract_id, contract_document_id, status,
                        overall_risk_level, summary, approval_suggestion
                    )
                    VALUES (%s, %s, %s, 'SUCCEEDED', 'LOW', '测试摘要', 'APPROVE')
                    """,
                    (review_run_id, result.contract_id, result.document_id),
                )
                connection.execute(
                    """
                    INSERT INTO risk_findings (
                        id, review_run_id, check_item_id, status, severity,
                        title, description
                    )
                    VALUES (%s, %s, %s, 'PASS', 'LOW', '测试结论', '测试说明')
                    """,
                    (finding_id, review_run_id, check_item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO finding_evidence (
                        finding_id, chunk_id, evidence_type, cited_text
                    )
                    VALUES (%s, %s, 'CONTRACT', '验收后付款。')
                    """,
                    (finding_id, chunk["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO approval_instances (
                        id, contract_id, review_run_id, status
                    )
                    VALUES (%s, %s, %s, 'IN_PROGRESS')
                    """,
                    (approval_id, result.contract_id, review_run_id),
                )
                connection.execute(
                    """
                    INSERT INTO approval_steps (
                        approval_instance_id, step_no, step_type, step_name, status
                    )
                    VALUES (%s, 1, 'BUSINESS', '业务审批', 'IN_PROGRESS')
                    """,
                    (approval_id,),
                )
                connection.execute(
                    """
                    INSERT INTO chat_sessions (
                        id, contract_id, review_run_id, finding_id,
                        contract_document_id, title
                    )
                    VALUES (%s, %s, %s, %s, %s, '测试问答')
                    """,
                    (
                        chat_session_id,
                        result.contract_id,
                        review_run_id,
                        finding_id,
                        result.document_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO chat_messages (
                        id, session_id, role, content, status
                    )
                    VALUES (%s, %s, 'ASSISTANT', '测试回答', 'SUCCEEDED')
                    """,
                    (chat_message_id, chat_session_id),
                )
                connection.execute(
                    """
                    INSERT INTO chat_message_citations (
                        message_id, chunk_id, cited_text
                    )
                    VALUES (%s, %s, '验收后付款。')
                    """,
                    (chat_message_id, chunk["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_runs (
                        id, run_type, review_run_id, graph_name, status
                    )
                    VALUES (%s, 'RISK_REVIEW', %s, 'cleanup_test', 'SUCCEEDED')
                    """,
                    (workflow_run_id, review_run_id),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_node_runs (
                        id, workflow_run_id, node_name, sequence_no, status
                    )
                    VALUES (%s, %s, 'cleanup_node', 0, 'SUCCEEDED')
                    """,
                    (node_run_id, workflow_run_id),
                )
                connection.execute(
                    """
                    INSERT INTO retrieval_runs (
                        id, node_run_id, query_text, top_k
                    )
                    VALUES (%s, %s, '测试查询', 1)
                    """,
                    (retrieval_run_id, node_run_id),
                )
                connection.execute(
                    """
                    INSERT INTO retrieval_hits (
                        retrieval_run_id, chunk_id, rank_no, similarity_score
                    )
                    VALUES (%s, %s, 1, 0.9)
                    """,
                    (retrieval_run_id, chunk["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO async_jobs (
                        id, celery_task_id, task_type, resource_type,
                        resource_id, status
                    )
                    VALUES (%s, %s, 'RISK_REVIEW', 'REVIEW_RUN', %s, 'SUCCEEDED')
                    """,
                    (review_job_id, str(uuid4()), review_run_id),
                )

        cleanup = repository.delete_contract_data(self.contract_no)

        self.assertTrue(cleanup["deleted"])
        self.assertEqual(1, cleanup["deleted_documents"])
        self.assertEqual(2, cleanup["deleted_clauses"])
        self.assertEqual(1, cleanup["deleted_reviews"])
        self.assertEqual(1, cleanup["deleted_approvals"])
        self.assertEqual(1, cleanup["deleted_chat_sessions"])
        self.assertEqual(2, cleanup["deleted_async_jobs"])
        with open_connection() as connection:
            contract_count = connection.execute(
                "SELECT COUNT(*) AS count FROM contracts WHERE contract_no = %s",
                (self.contract_no,),
            ).fetchone()["count"]
            remaining_jobs = connection.execute(
                "SELECT COUNT(*) AS count FROM async_jobs WHERE id IN (%s, %s)",
                (document_job["job_id"], review_job_id),
            ).fetchone()["count"]
        self.assertEqual(0, contract_count)
        self.assertEqual(0, remaining_jobs)


async def _asgi_request(
    method: str,
    path: str,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict]:
    messages: list[dict] = []
    request_delivered = False

    async def receive() -> dict:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

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
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "extensions": {},
    }
    await app(scope, receive, send)

    response_start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return response_start["status"], json.loads(response_body)
