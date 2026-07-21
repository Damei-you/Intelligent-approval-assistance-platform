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
            with connection.transaction():
                document_ids = connection.execute(
                    """
                    SELECT d.id
                    FROM documents d
                    JOIN contracts c ON c.id = d.contract_id
                    WHERE c.contract_no = %s
                    """,
                    (self.contract_no,),
                ).fetchall()
                stored_files = connection.execute(
                    """
                    SELECT d.storage_uri
                    FROM documents d
                    JOIN contracts c ON c.id = d.contract_id
                    WHERE c.contract_no = %s AND d.storage_uri IS NOT NULL
                    """,
                    (self.contract_no,),
                ).fetchall()
                for row in document_ids:
                    connection.execute(
                        "DELETE FROM async_jobs WHERE resource_id = %s",
                        (row["id"],),
                    )
                connection.execute(
                    "DELETE FROM contracts WHERE contract_no = %s",
                    (self.contract_no,),
                )
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
