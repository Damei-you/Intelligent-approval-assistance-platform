from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.core.database import open_connection
from app.modules.contract_chat.exceptions import ContractChatError
from app.modules.contract_chat.schemas import (
    ChatMessageCreateRequest,
    ChatSessionDetail,
    ChatTurnResponse,
)


STALE_TURN_MINUTES = 10


class ContractChatRepository:
    """持久化风险项会话、消息、引用和每轮 LangGraph 追踪记录。"""

    def create_or_get_session(self, finding_id: UUID) -> ChatSessionDetail:
        """为一个风险结论创建唯一会话；重复点击入口时恢复原会话。"""

        with open_connection() as connection:
            with connection.transaction():
                finding = connection.execute(
                    """
                    SELECT rf.id, rf.review_run_id, rr.contract_id,
                           rr.contract_document_id, rr.status AS review_status,
                           rci.name AS check_name
                    FROM risk_findings rf
                    JOIN review_runs rr ON rr.id = rf.review_run_id
                    JOIN review_check_items rci ON rci.id = rf.check_item_id
                    WHERE rf.id = %s
                    """,
                    (finding_id,),
                ).fetchone()
                if finding is None:
                    raise ContractChatError("FINDING_NOT_FOUND", "风险结论不存在。", 404)
                if finding["review_status"] != "SUCCEEDED":
                    raise ContractChatError(
                        "REVIEW_NOT_READY",
                        "风险审查完成后才能就结论继续询问。",
                        409,
                    )
                # finding_id 的部分唯一索引确保一个风险项只有一个可恢复会话。
                session = connection.execute(
                    """
                    INSERT INTO chat_sessions (
                        contract_id, review_run_id, finding_id,
                        contract_document_id, title
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (finding_id) WHERE finding_id IS NOT NULL
                    DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                    RETURNING id
                    """,
                    (
                        finding["contract_id"],
                        finding["review_run_id"],
                        finding_id,
                        finding["contract_document_id"],
                        f"就{finding['check_name']}继续询问",
                    ),
                ).fetchone()
        return self.get_session_detail(session["id"])

    def get_session_detail(self, session_id: UUID) -> ChatSessionDetail:
        """返回会话锚点、完整消息和由数据库正文恢复的引用。"""

        # 浏览器恢复会话时也会触发超时占位清理；这样服务进程意外中断后，
        # 前端轮询不必永远停在 PENDING，用户可以明确看到失败并重新发送。
        self._expire_stale_turns(session_id)
        with open_connection() as connection:
            session = connection.execute(
                """
                SELECT cs.id AS session_id, cs.finding_id, cs.review_run_id,
                       cs.contract_id, cs.contract_document_id, cs.title,
                       cs.created_at, cs.updated_at,
                       c.contract_no, c.name AS contract_name,
                       d.revision_no,
                       rci.code AS check_code, rci.name AS check_name,
                       rf.status AS finding_status, rf.severity,
                       rf.title AS finding_title, rf.description,
                       rf.suggestion
                FROM chat_sessions cs
                JOIN contracts c ON c.id = cs.contract_id
                JOIN documents d ON d.id = cs.contract_document_id
                JOIN risk_findings rf ON rf.id = cs.finding_id
                JOIN review_check_items rci ON rci.id = rf.check_item_id
                WHERE cs.id = %s
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                raise ContractChatError("CHAT_SESSION_NOT_FOUND", "对话会话不存在。", 404)
            messages = connection.execute(
                """
                SELECT message.id, message.role, message.content, message.intent,
                       message.status, message.structured_output, message.created_at
                FROM chat_messages message
                LEFT JOIN chat_messages parent
                  ON parent.id = message.reply_to_message_id
                WHERE message.session_id = %s
                -- 同一事务插入的用户和助手消息具有相同 CURRENT_TIMESTAMP；
                -- 先按用户消息所属轮次分组，再固定 USER 在 ASSISTANT 之前。
                ORDER BY COALESCE(parent.created_at, message.created_at),
                         CASE message.role
                              WHEN 'SYSTEM' THEN 0
                              WHEN 'USER' THEN 1
                              ELSE 2
                         END,
                         message.id
                """,
                (session_id,),
            ).fetchall()
            message_ids = [row["id"] for row in messages]
            citation_rows = []
            if message_ids:
                citation_rows = connection.execute(
                    """
                    SELECT cmc.message_id, cmc.chunk_id, cmc.citation_label,
                           CASE WHEN d.document_type = 'CONTRACT'
                                THEN 'CONTRACT' ELSE 'POLICY' END AS evidence_type,
                           d.title AS document_title, dc.clause_no, dc.title,
                           cmc.cited_text, cmc.relevance_score, cmc.sort_order
                    FROM chat_message_citations cmc
                    JOIN document_chunks dc ON dc.id = cmc.chunk_id
                    JOIN documents d ON d.id = dc.document_id
                    WHERE cmc.message_id = ANY(%s)
                    ORDER BY cmc.message_id, cmc.sort_order
                    """,
                    (message_ids,),
                ).fetchall()

        citations_by_message: dict[UUID, list[dict[str, Any]]] = {}
        label_counts: dict[tuple[UUID, str], int] = {}
        for row in citation_rows:
            citation = dict(row)
            message_id = citation.pop("message_id")
            citation.pop("sort_order")
            evidence_type = citation["evidence_type"]
            key = (message_id, evidence_type)
            prefix = "C" if evidence_type == "CONTRACT" else "P"
            citation_label = citation.get("citation_label")
            if citation_label:
                # 新消息直接恢复模型实际使用的标签；数字用于兼容同一消息里的历史 NULL 标签。
                label_counts[key] = max(
                    label_counts.get(key, 0), int(citation_label[1:])
                )
            else:
                label_counts[key] = label_counts.get(key, 0) + 1
                citation["citation_label"] = f"{prefix}{label_counts[key]}"
            citations_by_message.setdefault(message_id, []).append(citation)

        payload = {
            **{
                key: session[key]
                for key in (
                    "session_id",
                    "finding_id",
                    "review_run_id",
                    "contract_id",
                    "contract_document_id",
                    "contract_no",
                    "contract_name",
                    "revision_no",
                    "title",
                    "created_at",
                    "updated_at",
                )
            },
            "finding": {
                "check_code": session["check_code"],
                "check_name": session["check_name"],
                "status": session["finding_status"],
                "severity": session["severity"],
                "title": session["finding_title"],
                "description": session["description"],
                "suggestion": session["suggestion"],
            },
            "messages": [
                {
                    **dict(message),
                    "structured_output": message["structured_output"] or {},
                    "citations": citations_by_message.get(message["id"], []),
                }
                for message in messages
            ],
        }
        return ChatSessionDetail.model_validate(payload)

    def begin_turn(
        self, session_id: UUID, request: ChatMessageCreateRequest
    ) -> dict[str, Any]:
        """在一个事务中保存用户消息、占位回答和工作流，避免留下半轮对话。"""

        with open_connection() as connection:
            with connection.transaction():
                # 超时回收按“消息 -> 工作流 -> 会话”加锁，与完成一轮的顺序保持一致，
                # 再锁定会话串行化新发送，避免两条路径形成反向锁顺序。
                _expire_stale_turns_on_connection(connection, session_id)
                # FOR UPDATE 串行化同一会话的发送操作，确保多轮消息顺序稳定。
                session = connection.execute(
                    "SELECT id FROM chat_sessions WHERE id = %s FOR UPDATE",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise ContractChatError(
                        "CHAT_SESSION_NOT_FOUND", "对话会话不存在。", 404
                    )
                existing = connection.execute(
                    """
                    SELECT user_message.id AS user_message_id,
                           user_message.content AS original_content,
                           user_message.intent AS original_intent,
                           assistant.id AS assistant_message_id,
                           assistant.status AS assistant_status,
                           workflow.id AS workflow_run_id
                    FROM chat_messages user_message
                    LEFT JOIN chat_messages assistant
                      ON assistant.reply_to_message_id = user_message.id
                    LEFT JOIN workflow_runs workflow
                      ON workflow.chat_message_id = assistant.id
                    WHERE user_message.session_id = %s
                      AND user_message.client_request_id = %s
                    """,
                    (session_id, request.client_request_id),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["original_content"] != request.content
                        or existing["original_intent"] != request.intent
                    ):
                        raise ContractChatError(
                            "CHAT_IDEMPOTENCY_CONFLICT",
                            "同一 client_request_id 不能用于不同的问题或提问类型。",
                            409,
                        )
                    return {**dict(existing), "created": False}
                pending = connection.execute(
                    """
                    SELECT 1 FROM chat_messages
                    WHERE session_id = %s AND role = 'ASSISTANT' AND status = 'PENDING'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if pending is not None:
                    raise ContractChatError(
                        "CHAT_TURN_IN_PROGRESS",
                        "当前问题仍在生成回答，请完成后再继续询问。",
                        409,
                    )

                user_message_id = uuid4()
                assistant_message_id = uuid4()
                workflow_run_id = uuid4()
                connection.execute(
                    """
                    INSERT INTO chat_messages (
                        id, session_id, role, content, intent,
                        client_request_id, status
                    ) VALUES (%s, %s, 'USER', %s, %s, %s, 'SUCCEEDED')
                    """,
                    (
                        user_message_id,
                        session_id,
                        request.content,
                        request.intent,
                        request.client_request_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO chat_messages (
                        id, session_id, role, content, intent,
                        reply_to_message_id, status
                    ) VALUES (%s, %s, 'ASSISTANT', %s, %s, %s, 'PENDING')
                    """,
                    (
                        assistant_message_id,
                        session_id,
                        "正在根据风险结论和依据生成回答。",
                        request.intent,
                        user_message_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_runs (
                        id, run_type, chat_message_id, graph_name,
                        graph_version, status, started_at
                    ) VALUES (
                        %s, 'CONTRACT_CHAT', %s, 'contract_risk_chat',
                        '1.0', 'RUNNING', CURRENT_TIMESTAMP
                    )
                    """,
                    (workflow_run_id, assistant_message_id),
                )
                connection.execute(
                    "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (session_id,),
                )
        return {
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "workflow_run_id": workflow_run_id,
            "assistant_status": "PENDING",
            "created": True,
        }

    def get_turn_response(
        self, session_id: UUID, user_message_id: UUID, assistant_message_id: UUID
    ) -> ChatTurnResponse:
        session = self.get_session_detail(session_id)
        messages = {message.id: message for message in session.messages}
        if user_message_id not in messages or assistant_message_id not in messages:
            raise ContractChatError("CHAT_TURN_NOT_FOUND", "对话消息不存在。", 404)
        return ChatTurnResponse(
            session_id=session_id,
            user_message=messages[user_message_id],
            assistant_message=messages[assistant_message_id],
        )

    def get_generation_context(
        self, session_id: UUID, user_message_id: UUID, history_limit: int = 10
    ) -> dict[str, Any]:
        """加载固定风险锚点、审查证据和有限历史，避免上下文无限增长。"""

        with open_connection() as connection:
            context = connection.execute(
                """
                SELECT cs.id AS session_id, cs.finding_id, cs.review_run_id,
                       cs.contract_id, cs.contract_document_id,
                       c.contract_no, c.name AS contract_name,
                       d.revision_no, rci.code AS check_code,
                       rci.name AS check_name, rf.status AS finding_status,
                       rf.severity, rf.title AS finding_title,
                       rf.description, rf.suggestion
                FROM chat_sessions cs
                JOIN contracts c ON c.id = cs.contract_id
                JOIN documents d ON d.id = cs.contract_document_id
                JOIN risk_findings rf ON rf.id = cs.finding_id
                JOIN review_check_items rci ON rci.id = rf.check_item_id
                WHERE cs.id = %s
                """,
                (session_id,),
            ).fetchone()
            if context is None:
                raise ContractChatError("CHAT_SESSION_NOT_FOUND", "对话会话不存在。", 404)
            evidence = connection.execute(
                """
                SELECT dc.id, dc.document_id, d.title AS document_title,
                       dc.clause_no, dc.title, fe.cited_text AS content,
                       fe.relevance_score::DOUBLE PRECISION AS similarity_score,
                       fe.evidence_type
                FROM finding_evidence fe
                JOIN document_chunks dc ON dc.id = fe.chunk_id
                JOIN documents d ON d.id = dc.document_id
                WHERE fe.finding_id = %s
                ORDER BY fe.evidence_type, fe.sort_order
                """,
                (context["finding_id"],),
            ).fetchall()
            history = connection.execute(
                """
                SELECT message.id, message.role, message.content,
                       message.structured_output
                FROM chat_messages message
                LEFT JOIN chat_messages parent
                  ON parent.id = message.reply_to_message_id
                WHERE message.session_id = %s
                  AND message.status = 'SUCCEEDED' AND message.id <> %s
                  AND (
                      message.role <> 'USER'
                      OR EXISTS (
                          SELECT 1 FROM chat_messages reply
                          WHERE reply.reply_to_message_id = message.id
                            AND reply.status = 'SUCCEEDED'
                      )
                  )
                -- 先倒序取最近消息，Python 再 reversed 恢复为 USER -> ASSISTANT 的时间顺序。
                ORDER BY COALESCE(parent.created_at, message.created_at) DESC,
                         CASE WHEN message.role = 'ASSISTANT' THEN 0 ELSE 1 END,
                         message.id DESC
                LIMIT %s
                """,
                (session_id, user_message_id, history_limit),
            ).fetchall()
            history_message_ids = [row["id"] for row in history]
            history_citation_rows = []
            if history_message_ids:
                history_citation_rows = connection.execute(
                    """
                    SELECT cmc.message_id, cmc.chunk_id, dc.document_id,
                           cmc.citation_label,
                           CASE WHEN d.document_type = 'CONTRACT'
                                THEN 'CONTRACT' ELSE 'POLICY' END AS evidence_type,
                           d.title AS document_title, dc.clause_no, dc.title,
                           cmc.cited_text,
                           cmc.relevance_score::DOUBLE PRECISION AS similarity_score,
                           cmc.sort_order
                    FROM chat_message_citations cmc
                    JOIN document_chunks dc ON dc.id = cmc.chunk_id
                    JOIN documents d ON d.id = dc.document_id
                    WHERE cmc.message_id = ANY(%s)
                    ORDER BY cmc.message_id, cmc.sort_order
                    """,
                    (history_message_ids,),
                ).fetchall()

        history_citations: dict[UUID, list[dict[str, Any]]] = {}
        fallback_counts: dict[tuple[UUID, str], int] = {}
        for row in history_citation_rows:
            citation = dict(row)
            message_id = citation.pop("message_id")
            citation.pop("sort_order")
            if not citation.get("citation_label"):
                evidence_type = citation["evidence_type"]
                key = (message_id, evidence_type)
                fallback_counts[key] = fallback_counts.get(key, 0) + 1
                prefix = "C" if evidence_type == "CONTRACT" else "P"
                citation["citation_label"] = f"{prefix}{fallback_counts[key]}"
            history_citations.setdefault(message_id, []).append(citation)

        ordered_history = []
        for row in reversed(history):
            item = dict(row)
            message_id = item.pop("id")
            item["structured_output"] = item["structured_output"] or {}
            item["citations"] = history_citations.get(message_id, [])
            ordered_history.append(item)
        return {
            **dict(context),
            "anchor_evidence": [dict(row) for row in evidence],
            "history": ordered_history,
        }

    def _expire_stale_turns(self, session_id: UUID) -> None:
        """把进程中断后遗留的超时占位回答转为失败，解除会话发送锁。"""

        with open_connection() as connection:
            with connection.transaction():
                _expire_stale_turns_on_connection(connection, session_id)

    def search_contract_chunks(
        self, document_id: UUID, query_vector: list[float], top_k: int = 3
    ) -> list[dict[str, Any]]:
        vector_literal = _to_vector_literal(query_vector)
        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT dc.id, dc.document_id, d.title AS document_title,
                       dc.clause_no, dc.title, dc.content,
                       (1 - (dc.embedding <=> %s::vector))::DOUBLE PRECISION AS similarity_score,
                       'CONTRACT' AS evidence_type
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.document_id = %s
                  AND dc.chunk_type = 'CONTRACT_CLAUSE'
                  AND dc.embedding IS NOT NULL
                ORDER BY dc.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, document_id, vector_literal, top_k),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_policy_chunks(
        self, query_vector: list[float], top_k: int = 10
    ) -> list[dict[str, Any]]:
        vector_literal = _to_vector_literal(query_vector)
        with open_connection() as connection:
            rows = connection.execute(
                """
                SELECT dc.id, dc.document_id, d.title AS document_title,
                       dc.clause_no, dc.title, dc.content,
                       (1 - (dc.embedding <=> %s::vector))::DOUBLE PRECISION AS similarity_score,
                       'POLICY' AS evidence_type
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE d.document_type = 'POLICY' AND d.is_current = TRUE
                  AND dc.chunk_type = 'POLICY_SECTION'
                  AND dc.embedding IS NOT NULL
                ORDER BY dc.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, vector_literal, top_k),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_node_run(
        self,
        workflow_run_id: UUID,
        node_name: str,
        sequence_no: int,
        input_data: dict[str, Any],
    ) -> UUID:
        node_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                created = connection.execute(
                    """
                    INSERT INTO workflow_node_runs (
                        id, workflow_run_id, node_name, sequence_no,
                        status, input_data, started_at
                    )
                    SELECT %s, %s, %s, %s, 'RUNNING', %s, CURRENT_TIMESTAMP
                    WHERE EXISTS (
                        SELECT 1 FROM workflow_runs
                        WHERE id = %s AND status = 'RUNNING'
                    )
                    RETURNING id
                    """,
                    (
                        node_run_id,
                        workflow_run_id,
                        node_name,
                        sequence_no,
                        Jsonb(input_data),
                        workflow_run_id,
                    ),
                ).fetchone()
                if created is None:
                    raise ContractChatError(
                        "CHAT_TURN_EXPIRED",
                        "本轮回答已经超时结束，请重新发送问题。",
                        409,
                    )
        return node_run_id

    def finish_node(self, node_run_id: UUID, output_data: dict[str, Any]) -> None:
        with open_connection() as connection:
            with connection.transaction():
                updated = connection.execute(
                    """
                    UPDATE workflow_node_runs
                    SET status = 'SUCCEEDED', output_data = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        latency_ms = (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::INTEGER
                    WHERE id = %s AND status = 'RUNNING'
                    RETURNING id
                    """,
                    (Jsonb(output_data), node_run_id),
                ).fetchone()
                if updated is None:
                    raise ContractChatError(
                        "CHAT_TURN_EXPIRED",
                        "本轮回答已经超时结束，请重新发送问题。",
                        409,
                    )

    def record_retrieval(
        self,
        node_run_id: UUID,
        query_text: str,
        filters: dict[str, Any],
        hits: list[dict[str, Any]],
        model_name: str,
        *,
        final_top_k: int | None = None,
        ranking_strategy: str = "VECTOR",
        rerank_model: str | None = None,
        rerank_latency_ms: int | None = None,
        rerank_error: str | None = None,
    ) -> None:
        retrieval_run_id = uuid4()
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO retrieval_runs (
                        id, node_run_id, query_text, query_embedding_model,
                        filters, top_k, final_top_k, ranking_strategy,
                        rerank_model, rerank_latency_ms, rerank_error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        retrieval_run_id,
                        node_run_id,
                        query_text,
                        model_name,
                        Jsonb(filters),
                        max(1, len(hits)),
                        final_top_k,
                        ranking_strategy,
                        rerank_model,
                        rerank_latency_ms,
                        rerank_error,
                    ),
                )
                if hits:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO retrieval_hits (
                                retrieval_run_id, chunk_id, rank_no,
                                similarity_score, rerank_rank_no,
                                rerank_score, selected_for_context
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    retrieval_run_id,
                                    hit["id"],
                                    hit.get("vector_rank_no", rank),
                                    max(-1, min(1, hit["similarity_score"])),
                                    hit.get("rerank_rank_no"),
                                    hit.get("rerank_score"),
                                    hit.get("selected_for_context", True),
                                )
                                for rank, hit in enumerate(hits, 1)
                            ],
                        )

    def record_llm_call(
        self,
        node_run_id: UUID,
        intent: str,
        output_data: dict[str, Any],
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        model_name: str,
    ) -> None:
        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO llm_calls (
                        node_run_id, provider, model_name, prompt_name,
                        input_summary, output_data, input_tokens,
                        output_tokens, latency_ms, status
                    ) VALUES (
                        %s, 'DASHSCOPE', %s, 'contract_chat_v1',
                        %s, %s, %s, %s, %s, 'SUCCEEDED'
                    )
                    """,
                    (
                        node_run_id,
                        model_name,
                        Jsonb({"intent": intent}),
                        Jsonb(output_data),
                        input_tokens,
                        output_tokens,
                        latency_ms,
                    ),
                )

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
        """回答、引用和工作流状态在同一个事务中完成，避免展示无引用的半成品。"""

        with open_connection() as connection:
            with connection.transaction():
                updated_message = connection.execute(
                    """
                    UPDATE chat_messages
                    SET content = %s, intent = %s, model_name = %s,
                        token_usage = %s, latency_ms = %s,
                        structured_output = %s, status = 'SUCCEEDED'
                    WHERE id = %s AND status = 'PENDING'
                    RETURNING id
                    """,
                    (
                        content,
                        intent,
                        model_name,
                        Jsonb(usage),
                        latency_ms,
                        Jsonb(structured_output),
                        assistant_message_id,
                    ),
                ).fetchone()
                if updated_message is None:
                    raise ContractChatError(
                        "CHAT_TURN_EXPIRED",
                        "本轮回答已经超时结束，请重新发送问题。",
                        409,
                    )
                if citations:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO chat_message_citations (
                                message_id, chunk_id, citation_label, relevance_score,
                                cited_text, sort_order
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    assistant_message_id,
                                    citation["id"],
                                    citation["citation_label"],
                                    citation.get("similarity_score"),
                                    citation["content"],
                                    index,
                                )
                                for index, citation in enumerate(citations, 1)
                            ],
                        )
                connection.execute(
                    """
                    UPDATE workflow_node_runs
                    SET status = 'SUCCEEDED', output_data = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        latency_ms = (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::INTEGER
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "assistant_message_id": str(assistant_message_id),
                                "citation_count": len(citations),
                                "has_draft": bool(structured_output.get("draft")),
                            }
                        ),
                        persist_node_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE workflow_runs
                    SET status = 'SUCCEEDED', state_snapshot = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "intent": intent,
                                "citation_count": len(citations),
                                "has_draft": bool(structured_output.get("draft")),
                            }
                        ),
                        workflow_run_id,
                    ),
                )
                connection.execute(
                    "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (session_id,),
                )

    def fail_turn(
        self, assistant_message_id: UUID, workflow_run_id: UUID, message: str
    ) -> None:
        """模型或检索失败时保留用户问题，并明确结束占位回答与运行记录。"""

        with open_connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE chat_messages
                    SET content = %s, status = 'FAILED'
                    WHERE id = %s AND status = 'PENDING'
                    """,
                    (message, assistant_message_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_node_runs
                    SET status = 'FAILED', error_message = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        latency_ms = (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::INTEGER
                    WHERE workflow_run_id = %s AND status = 'RUNNING'
                    """,
                    (message, workflow_run_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_runs
                    SET status = 'FAILED', error_message = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING'
                    """,
                    (message, workflow_run_id),
                )


def _to_vector_literal(vector: list[float]) -> str:
    """把浮点数组转换为 pgvector 可以参数化绑定的文本表示。"""

    return "[" + ",".join(format(value, ".10g") for value in vector) + "]"


def _expire_stale_turns_on_connection(connection: Any, session_id: UUID) -> None:
    """在现有事务内回收超时 PENDING，并同步结束对应工作流记录。"""

    stale_rows = connection.execute(
        """
        UPDATE chat_messages
        SET content = %s, status = 'FAILED'
        WHERE session_id = %s
          AND role = 'ASSISTANT'
          AND status = 'PENDING'
          AND created_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
        RETURNING id
        """,
        (
            "上一次回答生成超时，请重新发送问题。",
            session_id,
            STALE_TURN_MINUTES,
        ),
    ).fetchall()
    stale_message_ids = [row["id"] for row in stale_rows]
    if not stale_message_ids:
        return

    # 一个助手占位消息对应一个工作流；只结束仍处于 RUNNING 的记录，
    # 避免恢复逻辑覆盖已经成功或失败的可观测状态。
    connection.execute(
        """
        UPDATE workflow_node_runs
        SET status = 'FAILED', error_message = %s,
            completed_at = CURRENT_TIMESTAMP,
            latency_ms = (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::INTEGER
        WHERE status = 'RUNNING'
          AND workflow_run_id IN (
              SELECT id FROM workflow_runs WHERE chat_message_id = ANY(%s)
          )
        """,
        ("回答生成超时，工作流已自动回收。", stale_message_ids),
    )
    connection.execute(
        """
        UPDATE workflow_runs
        SET status = 'FAILED', error_message = %s,
            completed_at = CURRENT_TIMESTAMP
        WHERE status = 'RUNNING' AND chat_message_id = ANY(%s)
        """,
        ("回答生成超时，工作流已自动回收。", stale_message_ids),
    )
    connection.execute(
        "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (session_id,),
    )
