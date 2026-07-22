from __future__ import annotations

import os
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

import app.modules.contract_chat.router as chat_router_module
from app.core.config import settings
from app.core.database import open_connection
from app.main import app
from app.modules.contract_chat.schemas import ModelChatAnswer, ModelClauseDraft
from app.modules.contract_import.repository import ContractImportRepository
from app.modules.contract_import.schemas import ContractJsonImportRequest
from app.modules.contract_import.service import ContractImportService
from app.modules.risk_review.service import PolicyRerankResult


class FakeChatModelProvider:
    """集成测试只验证数据库与 HTTP 闭环，不调用外部生成式模型。"""

    def answer(self, intent, question, context, contract_chunks, policy_chunks):
        if intent == "DRAFT_CLAUSE":
            answer = ModelChatAnswer(
                answer="已生成待业务和法务确认的付款条款草案。",
                contract_refs=["C1"],
                policy_refs=[],
                draft=ModelClauseDraft(
                    target_clause_ref="C1",
                    proposed_text="合同签订后支付百分之二十预付款，验收合格后支付余款。",
                    change_summary="降低预付款比例并增加验收节点。",
                    rationale="降低付款风险。",
                ),
            )
        elif intent == "EVIDENCE_QUERY":
            answer = ModelChatAnswer(
                answer="相关付款合同条款已列出。",
                contract_refs=["C1"],
                policy_refs=[],
            )
        else:
            answer = ModelChatAnswer(
                answer="该风险源于合同付款约定与审查要求存在偏差。",
                # 只引用第二条候选，验证历史查询不会把 C2 重新编号为 C1。
                contract_refs=["C2"],
                policy_refs=[],
            )
        return (
            answer,
            {"input_tokens": 12, "output_tokens": 6},
            8,
        )


class FakeChatEmbeddingProvider:
    """返回与测试分块相同维度的固定向量，避免调用外部 Embedding。"""

    def embed_documents(self, texts):
        return [[0.01] * settings.embedding_dimension for _ in texts]


class FakeChatRerankProvider:
    """使用原向量顺序模拟制度重排，保证集成测试不访问供应商接口。"""

    def rerank(self, query, hits, final_top_k):
        ranked = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": index,
                "rerank_score": 1 - index / 100,
                "selected_for_context": index <= final_top_k,
            }
            for index, hit in enumerate(hits, 1)
        ]
        return PolicyRerankResult(
            selected_hits=ranked[:final_top_k],
            all_hits=ranked,
            latency_ms=1,
        )


@unittest.skipUnless(
    os.getenv("RUN_INTEGRATION_TESTS") == "1",
    "Set RUN_INTEGRATION_TESTS=1 with PostgreSQL running to execute integration tests.",
)
class ContractChatIntegrationTests(unittest.TestCase):
    """使用真实 PostgreSQL 验证会话恢复、消息幂等和引用落库。"""

    def setUp(self) -> None:
        self.contract_no = f"CHAT-IT-{uuid4().hex[:10]}"
        self.contract_id = None
        self.document_id = None
        self.review_run_id = None
        self.original_api_key = settings.api_key
        self.original_model_provider = chat_router_module.service.model_provider
        self.original_embedding_provider = chat_router_module.service.embedding_provider
        self.original_rerank_provider = chat_router_module.service.rerank_provider
        self.addCleanup(self._restore_chat_dependencies)
        object.__setattr__(settings, "api_key", "")
        chat_router_module.service.model_provider = FakeChatModelProvider()
        chat_router_module.service.embedding_provider = FakeChatEmbeddingProvider()
        chat_router_module.service.rerank_provider = FakeChatRerankProvider()

        imported = ContractImportService(ContractImportRepository()).import_json(
            ContractJsonImportRequest(
                contract_no=self.contract_no,
                name="风险对话集成测试合同",
                contract_type_code="PURCHASE",
                clauses=[
                    {
                        "clause_no": "第十条",
                        "title": "付款安排",
                        "content": "合同签订后支付百分之五十预付款。",
                    },
                    {
                        "clause_no": "第十一条",
                        "title": "验收付款",
                        "content": "验收合格后支付剩余百分之五十价款。",
                    }
                ],
            )
        )
        self.contract_id = imported.contract_id
        self.document_id = imported.document_id
        self.addCleanup(self._cleanup_test_data)
        self.review_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO review_runs (
                        id, contract_id, contract_document_id, status,
                        overall_risk_level, summary, approval_suggestion,
                        started_at, completed_at
                    ) VALUES (
                        %s, %s, %s, 'SUCCEEDED', 'HIGH',
                        '发现付款条款风险。', 'APPROVE_AFTER_REVISION',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """,
                    (self.review_run_id, self.contract_id, self.document_id),
                )
                check_item = connection.execute(
                    "SELECT id FROM review_check_items WHERE code = 'PAYMENT_TERMS'"
                ).fetchone()
                finding = connection.execute(
                    """
                    INSERT INTO risk_findings (
                        review_run_id, check_item_id, status, severity,
                        title, description, suggestion
                    ) VALUES (%s, %s, 'RISK', 'HIGH', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        self.review_run_id,
                        check_item["id"],
                        "预付款比例偏高",
                        "合同约定的预付款比例偏高。",
                        "建议降低预付款并增加验收付款节点。",
                    ),
                ).fetchone()
                self.finding_id = finding["id"]
                chunks = connection.execute(
                    """
                    SELECT id, content FROM document_chunks
                    WHERE document_id = %s ORDER BY chunk_index
                    """,
                    (self.document_id,),
                ).fetchall()
                # 真实 Repository 只有在分块存在向量时才会执行合同 RAG；
                # 第一条与查询向量同向、第二条反向，使 C1 排名确定且不调用外部模型。
                for index, chunk in enumerate(chunks):
                    value = "0.01" if index == 0 else "-0.01"
                    vector_literal = "[" + ",".join(
                        [value] * settings.embedding_dimension
                    ) + "]"
                    connection.execute(
                        "UPDATE document_chunks SET embedding = %s::vector WHERE id = %s",
                        (vector_literal, chunk["id"]),
                    )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO finding_evidence (
                            finding_id, chunk_id, evidence_type,
                            relevance_score, cited_text, sort_order
                        ) VALUES (%s, %s, 'CONTRACT', 0.91, %s, %s)
                        """,
                        [
                            (self.finding_id, chunk["id"], chunk["content"], index)
                            for index, chunk in enumerate(chunks, 1)
                        ],
                    )

    def _restore_chat_dependencies(self) -> None:
        """即使 setUp 中途失败，也恢复被替换的全局服务依赖。"""

        chat_router_module.service.model_provider = self.original_model_provider
        chat_router_module.service.embedding_provider = self.original_embedding_provider
        chat_router_module.service.rerank_provider = self.original_rerank_provider
        object.__setattr__(settings, "api_key", self.original_api_key)

    def _cleanup_test_data(self) -> None:
        """只清理本测试创建的合同及其级联记录，避免影响演示数据。"""

        if self.contract_id is None:
            return
        with open_connection() as connection:
            with connection.transaction():
                if self.review_run_id is not None:
                    connection.execute(
                        "DELETE FROM review_runs WHERE id = %s", (self.review_run_id,)
                    )
                if self.document_id is not None:
                    connection.execute(
                        "DELETE FROM async_jobs WHERE resource_id = %s", (self.document_id,)
                    )
                connection.execute(
                    "DELETE FROM contracts WHERE id = %s", (self.contract_id,)
                )

    def test_http_chat_round_trip_is_versioned_cited_and_idempotent(self) -> None:
        """同一风险入口恢复会话，同一客户端请求不能重复调用模型或写消息。"""

        client_request_id = str(uuid4())
        with TestClient(app) as client:
            first_session = client.post(
                f"/api/v1/risk-findings/{self.finding_id}/chat-sessions"
            )
            self.assertEqual(201, first_session.status_code, first_session.text)
            first_payload = first_session.json()
            session_id = first_payload["session_id"]
            self.assertEqual(str(self.document_id), first_payload["contract_document_id"])

            restored_session = client.post(
                f"/api/v1/risk-findings/{self.finding_id}/chat-sessions"
            )
            self.assertEqual(session_id, restored_session.json()["session_id"])

            request_payload = {
                "content": "为什么这一项是高风险？",
                "intent": "EXPLAIN",
                "client_request_id": client_request_id,
            }
            first_turn = client.post(
                f"/api/v1/chat-sessions/{session_id}/messages",
                json=request_payload,
            )
            self.assertEqual(201, first_turn.status_code, first_turn.text)
            first_turn_payload = first_turn.json()
            assistant = first_turn_payload["assistant_message"]
            self.assertEqual("SUCCEEDED", assistant["status"])
            self.assertEqual("EXPLAIN", assistant["intent"])
            self.assertEqual("C2", assistant["citations"][0]["citation_label"])
            self.assertEqual(
                "验收合格后支付剩余百分之五十价款。",
                assistant["citations"][0]["cited_text"],
            )

            repeated_turn = client.post(
                f"/api/v1/chat-sessions/{session_id}/messages",
                json=request_payload,
            )
            self.assertEqual(201, repeated_turn.status_code, repeated_turn.text)
            self.assertEqual(
                first_turn_payload["user_message"]["id"],
                repeated_turn.json()["user_message"]["id"],
            )
            conflicting_turn = client.post(
                f"/api/v1/chat-sessions/{session_id}/messages",
                json={**request_payload, "content": "复用 UUID 的另一个问题。"},
            )
            self.assertEqual(409, conflicting_turn.status_code, conflicting_turn.text)
            self.assertEqual(
                "CHAT_IDEMPOTENCY_CONFLICT", conflicting_turn.json()["code"]
            )
            history = client.get(f"/api/v1/chat-sessions/{session_id}")
            self.assertEqual(200, history.status_code, history.text)
            self.assertEqual(2, len(history.json()["messages"]))
            self.assertEqual(
                ["USER", "ASSISTANT"],
                [message["role"] for message in history.json()["messages"]],
            )

        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT workflow.status,
                       (SELECT COUNT(*) FROM chat_message_citations citation
                        WHERE citation.message_id = workflow.chat_message_id) AS citation_count
                FROM workflow_runs workflow
                WHERE workflow.run_type = 'CONTRACT_CHAT'
                  AND workflow.chat_message_id = %s
                """,
                (first_turn_payload["assistant_message"]["id"],),
            ).fetchone()
        self.assertEqual("SUCCEEDED", row["status"])
        self.assertEqual(1, row["citation_count"])

    def test_stale_pending_turn_is_failed_and_session_can_continue(self) -> None:
        """进程中断留下的超时占位应在查询会话时回收，不能永久锁死。"""

        with TestClient(app) as client:
            session_payload = client.post(
                f"/api/v1/risk-findings/{self.finding_id}/chat-sessions"
            ).json()
            session_id = session_payload["session_id"]

            user_message_id = uuid4()
            assistant_message_id = uuid4()
            workflow_run_id = uuid4()
            with open_connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO chat_messages (
                            id, session_id, role, content, intent,
                            client_request_id, status, created_at
                        ) VALUES (
                            %s, %s, 'USER', '超时前的问题。', 'EXPLAIN',
                            %s, 'SUCCEEDED', CURRENT_TIMESTAMP - INTERVAL '11 minutes'
                        )
                        """,
                        (user_message_id, session_id, uuid4()),
                    )
                    connection.execute(
                        """
                        INSERT INTO chat_messages (
                            id, session_id, role, content, intent,
                            reply_to_message_id, status, created_at
                        ) VALUES (
                            %s, %s, 'ASSISTANT', '正在生成。', 'EXPLAIN',
                            %s, 'PENDING', CURRENT_TIMESTAMP - INTERVAL '11 minutes'
                        )
                        """,
                        (assistant_message_id, session_id, user_message_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO workflow_runs (
                            id, run_type, chat_message_id, graph_name,
                            graph_version, status, started_at
                        ) VALUES (
                            %s, 'CONTRACT_CHAT', %s, 'contract_risk_chat',
                            '1.0', 'RUNNING', CURRENT_TIMESTAMP - INTERVAL '11 minutes'
                        )
                        """,
                        (workflow_run_id, assistant_message_id),
                    )

            recovered = client.get(f"/api/v1/chat-sessions/{session_id}")
            self.assertEqual(200, recovered.status_code, recovered.text)
            assistant = next(
                message
                for message in recovered.json()["messages"]
                if message["id"] == str(assistant_message_id)
            )
            self.assertEqual("FAILED", assistant["status"])
            self.assertIn("生成超时", assistant["content"])

            continued = client.post(
                f"/api/v1/chat-sessions/{session_id}/messages",
                json={
                    "content": "超时后重新解释这项风险。",
                    "intent": "EXPLAIN",
                    "client_request_id": str(uuid4()),
                },
            )
            self.assertEqual(201, continued.status_code, continued.text)
            self.assertEqual(
                "SUCCEEDED", continued.json()["assistant_message"]["status"]
            )

        with open_connection() as connection:
            workflow = connection.execute(
                "SELECT status FROM workflow_runs WHERE id = %s",
                (workflow_run_id,),
            ).fetchone()
        self.assertEqual("FAILED", workflow["status"])

    def test_evidence_query_and_draft_persist_real_pgvector_results(self) -> None:
        """依据查询与草案应经过真实向量 SQL，并把后端原文写入结构化结果。"""

        with open_connection() as connection:
            original_chunks = connection.execute(
                """
                SELECT id, content FROM document_chunks
                WHERE document_id = %s ORDER BY chunk_index
                """,
                (self.document_id,),
            ).fetchall()

        with TestClient(app) as client:
            session = client.post(
                f"/api/v1/risk-findings/{self.finding_id}/chat-sessions"
            ).json()
            session_id = session["session_id"]
            evidence_turn = client.post(
                f"/api/v1/chat-sessions/{session_id}/messages",
                json={
                    "content": "查询付款合同条款与制度依据。",
                    "intent": "EVIDENCE_QUERY",
                    "client_request_id": str(uuid4()),
                },
            )
            self.assertEqual(201, evidence_turn.status_code, evidence_turn.text)
            self.assertEqual(
                "CONTRACT",
                evidence_turn.json()["assistant_message"]["citations"][0]["evidence_type"],
            )

            draft_turn = client.post(
                f"/api/v1/chat-sessions/{session_id}/messages",
                json={
                    "content": "将预付款比例调整为百分之二十并生成草案。",
                    "intent": "DRAFT_CLAUSE",
                    "client_request_id": str(uuid4()),
                },
            )
            self.assertEqual(201, draft_turn.status_code, draft_turn.text)
            assistant = draft_turn.json()["assistant_message"]
            draft = assistant["structured_output"]["draft"]
            self.assertEqual(str(original_chunks[0]["id"]), draft["target_clause_id"])
            self.assertEqual(original_chunks[0]["content"], draft["original_text"])
            self.assertIn("百分之二十", draft["proposed_text"])
            self.assertIn("需由业务和法务人员确认", draft["warnings"][-1])

        with open_connection() as connection:
            unchanged_chunks = connection.execute(
                """
                SELECT id, content FROM document_chunks
                WHERE document_id = %s ORDER BY chunk_index
                """,
                (self.document_id,),
            ).fetchall()
        self.assertEqual(original_chunks, unchanged_chunks)


if __name__ == "__main__":
    unittest.main()
