from __future__ import annotations

import os
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import PROJECT_ROOT, settings
from app.core.database import open_connection
from app.main import app
from app.modules.policy_import.repository import PolicyImportRepository
from app.modules.policy_import.schemas import PolicyFileMetadata, PolicyJsonImportRequest
from app.modules.policy_import.service import PolicyImportService
from app.modules.vectorization.service import VectorizationRepository, VectorizationService


class FakePolicyEmbeddingProvider:
    """测试替身：生成固定 1536 维向量，不访问外部模型。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.002] * 1536 for _ in texts]


@unittest.skipUnless(
    os.getenv("RUN_INTEGRATION_TESTS") == "1",
    "Set RUN_INTEGRATION_TESTS=1 with PostgreSQL running to execute integration tests.",
)
class PolicyImportIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy_no = f"POLICY-IT-{uuid4().hex[:10]}"
        self.original_api_key = settings.api_key
        object.__setattr__(settings, "api_key", "")

    def tearDown(self) -> None:
        object.__setattr__(settings, "api_key", self.original_api_key)
        with open_connection() as connection:
            with connection.transaction():
                documents = connection.execute(
                    """
                    SELECT id, storage_uri
                    FROM documents
                    WHERE document_type = 'POLICY'
                      AND metadata ->> 'policy_no' = %s
                    """,
                    (self.policy_no,),
                ).fetchall()
                for document in documents:
                    connection.execute(
                        "DELETE FROM async_jobs WHERE resource_id = %s",
                        (document["id"],),
                    )
                connection.execute(
                    """
                    DELETE FROM documents
                    WHERE document_type = 'POLICY'
                      AND metadata ->> 'policy_no' = %s
                    """,
                    (self.policy_no,),
                )
        for document in documents:
            if not document["storage_uri"]:
                continue
            candidate = (PROJECT_ROOT / document["storage_uri"]).resolve()
            if candidate.is_relative_to(settings.upload_dir.resolve()):
                candidate.unlink(missing_ok=True)

    def test_policy_preview_confirm_and_revision(self) -> None:
        service = PolicyImportService(PolicyImportRepository())
        source = (
            "第一条 预付款控制\n预付款不得超过合同总价的百分之三十。\n\n"
            "第二条 质量保证\n办公设备质保期不得少于十二个月。"
        ).encode("utf-8")
        preview = service.preview_file(
            file_name="policy.txt",
            mime_type="text/plain",
            data=source,
            file_metadata=PolicyFileMetadata(
                policy_no=self.policy_no,
                title="采购合同管理制度",
                version="V1.0",
                issuer="采购管理部",
            ),
        )

        with open_connection() as connection:
            before_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM documents
                WHERE document_type = 'POLICY'
                  AND metadata ->> 'policy_no' = %s
                """,
                (self.policy_no,),
            ).fetchone()["count"]
        self.assertEqual(0, before_count)
        self.assertEqual(2, preview.section_count)

        first = service.confirm_file(
            file_name="policy.txt",
            mime_type="text/plain",
            data=source,
            preview_hash=preview.preview_hash,
            payload=preview.payload,
        )
        second_payload = preview.payload.model_copy(update={"version": "V1.1"})
        second = service.import_json(second_payload)

        self.assertEqual(1, first.revision_no)
        self.assertEqual(2, second.revision_no)
        detail = service.get_import_detail(str(second.document_id))
        self.assertTrue(detail.is_current)
        self.assertEqual("V1.1", detail.version)
        self.assertEqual(2, detail.section_count)

    def test_policy_sections_can_be_vectorized(self) -> None:
        service = PolicyImportService(PolicyImportRepository())
        result = service.import_json(
            PolicyJsonImportRequest(
                policy_no=self.policy_no,
                title="制度向量化测试",
                sections=[
                    {
                        "section_no": "第一条",
                        "title": "付款控制",
                        "content": "预付款比例不得超过百分之三十。",
                    }
                ],
            )
        )
        repository = VectorizationRepository()
        job = repository.create_job(result.document_id)

        VectorizationService(
            repository=repository,
            provider=FakePolicyEmbeddingProvider(),
        ).vectorize_document(job["job_id"], result.document_id)
        detail = service.get_import_detail(str(result.document_id))

        self.assertEqual(1, detail.vectorized_section_count)
        with open_connection() as connection:
            chunk = connection.execute(
                """
                SELECT chunk_type, embedding_model
                FROM document_chunks
                WHERE document_id = %s
                """,
                (result.document_id,),
            ).fetchone()
        self.assertEqual("POLICY_SECTION", chunk["chunk_type"])
        self.assertEqual("text-embedding-v4", chunk["embedding_model"])

    def test_policy_http_endpoints(self) -> None:
        payload = {
            "policy_no": self.policy_no,
            "title": "HTTP 制度导入测试",
            "version": "V1.0",
            "sections": [
                {
                    "section_no": "第一条",
                    "title": "适用范围",
                    "content": "本制度适用于采购合同。",
                }
            ],
        }
        with TestClient(app) as client:
            response = client.post("/api/v1/policies/imports/json", json=payload)
            self.assertEqual(201, response.status_code)
            imported = response.json()
            self.assertEqual(self.policy_no, imported["policy_no"])

            detail_response = client.get(
                f"/api/v1/policies/imports/{imported['document_id']}"
            )
            self.assertEqual(200, detail_response.status_code)
            self.assertEqual(1, detail_response.json()["section_count"])

            vector_response = client.get(
                f"/api/v1/policies/imports/{imported['document_id']}/vectorization"
            )
            self.assertEqual(200, vector_response.status_code)
            self.assertEqual("NOT_CONFIGURED", vector_response.json()["status"])
