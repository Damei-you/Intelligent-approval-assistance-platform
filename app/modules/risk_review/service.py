from __future__ import annotations

from dataclasses import dataclass
from operator import add
from time import perf_counter
from typing import Annotated, Any, TypedDict
from uuid import UUID

import httpx
from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr

from app.core.config import settings
from app.modules.risk_review.exceptions import RiskReviewError
from app.modules.risk_review.repository import REVIEW_CHECK_CODES, RiskReviewRepository
from app.modules.risk_review.schemas import ModelRiskDecision, RiskReviewCreateResponse
from app.modules.vectorization.service import DashScopeEmbeddingProvider, _safe_error_message


CHECK_QUERIES = {
    "PAYMENT_TERMS": "合同付款方式、预付款比例、付款节点、付款期限，以及制度中的付款和预付款限制",
    "WARRANTY": "合同质保范围、质量保证期限、售后责任，以及制度中的最低质保要求",
    "BREACH_LIABILITY": "合同双方违约责任、违约金、赔偿上限，以及制度中的违约责任要求",
    "DISPUTE_RESOLUTION": "合同适用法律、管辖法院、仲裁约定，以及制度中的争议解决和管辖要求",
}

# 第二次查询不重复首次的概括性表述，而是补充常见条款名称和同义表达。
# 该查询由后端固定生成，避免让模型自由决定检索次数或扩大到当前合同、当前制度之外。
CHECK_RETRY_QUERIES = {
    "PAYMENT_TERMS": "价款支付条件、预付款上限、付款比例、验收、发票、账期和逾期付款约定",
    "WARRANTY": "质量保证、保修期限、缺陷责任、维修更换、售后服务和质保金约定",
    "BREACH_LIABILITY": "违约金计算、损失赔偿、责任限制、逾期履行、解除责任和赔偿上限约定",
    "DISPUTE_RESOLUTION": "适用法律、诉讼管辖、法院所在地、仲裁机构、仲裁地点和争议协商约定",
}

CONTRACT_RERANK_INSTRUCT = (
    "Given a contract risk query, retrieve relevant clauses from the current contract."
)
POLICY_RERANK_INSTRUCT = (
    "Given a contract risk query, retrieve relevant enterprise policy clauses."
)


class ReviewState(TypedDict, total=False):
    """LangGraph 节点间传递的小型状态，只保存标识和结果摘要。"""

    review_run_id: UUID
    job_id: UUID
    context: dict[str, Any]
    # 四个并行节点都会写 findings。Annotated 指定 add reducer（归并函数），
    # LangGraph 会把各分支的一项列表相加，而不是让后完成的分支覆盖先完成的结果。
    findings: Annotated[list[dict[str, Any]], add]
    result: dict[str, Any]


class EvidenceCheckState(TypedDict, total=False):
    """单项检查条件子图的状态，最多保存两轮检索和最终风险项摘要。"""

    review_run_id: UUID
    job_id: UUID
    context: dict[str, Any]
    check_item: dict[str, Any]
    check_code: str
    node_run_id: UUID
    initial_query: str
    retrieval_attempts: int
    contract_chunks: list[dict[str, Any]]
    policy_chunks: list[dict[str, Any]]
    initial_missing_sources: list[str]
    retried_sources: list[str]
    finding: dict[str, Any]


class ReviewModelProvider:
    """通过 LangChain 调用百炼兼容的聊天模型并解析为 Pydantic 对象。"""

    def __init__(self) -> None:
        if not settings.api_key:
            raise RiskReviewError(
                "REVIEW_MODEL_NOT_CONFIGURED",
                "未配置 api-key 环境变量，无法执行风险审查。",
            )
        self.parser = PydanticOutputParser(pydantic_object=ModelRiskDecision)
        self.client = ChatOpenAI(
            model=settings.review_model,
            api_key=SecretStr(settings.api_key),
            base_url=settings.dashscope_base_url,
            temperature=0,
            timeout=settings.model_timeout_seconds,
            max_retries=2,
        )

    def judge(
        self,
        check_item: dict[str, Any],
        contract_chunks: list[dict[str, Any]],
        policy_chunks: list[dict[str, Any]],
    ) -> tuple[ModelRiskDecision, dict[str, int | None]]:
        """根据受限证据生成一个检查项结论，不允许引用候选列表之外的内容。"""

        prompt = f"""
你是单企业演示项目中的合同风险审查助手。当前检查项是：{check_item['name']}。
检查要求：{check_item['prompt_template']}

必须遵守：
1. 只能使用下方合同证据和制度证据，不得使用外部知识或编造条款。
2. RISK 仅用于合同约定与制度存在明确冲突；PASS 仅用于证据足以证明符合要求。
3. 合同缺少相关约定、制度依据不足或语义无法确定时，返回 INSUFFICIENT_INFORMATION。
4. contract_refs 和 policy_refs 只能填写候选标签，例如 C1、P2；不得填写 UUID 或不存在的标签。
5. 风险等级只能是 LOW、MEDIUM、HIGH，建议应具体且简短。

合同证据：
{_format_candidates(contract_chunks, 'C')}

制度证据：
{_format_candidates(policy_chunks, 'P')}

{self.parser.get_format_instructions()}
""".strip()
        response = self.client.invoke(prompt)
        content = response.content
        if not isinstance(content, str):
            content = str(content)
        decision = self.parser.parse(content)
        usage = response.usage_metadata or {}
        return decision, {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }


@dataclass(slots=True)
class PolicyRerankResult:
    """保存合同或制度候选的重排结果，以及用于技术追踪的耗时。"""

    selected_hits: list[dict[str, Any]]
    all_hits: list[dict[str, Any]]
    latency_ms: int


class DashScopeRerankProvider:
    """调用百炼专用文本重排序接口，处理合同或制度候选。"""

    def __init__(
        self,
        client: httpx.Client | None = None,
        api_key: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.api_key
        if not self.api_key:
            raise RiskReviewError(
                "RERANK_MODEL_NOT_CONFIGURED",
                "未配置 api-key 环境变量，无法执行条款重排序。",
            )
        # httpx.Client 会复用 HTTPS 连接；同一个 Celery 任务的四个 LangGraph 分支可并发使用。
        self.client = client or httpx.Client(timeout=settings.rerank_timeout_seconds)

    def rerank(
        self,
        query: str,
        hits: list[dict[str, Any]],
        final_top_k: int,
        *,
        instruct: str = POLICY_RERANK_INSTRUCT,
    ) -> PolicyRerankResult:
        """对向量召回结果重新评分，返回按重排顺序筛出的上下文候选。"""

        if not hits:
            return PolicyRerankResult([], [], 0)
        documents = [_build_rerank_document(hit) for hit in hits]
        started_at = perf_counter()
        # qwen3-rerank 使用兼容 Rerank API，query/documents/top_n 位于请求体顶层。
        # 与已停用的 gte-rerank-v2 相比，其响应 results 也直接位于顶层。
        request_body = {
            "model": settings.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "instruct": instruct,
        }
        response = self.client.post(
            settings.rerank_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            # 请求返回全部候选的得分，便于持久化并对比向量排名与重排排名。
            json=request_body,
        )
        response.raise_for_status()
        latency_ms = round((perf_counter() - started_at) * 1000)
        payload = response.json()
        raw_results = payload.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            raise ValueError("重排序接口未返回有效 results。")

        candidates = [
            {
                **hit,
                "vector_rank_no": index,
                "rerank_rank_no": None,
                "rerank_score": None,
                "selected_for_context": False,
            }
            for index, hit in enumerate(hits, 1)
        ]
        ordered: list[dict[str, Any]] = []
        seen_indexes: set[int] = set()
        for rerank_position, item in enumerate(raw_results, 1):
            document_index = item.get("index")
            if (
                not isinstance(document_index, int)
                or document_index < 0
                or document_index >= len(candidates)
                or document_index in seen_indexes
            ):
                raise ValueError("重排序接口返回了无效的候选索引。")
            score = item.get("relevance_score")
            if not isinstance(score, (int, float)):
                raise ValueError("重排序接口返回了无效的相关性分数。")
            candidate = candidates[document_index]
            candidate["rerank_rank_no"] = rerank_position
            candidate["rerank_score"] = float(score)
            ordered.append(candidate)
            seen_indexes.add(document_index)

        # 服务若没有返回全部候选，未评分项仍保留向量原顺序，保证检索追踪记录完整。
        ordered.extend(
            candidate
            for index, candidate in enumerate(candidates)
            if index not in seen_indexes
        )
        selected = ordered[: max(1, min(final_top_k, len(ordered)))]
        for candidate in selected:
            candidate["selected_for_context"] = True
        return PolicyRerankResult(selected, candidates, latency_ms)


class RiskReviewService:
    """使用 LangGraph 并行执行四项 RAG 检查并汇总审批建议。"""

    def __init__(
        self,
        repository: RiskReviewRepository | None = None,
        embedding_provider: DashScopeEmbeddingProvider | None = None,
        model_provider: ReviewModelProvider | None = None,
        rerank_provider: DashScopeRerankProvider | None = None,
    ) -> None:
        self.repository = repository or RiskReviewRepository()
        self.embedding_provider = embedding_provider
        self.model_provider = model_provider
        self.rerank_provider = rerank_provider

    def create_review(self, contract_id: UUID) -> RiskReviewCreateResponse:
        if not settings.api_key:
            raise RiskReviewError(
                "REVIEW_MODEL_NOT_CONFIGURED",
                "未配置 api-key 环境变量，无法创建风险审查任务。",
            )
        task = self.repository.create_review(contract_id)
        try:
            # 延迟导入避免 Celery 在加载任务模块时和业务服务形成循环导入。
            from app.modules.risk_review.tasks import run_risk_review_task

            run_risk_review_task.apply_async(
                args=[str(task["review_run_id"]), str(task["job_id"])],
                task_id=task["celery_task_id"],
            )
        except Exception as exc:
            message = _safe_error_message(exc)
            self.repository.fail_review(task["review_run_id"], task["job_id"], message)
            raise RiskReviewError(
                "REVIEW_QUEUE_UNAVAILABLE",
                "风险审查任务投递失败，请确认 Redis 已启动。",
                503,
            ) from exc
        return RiskReviewCreateResponse.model_validate(task)

    def run(self, review_run_id: UUID, job_id: UUID) -> dict[str, Any]:
        """Celery Worker 中同步执行编译后的 LangGraph。"""

        self.repository.mark_running(review_run_id, job_id)
        # Provider 在 Worker 真正执行任务时创建，确保读取到 Worker 进程自己的 api-key。
        self.embedding_provider = self.embedding_provider or DashScopeEmbeddingProvider()
        self.model_provider = self.model_provider or ReviewModelProvider()
        self.rerank_provider = self.rerank_provider or DashScopeRerankProvider()

        graph = StateGraph(ReviewState)
        graph.add_node("load_context", self._load_context)
        for index, code in enumerate(REVIEW_CHECK_CODES, 1):
            graph.add_node(
                code.lower(),
                self._build_check_node(code, index),
            )
        graph.add_node("aggregate", self._aggregate)
        graph.add_edge(START, "load_context")
        check_nodes = [code.lower() for code in REVIEW_CHECK_CODES]
        # Fan-out（扇出）让四项检查在 load_context 完成后同时进入可执行状态。
        # 以节点列表作为起点的汇聚边会等待四个分支全部结束，之后才运行 aggregate。
        for node_name in check_nodes:
            graph.add_edge("load_context", node_name)
        graph.add_edge(check_nodes, "aggregate")
        graph.add_edge("aggregate", END)
        compiled = graph.compile()
        final_state = compiled.invoke(
            {"review_run_id": review_run_id, "job_id": job_id, "findings": []}
        )
        return final_state["result"]

    def _load_context(self, state: ReviewState) -> dict[str, Any]:
        context = self.repository.get_review_context(state["review_run_id"])
        node_run_id = self.repository.create_node_run(
            context["workflow_run_id"], "load_context", 0, {"review_run_id": str(state["review_run_id"])}
        )
        self.repository.finish_node(
            node_run_id,
            {
                "contract_id": str(context["contract_id"]),
                "contract_document_id": str(context["contract_document_id"]),
            },
        )
        self.repository.update_progress(state["job_id"], 5)
        return {"context": context}

    def _build_check_node(self, code: str, sequence_no: int):
        # 每个固定检查项内部使用同一张条件子图：首次证据充分时直接判断；不足时只补检一次。
        # 子图仍封装在主图的一个并行分支中，因此四项检查可以继续并发执行。
        check_graph = StateGraph(EvidenceCheckState)
        check_graph.add_node("retrieve_initial", self._retrieve_initial_evidence)
        check_graph.add_node("retrieve_retry", self._retrieve_missing_evidence)
        check_graph.add_node("judge", self._judge_with_evidence)
        check_graph.add_node("insufficient", self._finish_as_insufficient)
        check_graph.add_edge(START, "retrieve_initial")
        # add_conditional_edges 会读取节点完成后的最新状态，并选择唯一的下一条边。
        # 路由函数只检查可验证的候选集合，不使用模型自报置信度，也不会产生无限循环。
        check_graph.add_conditional_edges(
            "retrieve_initial",
            self._route_after_initial_retrieval,
            {"judge": "judge", "retry": "retrieve_retry"},
        )
        check_graph.add_conditional_edges(
            "retrieve_retry",
            self._route_after_retry,
            {"judge": "judge", "insufficient": "insufficient"},
        )
        check_graph.add_edge("judge", END)
        check_graph.add_edge("insufficient", END)
        compiled_check_graph = check_graph.compile()

        # LangGraph 的节点需要接收单个 state 参数，闭包把当前检查项代码和顺序绑定进节点函数。
        def review_check(state: ReviewState) -> dict[str, Any]:
            context = state["context"]
            check_item = self.repository.get_check_item(context["contract_id"], code)
            query = CHECK_QUERIES[code]
            node_run_id = self.repository.create_node_run(
                context["workflow_run_id"],
                code.lower(),
                sequence_no,
                {"check_code": code, "query": query, "max_retrieval_attempts": 2},
            )
            check_state = compiled_check_graph.invoke(
                {
                    "review_run_id": state["review_run_id"],
                    "job_id": state["job_id"],
                    "context": context,
                    "check_item": check_item,
                    "check_code": code,
                    "node_run_id": node_run_id,
                    "initial_query": query,
                    "retrieval_attempts": 0,
                    "contract_chunks": [],
                    "policy_chunks": [],
                    "initial_missing_sources": [],
                    "retried_sources": [],
                }
            )
            # 每个并行分支只返回自己的结果，由 ReviewState 上的 add reducer 安全合并。
            return {"findings": [check_state["finding"]]}

        return review_check

    def _retrieve_initial_evidence(
        self, state: EvidenceCheckState
    ) -> dict[str, Any]:
        """执行首次合同与制度检索，并保存条件路由所需的缺失来源。"""

        evidence = self._retrieve_evidence_sources(
            state,
            state["initial_query"],
            attempt=1,
            sources=("CONTRACT", "POLICY"),
        )
        missing_sources = _missing_evidence_sources(
            evidence["contract_chunks"], evidence["policy_chunks"]
        )
        return {
            **evidence,
            "retrieval_attempts": 1,
            "initial_missing_sources": missing_sources,
        }

    def _route_after_initial_retrieval(self, state: EvidenceCheckState) -> str:
        """首次证据完整时直接判断，否则进入唯一一次补检。"""

        return "retry" if state["initial_missing_sources"] else "judge"

    def _retrieve_missing_evidence(
        self, state: EvidenceCheckState
    ) -> dict[str, Any]:
        """仅对首次缺失的来源使用固定补充查询再检索一次。"""

        missing_sources = state["initial_missing_sources"]
        retry_query = _build_retry_query(
            state["check_code"], state["check_item"], missing_sources
        )
        evidence = self._retrieve_evidence_sources(
            state,
            retry_query,
            attempt=2,
            sources=tuple(missing_sources),
        )
        return {
            **evidence,
            "retrieval_attempts": 2,
            "retried_sources": list(missing_sources),
        }

    def _route_after_retry(self, state: EvidenceCheckState) -> str:
        """补检后仍缺少任一来源时结束为信息不足，不再继续循环。"""

        missing_sources = _missing_evidence_sources(
            state["contract_chunks"], state["policy_chunks"]
        )
        return "insufficient" if missing_sources else "judge"

    def _retrieve_evidence_sources(
        self,
        state: EvidenceCheckState,
        query: str,
        *,
        attempt: int,
        sources: tuple[str, ...],
    ) -> dict[str, list[dict[str, Any]]]:
        """检索指定证据来源；补检时不重复请求首次已经充分的一侧。"""

        context = state["context"]
        node_run_id = state["node_run_id"]
        query_vector = self.embedding_provider.embed_documents([query])[0]
        result: dict[str, list[dict[str, Any]]] = {}
        tracking_filters = {
            "attempt": attempt,
            "query_kind": "INITIAL" if attempt == 1 else "SUPPLEMENTAL",
        }

        if "CONTRACT" in sources:
            contract_candidates = self.repository.search_contract_chunks(
                context["contract_document_id"],
                query_vector,
                settings.contract_recall_top_k,
            )
            contract_rerank_strategy = "RERANK"
            contract_rerank_error = None
            contract_rerank_latency_ms = None
            contract_query_score = None
            contract_confidence = "REJECTED"
            try:
                contract_rerank_result = self.rerank_provider.rerank(
                    query,
                    contract_candidates,
                    settings.contract_final_top_k,
                    instruct=CONTRACT_RERANK_INSTRUCT,
                )
                contract_candidates = contract_rerank_result.all_hits
                contract_rerank_latency_ms = contract_rerank_result.latency_ms
                contract_query_score = _first_rerank_score(
                    contract_rerank_result.selected_hits
                )
                if (
                    contract_query_score is not None
                    and contract_query_score
                    >= settings.contract_rerank_query_min_score
                ):
                    contract_chunks = contract_rerank_result.selected_hits
                    contract_confidence = (
                        "LOW"
                        if contract_query_score
                        < settings.contract_rerank_low_confidence_score
                        else "NORMAL"
                    )
                else:
                    # 合同阈值是查询级门槛：第一名不可信时整组候选均不能进入模型上下文。
                    contract_chunks = []
                    _mark_selected_candidates(contract_candidates, [])
            except Exception as exc:
                # Rerank 不可用时保留原有向量检索能力，不让质量增强步骤中断整项审查。
                contract_rerank_strategy = "RERANK_FALLBACK"
                contract_rerank_error = _safe_error_message(exc)
                contract_candidates, contract_chunks = _build_vector_fallback(
                    contract_candidates, settings.contract_final_top_k
                )
                contract_confidence = "FALLBACK"
            self.repository.record_retrieval(
                node_run_id,
                query,
                {
                    **tracking_filters,
                    "document_id": str(context["contract_document_id"]),
                    "chunk_type": "CONTRACT_CLAUSE",
                    "query_min_score": settings.contract_rerank_query_min_score,
                    "low_confidence_score": settings.contract_rerank_low_confidence_score,
                    "query_score": contract_query_score,
                    "confidence_band": contract_confidence,
                },
                contract_candidates,
                settings.embedding_model,
                final_top_k=settings.contract_final_top_k,
                ranking_strategy=contract_rerank_strategy,
                rerank_model=settings.rerank_model,
                rerank_latency_ms=contract_rerank_latency_ms,
                rerank_error=contract_rerank_error,
            )
            result["contract_chunks"] = contract_chunks

        if "POLICY" in sources:
            policy_candidates = self.repository.search_policy_chunks(
                query_vector, settings.policy_recall_top_k
            )
            policy_rerank_strategy = "RERANK"
            policy_rerank_error = None
            policy_rerank_latency_ms = None
            try:
                policy_rerank_result = self.rerank_provider.rerank(
                    query,
                    policy_candidates,
                    settings.policy_final_top_k,
                    instruct=POLICY_RERANK_INSTRUCT,
                )
                policy_candidates = policy_rerank_result.all_hits
                policy_chunks = _filter_selected_candidates(
                    policy_rerank_result.selected_hits,
                    policy_candidates,
                    settings.policy_rerank_min_score,
                )
                policy_rerank_latency_ms = policy_rerank_result.latency_ms
            except Exception as exc:
                # 重排序是质量增强步骤，不应让整项风险审查失败；失败时使用向量排名前 5 条降级。
                policy_rerank_strategy = "RERANK_FALLBACK"
                policy_rerank_error = _safe_error_message(exc)
                policy_candidates, policy_chunks = _build_vector_fallback(
                    policy_candidates, settings.policy_final_top_k
                )
            self.repository.record_retrieval(
                node_run_id,
                query,
                {
                    **tracking_filters,
                    "document_type": "POLICY",
                    "is_current": True,
                    "chunk_type": "POLICY_SECTION",
                    "candidate_min_score": settings.policy_rerank_min_score,
                    "high_confidence_score": settings.rerank_high_confidence_score,
                },
                policy_candidates,
                settings.embedding_model,
                final_top_k=settings.policy_final_top_k,
                ranking_strategy=policy_rerank_strategy,
                rerank_model=settings.rerank_model,
                rerank_latency_ms=policy_rerank_latency_ms,
                rerank_error=policy_rerank_error,
            )
            result["policy_chunks"] = policy_chunks

        return result

    def _judge_with_evidence(self, state: EvidenceCheckState) -> dict[str, Any]:
        """证据完整时调用模型，并校验模型只能引用本次候选标签。"""

        check_item = state["check_item"]
        contract_chunks = state["contract_chunks"]
        policy_chunks = state["policy_chunks"]
        decision, usage = self.model_provider.judge(
            check_item, contract_chunks, policy_chunks
        )
        self.repository.record_llm_call(
            state["node_run_id"],
            settings.review_model,
            state["check_code"],
            decision.model_dump(),
            usage["input_tokens"],
            usage["output_tokens"],
        )

        selected_contract = _select_references(decision.contract_refs, contract_chunks, "C")
        selected_policy = _select_references(decision.policy_refs, policy_chunks, "P")
        # 风险或通过结论都必须同时有合同和制度证据，否则降级为信息不足。
        if decision.status != "INSUFFICIENT_INFORMATION" and (
            not selected_contract or not selected_policy
        ):
            decision = decision.model_copy(
                update={
                    "status": "INSUFFICIENT_INFORMATION",
                    "title": f"{check_item['name']}证据不足",
                    "description": "模型未能给出完整、有效的合同与制度引用，系统已阻止生成无依据结论。",
                    "suggestion": "请人工核对相关条款或补充制度依据后重新审查。",
                    "confidence": 0,
                }
            )
            selected_contract = contract_chunks[:1]
            selected_policy = policy_chunks[:1]

        finding = self._persist_check_result(
            state,
            decision,
            selected_contract,
            selected_policy,
            route="MODEL_JUDGMENT",
        )
        return {"finding": finding}

    def _finish_as_insufficient(
        self, state: EvidenceCheckState
    ) -> dict[str, Any]:
        """唯一一次补检仍失败时生成确定性信息不足结论，不调用聊天模型。"""

        contract_chunks = state["contract_chunks"]
        policy_chunks = state["policy_chunks"]
        missing_sources = _missing_evidence_sources(contract_chunks, policy_chunks)
        missing_label = _format_missing_evidence_sources(missing_sources)
        check_item = state["check_item"]
        decision = ModelRiskDecision(
            status="INSUFFICIENT_INFORMATION",
            severity=check_item["default_severity"],
            title=f"{check_item['name']}信息不足",
            description=f"首次检索和一次补检后仍缺少足够的{missing_label}，无法形成可靠结论。",
            suggestion=f"请补充或确认{missing_label}后重新审查。",
            confidence=0,
            contract_refs=["C1"] if contract_chunks else [],
            policy_refs=["P1"] if policy_chunks else [],
        )
        finding = self._persist_check_result(
            state,
            decision,
            contract_chunks[:1],
            policy_chunks[:1],
            route="INSUFFICIENT_INFORMATION",
        )
        return {"finding": finding}

    def _persist_check_result(
        self,
        state: EvidenceCheckState,
        decision: ModelRiskDecision,
        selected_contract: list[dict[str, Any]],
        selected_policy: list[dict[str, Any]],
        *,
        route: str,
    ) -> dict[str, Any]:
        """统一保存两条条件分支的业务结果和可追溯路由摘要。"""

        finding_id = self.repository.save_finding(
            state["review_run_id"],
            state["check_item"]["id"],
            decision,
            selected_contract,
            selected_policy,
        )
        final_missing_sources = _missing_evidence_sources(
            state["contract_chunks"], state["policy_chunks"]
        )
        self.repository.finish_node(
            state["node_run_id"],
            {
                "finding_id": str(finding_id),
                "status": decision.status,
                "severity": decision.severity,
                "route": route,
                "retrieval_attempts": state["retrieval_attempts"],
                "initial_missing_sources": state["initial_missing_sources"],
                "retried_sources": state["retried_sources"],
                "final_missing_sources": final_missing_sources,
                "contract_evidence_count": len(selected_contract),
                "policy_evidence_count": len(selected_policy),
            },
        )
        self.repository.update_check_progress(state["job_id"], state["review_run_id"])
        return {
            "finding_id": str(finding_id),
            "check_code": state["check_code"],
            "status": decision.status,
            "severity": decision.severity,
        }

    def _aggregate(self, state: ReviewState) -> dict[str, Any]:
        context = state["context"]
        node_run_id = self.repository.create_node_run(
            context["workflow_run_id"],
            "aggregate",
            5,
            {"finding_count": len(state.get("findings", []))},
        )
        result = self.repository.complete_review(state["review_run_id"], state["job_id"])
        self.repository.finish_node(node_run_id, result)
        return {"result": result}


def _missing_evidence_sources(
    contract_chunks: list[dict[str, Any]], policy_chunks: list[dict[str, Any]]
) -> list[str]:
    """按固定顺序返回缺失来源，作为条件边和可观测记录的稳定路由依据。"""

    missing_sources = []
    if not contract_chunks:
        missing_sources.append("CONTRACT")
    if not policy_chunks:
        missing_sources.append("POLICY")
    return missing_sources


def _build_retry_query(
    check_code: str,
    check_item: dict[str, Any],
    missing_sources: list[str],
) -> str:
    """使用固定同义词生成第二查询，并明确限制在缺失的证据来源内。"""

    source_labels = {
        "CONTRACT": "当前合同正文中的明确约定或相关条款",
        "POLICY": "当前有效企业制度中的限制、最低要求或审批条件",
    }
    source_scope = "；".join(source_labels[source] for source in missing_sources)
    return (
        f"{check_item['name']}补充检索：{CHECK_RETRY_QUERIES[check_code]}；"
        f"重点查找{source_scope}"
    )


def _format_missing_evidence_sources(missing_sources: list[str]) -> str:
    """把内部来源代码转换为可安全展示的中文说明。"""

    labels = {"CONTRACT": "合同相关条款", "POLICY": "制度依据"}
    return "和".join(labels[source] for source in missing_sources)


def _format_candidates(chunks: list[dict[str, Any]], prefix: str) -> str:
    if not chunks:
        return "（未检索到候选证据）"
    blocks = []
    for index, chunk in enumerate(chunks, 1):
        heading = " ".join(
            value for value in (chunk.get("clause_no"), chunk.get("title")) if value
        )
        blocks.append(
            f"[{prefix}{index}] 文档：{chunk['document_title']}；位置：{heading or '未标注'}\n{chunk['content']}"
        )
    return "\n\n".join(blocks)


def _build_rerank_document(chunk: dict[str, Any]) -> str:
    """把文档来源与条款正文组合为重排序模型可理解的单段文本。"""

    heading = " ".join(
        str(value)
        for value in (
            chunk.get("document_title"),
            chunk.get("clause_no"),
            chunk.get("title"),
        )
        if value
    )
    return f"{heading}\n{chunk['content']}" if heading else str(chunk["content"])


def _first_rerank_score(hits: list[dict[str, Any]]) -> float | None:
    """读取重排第一名分数；服务未评分时返回 None，避免把未知结果当成低分。"""

    if not hits:
        return None
    score = hits[0].get("rerank_score")
    return float(score) if isinstance(score, (int, float)) else None


def _mark_selected_candidates(
    candidates: list[dict[str, Any]], selected: list[dict[str, Any]]
) -> None:
    """同步上下文入选标记，使检索追踪与真正传给模型的候选保持一致。"""

    selected_ids = {candidate["id"] for candidate in selected}
    for candidate in candidates:
        candidate["selected_for_context"] = candidate["id"] in selected_ids


def _filter_selected_candidates(
    selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    min_score: float,
) -> list[dict[str, Any]]:
    """按 Rerank 阈值过滤最终候选，但仍保留全部召回轨迹用于审计。"""

    filtered = [
        candidate
        for candidate in selected
        if isinstance(candidate.get("rerank_score"), (int, float))
        and candidate["rerank_score"] >= min_score
    ]
    _mark_selected_candidates(candidates, filtered)
    return filtered


def _build_vector_fallback(
    hits: list[dict[str, Any]], final_top_k: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rerank 失败时恢复向量顺序，并明确标记真正进入上下文的前 K 条。"""

    candidates = [
        {
            **hit,
            "vector_rank_no": index,
            "rerank_rank_no": None,
            "rerank_score": None,
            "selected_for_context": index <= final_top_k,
        }
        for index, hit in enumerate(hits, 1)
    ]
    return candidates, candidates[:final_top_k]


def _select_references(
    references: list[str], chunks: list[dict[str, Any]], prefix: str
) -> list[dict[str, Any]]:
    """只接受后端实际提供的候选标签，自动忽略模型编造或重复的标签。"""

    mapping = {f"{prefix}{index}": chunk for index, chunk in enumerate(chunks, 1)}
    selected = []
    seen = set()
    for reference in references:
        normalized = reference.strip().upper()
        if normalized in mapping and normalized not in seen:
            selected.append(mapping[normalized])
            seen.add(normalized)
    return selected


def run_review_safely(review_run_id: UUID, job_id: UUID) -> dict[str, Any]:
    """任务入口统一脱敏异常并把失败状态写回 PostgreSQL。"""

    repository = RiskReviewRepository()
    try:
        return RiskReviewService(repository=repository).run(review_run_id, job_id)
    except Exception as exc:
        message = _safe_error_message(exc)
        repository.fail_review(review_run_id, job_id, message)
        raise
