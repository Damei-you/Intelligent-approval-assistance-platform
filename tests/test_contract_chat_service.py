from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID, uuid4

from app.modules.contract_chat.exceptions import ContractChatError
from app.modules.contract_chat.schemas import (
    ChatMessageCreateRequest,
    ModelChatAnswer,
    ModelClauseDraft,
)
from app.modules.contract_chat.service import ContractChatService, _format_history
from app.modules.risk_review.service import PolicyRerankResult


class FakeEmbedding:
    """用固定向量替代外部 Embedding 服务，并记录实际检索问题。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts]


class FakeModel:
    """返回可控的结构化回答，避免单元测试调用真实大模型。"""

    def __init__(self, answer: ModelChatAnswer | None = None) -> None:
        self.configured_answer = answer
        self.calls: list[dict[str, Any]] = []

    def answer(
        self,
        intent: str,
        question: str,
        context: dict[str, Any],
        contract_chunks: list[dict[str, Any]],
        policy_chunks: list[dict[str, Any]],
    ) -> tuple[ModelChatAnswer, dict[str, int | None], int]:
        self.calls.append(
            {
                "intent": intent,
                "question": question,
                "context": context,
                "contract_chunks": list(contract_chunks),
                "policy_chunks": list(policy_chunks),
            }
        )
        answer = self.configured_answer
        if answer is None:
            draft = None
            if intent == "DRAFT_CLAUSE":
                draft = ModelClauseDraft(
                    target_clause_ref="C1",
                    proposed_text="建议修改后的合同条款。",
                    change_summary="收紧风险条款。",
                    rationale="与制度要求保持一致。",
                )
            answer = ModelChatAnswer(
                answer="这是基于已提供证据生成的回答。",
                contract_refs=["C1"],
                policy_refs=["P1"],
                draft=draft,
            )
        return answer, {"input_tokens": 20, "output_tokens": 8}, 12


class FakeRerank:
    """保留制度候选顺序，模拟重排序接口的返回结构。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def rerank(
        self, query: str, hits: list[dict[str, Any]], final_top_k: int
    ) -> PolicyRerankResult:
        self.calls.append(
            {"query": query, "hits": list(hits), "final_top_k": final_top_k}
        )
        ranked = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": index,
                "rerank_score": 1 - index / 10,
                "selected_for_context": index <= final_top_k,
            }
            for index, hit in enumerate(hits, 1)
        ]
        return PolicyRerankResult(
            selected_hits=ranked[:final_top_k],
            all_hits=ranked,
            latency_ms=3,
        )


class FakeRepository:
    """在内存中记录对话服务的持久化边界和检索参数。"""

    def __init__(
        self,
        *,
        history: list[dict[str, Any]] | None = None,
        begin_created: bool = True,
        existing_status: str = "SUCCEEDED",
    ) -> None:
        self.session_id = uuid4()
        self.finding_id = uuid4()
        self.document_id = uuid4()
        self.user_message_id = uuid4()
        self.assistant_message_id = uuid4()
        self.workflow_run_id = uuid4()
        self.begin_created = begin_created
        self.existing_status = existing_status
        self.history = history or []
        self.anchor_contract = self._chunk(
            "审查时保存的合同原文。", "CONTRACT", clause_no="第十条"
        )
        self.anchor_policy = self._chunk(
            "审查时保存的制度依据。", "POLICY", clause_no="第三条"
        )
        self.contract_hits = [
            self._chunk("后端检索到的第一条合同原文。", "CONTRACT", clause_no="第一条"),
            self._chunk("后端检索到的第二条合同原文。", "CONTRACT", clause_no="第二条"),
        ]
        self.policy_hits = [
            self._chunk("当前制度检索结果。", "POLICY", clause_no="第五条")
        ]
        self.begin_requests: list[ChatMessageCreateRequest] = []
        self.history_limits: list[int] = []
        self.contract_searches: list[dict[str, Any]] = []
        self.policy_searches: list[dict[str, Any]] = []
        self.node_runs: list[dict[str, Any]] = []
        self.finished_nodes: list[dict[str, Any]] = []
        self.retrieval_records: list[dict[str, Any]] = []
        self.llm_records: list[dict[str, Any]] = []
        self.completed_turns: list[dict[str, Any]] = []
        self.failed_turns: list[dict[str, Any]] = []
        self.turn_response = {"result": "existing-turn"}

    @staticmethod
    def _chunk(
        content: str,
        evidence_type: str,
        *,
        clause_no: str,
    ) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "document_id": uuid4(),
            "document_title": "测试文档",
            "clause_no": clause_no,
            "title": "测试条目",
            "content": content,
            "similarity_score": 0.9,
            "evidence_type": evidence_type,
        }

    def begin_turn(
        self, session_id: UUID, request: ChatMessageCreateRequest
    ) -> dict[str, Any]:
        self.begin_requests.append(request)
        return {
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "workflow_run_id": self.workflow_run_id,
            "assistant_status": self.existing_status if not self.begin_created else "PENDING",
            "created": self.begin_created,
        }

    def get_turn_response(
        self, session_id: UUID, user_message_id: UUID, assistant_message_id: UUID
    ) -> dict[str, str]:
        return self.turn_response

    def get_generation_context(
        self, session_id: UUID, user_message_id: UUID, history_limit: int
    ) -> dict[str, Any]:
        self.history_limits.append(history_limit)
        return {
            "session_id": session_id,
            "finding_id": self.finding_id,
            "review_run_id": uuid4(),
            "contract_id": uuid4(),
            "contract_document_id": self.document_id,
            "contract_no": "CHAT-TEST-001",
            "contract_name": "对话测试合同",
            "revision_no": 2,
            "check_code": "PAYMENT_TERMS",
            "check_name": "付款风险",
            "finding_status": "RISK",
            "severity": "HIGH",
            "finding_title": "预付款比例过高",
            "description": "预付款约定超出制度要求。",
            "suggestion": "建议降低预付款比例。",
            "anchor_evidence": [self.anchor_contract, self.anchor_policy],
            # 真实 Repository 在 SQL 中 LIMIT 后再恢复时间顺序；Fake 保持同一行为。
            "history": self.history[-history_limit:],
        }

    def search_contract_chunks(
        self, document_id: UUID, query_vector: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        self.contract_searches.append(
            {
                "document_id": document_id,
                "query_vector": query_vector,
                "top_k": top_k,
            }
        )
        return list(self.contract_hits)

    def search_policy_chunks(
        self, query_vector: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        self.policy_searches.append(
            {"query_vector": query_vector, "top_k": top_k}
        )
        return list(self.policy_hits)

    def create_node_run(
        self,
        workflow_run_id: UUID,
        node_name: str,
        sequence_no: int,
        input_data: dict[str, Any],
    ) -> UUID:
        node_run_id = uuid4()
        self.node_runs.append(
            {
                "id": node_run_id,
                "name": node_name,
                "sequence_no": sequence_no,
                "input_data": input_data,
            }
        )
        return node_run_id

    def finish_node(self, node_run_id: UUID, output_data: dict[str, Any]) -> None:
        self.finished_nodes.append(
            {"node_run_id": node_run_id, "output_data": output_data}
        )

    def record_retrieval(self, *args: Any, **kwargs: Any) -> None:
        self.retrieval_records.append({"args": args, "kwargs": kwargs})

    def record_llm_call(self, *args: Any, **kwargs: Any) -> None:
        self.llm_records.append({"args": args, "kwargs": kwargs})

    def complete_turn(
        self,
        session_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        workflow_run_id: UUID,
        persist_node_id: UUID,
        intent: str,
        content: str,
        structured_output: dict[str, Any],
        citations: list[dict[str, Any]],
        usage: dict[str, int | None],
        latency_ms: int,
        model_name: str,
    ) -> None:
        self.completed_turns.append(
            {
                "intent": intent,
                "content": content,
                "structured_output": structured_output,
                "citations": citations,
                "usage": usage,
                "latency_ms": latency_ms,
                "model_name": model_name,
            }
        )

    def fail_turn(
        self, assistant_message_id: UUID, workflow_run_id: UUID, message: str
    ) -> None:
        self.failed_turns.append(
            {
                "assistant_message_id": assistant_message_id,
                "workflow_run_id": workflow_run_id,
                "message": message,
            }
        )


class ContractChatServiceTests(unittest.TestCase):
    def _send(
        self,
        service: ContractChatService,
        repository: FakeRepository,
        content: str,
        intent: str = "AUTO",
    ) -> Any:
        request = ChatMessageCreateRequest(
            content=content,
            intent=intent,
            client_request_id=uuid4(),
        )
        return service.send_message(repository.session_id, request)

    def test_auto_intent_routes_to_three_supported_flows(self) -> None:
        """AUTO 应按透明关键词分别进入解释、依据查询和草案生成。"""

        cases = (
            ("为什么判定为高风险？", "EXPLAIN", "anchor_context", False),
            ("上一版草案为什么这样调整？", "EXPLAIN", "anchor_context", False),
            ("请给出相关制度依据。", "EVIDENCE_QUERY", "rag_context", True),
            ("请生成一份修改草案。", "DRAFT_CLAUSE", "rag_context", True),
            ("请将预付款比例改为20%。", "DRAFT_CLAUSE", "rag_context", True),
        )
        for content, expected_intent, expected_node, uses_embedding in cases:
            with self.subTest(expected_intent=expected_intent):
                repository = FakeRepository()
                embedding = FakeEmbedding()
                model = FakeModel()
                service = ContractChatService(
                    repository=repository,
                    embedding_provider=embedding,
                    model_provider=model,
                    rerank_provider=FakeRerank(),
                )

                self._send(service, repository, content)

                self.assertEqual(expected_intent, model.calls[0]["intent"])
                self.assertIn(expected_node, [item["name"] for item in repository.node_runs])
                self.assertEqual(uses_embedding, bool(embedding.calls))

    def test_explain_uses_only_anchor_evidence_without_embedding(self) -> None:
        """解释既有结论时不得重新检索，避免证据随当前制度变化。"""

        repository = FakeRepository()
        embedding = FakeEmbedding()
        model = FakeModel()
        service = ContractChatService(
            repository=repository,
            embedding_provider=embedding,
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "解释这个风险结论。", "EXPLAIN")

        self.assertEqual([], embedding.calls)
        self.assertEqual([], repository.contract_searches)
        self.assertEqual([], repository.policy_searches)
        self.assertEqual(
            "REVIEW_SNAPSHOT",
            model.calls[0]["contract_chunks"][0]["source_scope"],
        )
        self.assertEqual(
            repository.anchor_contract["id"],
            model.calls[0]["contract_chunks"][0]["id"],
        )
        self.assertEqual(
            "REVIEW_SNAPSHOT",
            model.calls[0]["policy_chunks"][0]["source_scope"],
        )

    def test_evidence_query_limits_document_and_history(self) -> None:
        """依据查询只能检索绑定修订版本，并最多向模型携带十条历史消息。"""

        history = [
            {
                "role": "USER" if index % 2 == 0 else "ASSISTANT",
                "content": f"历史消息{index}",
            }
            for index in range(14)
        ]
        repository = FakeRepository(history=history)
        model = FakeModel()
        embedding = FakeEmbedding()
        service = ContractChatService(
            repository=repository,
            embedding_provider=embedding,
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "制度依据的原文是什么？", "EVIDENCE_QUERY")

        self.assertEqual([10], repository.history_limits)
        self.assertEqual(history[-10:], model.calls[0]["context"]["history"])
        self.assertEqual(repository.document_id, repository.contract_searches[0]["document_id"])
        self.assertEqual(3, repository.contract_searches[0]["top_k"])
        contract_retrieval = repository.retrieval_records[0]
        self.assertEqual(
            str(repository.document_id),
            contract_retrieval["args"][2]["document_id"],
        )

    def test_previous_draft_explanation_restores_its_citation_chunks(self) -> None:
        """追问上一版草案时应走 RAG，并把上一轮引用按真实 chunk_id 带回候选。"""

        previous_contract_id = uuid4()
        previous_policy_id = uuid4()
        repository = FakeRepository(
            history=[
                {
                    "role": "ASSISTANT",
                    "content": "上一轮生成了草案。",
                    "structured_output": {},
                    "citations": [
                        {
                            "chunk_id": previous_contract_id,
                            "document_id": None,
                            "citation_label": "C2",
                            "evidence_type": "CONTRACT",
                            "document_title": "测试合同",
                            "clause_no": "第八条",
                            "title": "付款",
                            "cited_text": "上一轮实际引用的合同条款。",
                        },
                        {
                            "chunk_id": previous_policy_id,
                            "document_id": uuid4(),
                            "citation_label": "P2",
                            "evidence_type": "POLICY",
                            "document_title": "付款制度",
                            "clause_no": "第五条",
                            "title": "预付款",
                            "cited_text": "上一轮实际引用的制度条款。",
                        },
                    ],
                }
            ]
        )
        repository.history[0]["citations"][0]["document_id"] = repository.document_id
        model = FakeModel()
        embedding = FakeEmbedding()
        service = ContractChatService(
            repository=repository,
            embedding_provider=embedding,
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "上一版草案为什么这样调整？", "AUTO")

        self.assertTrue(embedding.calls)
        contract_by_id = {
            item["id"]: item for item in model.calls[0]["contract_chunks"]
        }
        policy_by_id = {
            item["id"]: item for item in model.calls[0]["policy_chunks"]
        }
        self.assertEqual("PREVIOUS_TURN", contract_by_id[previous_contract_id]["source_scope"])
        self.assertEqual("PREVIOUS_TURN", policy_by_id[previous_policy_id]["source_scope"])

    def test_previous_risk_conclusion_question_still_uses_review_snapshot(self) -> None:
        """普通的历史结论解释不能因“之前”一词引入当前制度并改变审查边界。"""

        repository = FakeRepository(
            history=[{"role": "USER", "content": "之前的问题。"}]
        )
        embedding = FakeEmbedding()
        service = ContractChatService(
            repository=repository,
            embedding_provider=embedding,
            model_provider=FakeModel(),
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "之前为什么判定为高风险？", "AUTO")

        self.assertEqual([], embedding.calls)
        self.assertIn("anchor_context", [item["name"] for item in repository.node_runs])

    def test_draft_identity_and_original_text_are_filled_from_candidate(self) -> None:
        """模型只选择 C 标签，草案目标 ID 与原文必须由后端候选回填。"""

        fake_model_clause_id = uuid4()
        model_draft = ModelClauseDraft.model_validate(
            {
                "target_clause_ref": "C2",
                "target_clause_id": fake_model_clause_id,
                "original_text": "模型伪造的原条款。",
                "proposed_text": "模型建议的新条款。",
                "change_summary": "调整付款条件。",
                "rationale": "降低付款风险。",
                "warnings": ["模型提示"],
            }
        )
        model = FakeModel(
            ModelChatAnswer(
                answer="已生成待人工确认的草案。",
                contract_refs=["C2"],
                policy_refs=["P1"],
                draft=model_draft,
            )
        )
        repository = FakeRepository()
        service = ContractChatService(
            repository=repository,
            embedding_provider=FakeEmbedding(),
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "生成修改草案。", "DRAFT_CLAUSE")

        persisted_draft = repository.completed_turns[0]["structured_output"]["draft"]
        backend_clause = repository.contract_hits[1]
        # 结构化草案最终写入 JSONB，因此 UUID 应由后端转换为标准字符串。
        self.assertEqual(str(backend_clause["id"]), persisted_draft["target_clause_id"])
        self.assertNotEqual(str(fake_model_clause_id), persisted_draft["target_clause_id"])
        self.assertEqual(backend_clause["content"], persisted_draft["original_text"])
        self.assertNotEqual("模型伪造的原条款。", persisted_draft["original_text"])
        self.assertEqual("模型建议的新条款。", persisted_draft["proposed_text"])

    def test_unknown_and_duplicate_references_are_filtered_before_persist(self) -> None:
        """模型编造、大小写不同或重复的标签不能产生重复引用记录。"""

        model = FakeModel(
            ModelChatAnswer(
                answer="引用已由后端校验。",
                contract_refs=["c1", "C99", "C1", " C2 "],
                policy_refs=["P99", "p1", "P1"],
            )
        )
        repository = FakeRepository()
        service = ContractChatService(
            repository=repository,
            embedding_provider=FakeEmbedding(),
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "查询条款与制度依据。", "EVIDENCE_QUERY")

        citation_ids = [
            item["id"] for item in repository.completed_turns[0]["citations"]
        ]
        citation_labels = [
            item["citation_label"]
            for item in repository.completed_turns[0]["citations"]
        ]
        self.assertEqual(
            [
                repository.contract_hits[0]["id"],
                repository.contract_hits[1]["id"],
                repository.policy_hits[0]["id"],
            ],
            citation_ids,
        )
        self.assertEqual(["C1", "C2", "P1"], citation_labels)
        self.assertEqual(len(citation_ids), len(set(citation_ids)))

    def test_invalid_draft_target_is_blocked_without_binding_first_clause(self) -> None:
        """不存在的草案目标不能静默改绑 C1，否则建议正文可能匹配错误条款。"""

        model = FakeModel(
            ModelChatAnswer(
                answer="已生成草案。",
                contract_refs=["C1"],
                policy_refs=["P1"],
                draft=ModelClauseDraft(
                    target_clause_ref="C99",
                    proposed_text="为不存在的目标生成的文本。",
                    change_summary="错误目标。",
                    rationale="测试后端校验。",
                ),
            )
        )
        repository = FakeRepository()
        service = ContractChatService(
            repository=repository,
            embedding_provider=FakeEmbedding(),
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "生成修改草案。", "DRAFT_CLAUSE")

        completed = repository.completed_turns[0]
        self.assertIsNone(completed["structured_output"]["draft"])
        self.assertEqual([], completed["citations"])
        self.assertIn("系统已阻止展示", completed["content"])

    def test_missing_or_forged_citations_are_not_replaced_with_first_hits(self) -> None:
        """空引用或正文伪造标签必须受控阻止，不能自动制造 C1/P1 关系。"""

        for answer in (
            ModelChatAnswer(answer="没有返回结构化引用。"),
            ModelChatAnswer(
                answer="根据不存在的依据 [C99]。",
                contract_refs=["C1"],
            ),
            ModelChatAnswer(
                answer="正文引用了未写入 refs 的真实候选 [C2]。",
                contract_refs=["C1"],
            ),
        ):
            with self.subTest(answer=answer.answer):
                repository = FakeRepository()
                service = ContractChatService(
                    repository=repository,
                    embedding_provider=FakeEmbedding(),
                    model_provider=FakeModel(answer),
                    rerank_provider=FakeRerank(),
                )

                self._send(service, repository, "查询相关依据。", "EVIDENCE_QUERY")

                completed = repository.completed_turns[0]
                self.assertEqual([], completed["citations"])
                self.assertIn("系统已阻止展示", completed["content"])

    def test_explicit_insufficient_evidence_allows_a_grounded_zero_reference_answer(self) -> None:
        """模型明确声明证据不足时可以零引用，不能被向量 Top-K 候选强制改写。"""

        answer = ModelChatAnswer(
            answer="当前材料不足以确认具体期限，缺少适用制度原文。",
            insufficient_evidence=True,
        )
        repository = FakeRepository()
        service = ContractChatService(
            repository=repository,
            embedding_provider=FakeEmbedding(),
            model_provider=FakeModel(answer),
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "具体期限是多少？", "EVIDENCE_QUERY")

        completed = repository.completed_turns[0]
        self.assertEqual(answer.answer, completed["content"])
        self.assertEqual([], completed["citations"])

    def test_insufficient_evidence_cannot_be_combined_with_a_draft(self) -> None:
        """证据不足与可用草案互相矛盾，后端应阻止这种交叉字段输出。"""

        answer = ModelChatAnswer(
            answer="证据不足但仍返回草案。",
            contract_refs=["C1"],
            insufficient_evidence=True,
            draft=ModelClauseDraft(
                target_clause_ref="C1",
                proposed_text="不应展示的草案。",
                change_summary="矛盾输出。",
                rationale="测试交叉字段校验。",
            ),
        )
        repository = FakeRepository()
        service = ContractChatService(
            repository=repository,
            embedding_provider=FakeEmbedding(),
            model_provider=FakeModel(answer),
            rerank_provider=FakeRerank(),
        )

        self._send(service, repository, "生成草案。", "DRAFT_CLAUSE")

        completed = repository.completed_turns[0]
        self.assertIsNone(completed["structured_output"]["draft"])
        self.assertEqual([], completed["citations"])
        self.assertIn("系统已阻止展示", completed["content"])

    def test_history_format_preserves_previous_draft_and_citation_labels(self) -> None:
        """短期上下文应保留上一版草案以及当轮 P/C 标签到原文的映射。"""

        formatted = _format_history(
            [
                {
                    "role": "ASSISTANT",
                    "content": "上一轮回答。",
                    "structured_output": {
                        "draft": {
                            "original_text": "原付款条款。",
                            "proposed_text": "调整后的付款条款。",
                            "rationale": "降低预付款风险。",
                        }
                    },
                    "citations": [
                        {
                            "citation_label": "P2",
                            "document_title": "付款管理制度",
                            "clause_no": "第五条",
                            "title": "预付款",
                            "cited_text": "预付款原则上不得超过合同金额的百分之二十。",
                        }
                    ],
                }
            ]
        )

        self.assertIn("调整后的付款条款", formatted)
        self.assertIn("[P2]", formatted)
        self.assertIn("标签仅属于上一轮", formatted)
        self.assertIn("百分之二十", formatted)

    def test_existing_client_request_skips_graph_execution(self) -> None:
        """重复 client_request_id 应直接返回首轮结果，不再检索或调用模型。"""

        repository = FakeRepository(begin_created=False)
        embedding = FakeEmbedding()
        model = FakeModel()
        service = ContractChatService(
            repository=repository,
            embedding_provider=embedding,
            model_provider=model,
            rerank_provider=FakeRerank(),
        )

        result = self._send(service, repository, "重复发送的问题。")

        self.assertIs(repository.turn_response, result)
        self.assertEqual([], repository.node_runs)
        self.assertEqual([], repository.completed_turns)
        self.assertEqual([], embedding.calls)
        self.assertEqual([], model.calls)

    def test_pending_duplicate_request_returns_conflict_without_second_graph(self) -> None:
        """首次请求尚未完成时，相同幂等键应提示稍后重试而不是返回永久占位回答。"""

        repository = FakeRepository(begin_created=False, existing_status="PENDING")
        service = ContractChatService(
            repository=repository,
            embedding_provider=FakeEmbedding(),
            model_provider=FakeModel(),
            rerank_provider=FakeRerank(),
        )

        with self.assertRaisesRegex(ContractChatError, "相同请求仍在生成回答"):
            self._send(service, repository, "仍在生成的问题。")

        self.assertEqual([], repository.node_runs)
        self.assertEqual([], repository.completed_turns)


if __name__ == "__main__":
    unittest.main()
