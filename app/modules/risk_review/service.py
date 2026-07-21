from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict
from uuid import UUID

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


class ReviewState(TypedDict, total=False):
    """LangGraph 节点间传递的小型状态，只保存标识和结果摘要。"""

    review_run_id: UUID
    job_id: UUID
    context: dict[str, Any]
    # 四个并行节点都会写 findings。Annotated 指定 add reducer（归并函数），
    # LangGraph 会把各分支的一项列表相加，而不是让后完成的分支覆盖先完成的结果。
    findings: Annotated[list[dict[str, Any]], add]
    result: dict[str, Any]


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


class RiskReviewService:
    """使用 LangGraph 并行执行四项 RAG 检查并汇总审批建议。"""

    def __init__(
        self,
        repository: RiskReviewRepository | None = None,
        embedding_provider: DashScopeEmbeddingProvider | None = None,
        model_provider: ReviewModelProvider | None = None,
    ) -> None:
        self.repository = repository or RiskReviewRepository()
        self.embedding_provider = embedding_provider
        self.model_provider = model_provider

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
        # LangGraph 的节点需要接收单个 state 参数，闭包把当前检查项代码绑定进节点函数。
        def review_check(state: ReviewState) -> dict[str, Any]:
            finding = self._review_check(state, code, sequence_no)
            # 每个并行分支只返回自己的结果，由 ReviewState 上的 add reducer 安全合并。
            return {"findings": [finding]}

        return review_check

    def _review_check(
        self, state: ReviewState, code: str, sequence_no: int
    ) -> dict[str, Any]:
        context = state["context"]
        check_item = self.repository.get_check_item(context["contract_id"], code)
        query = CHECK_QUERIES[code]
        node_run_id = self.repository.create_node_run(
            context["workflow_run_id"],
            code.lower(),
            sequence_no,
            {"check_code": code, "query": query},
        )
        query_vector = self.embedding_provider.embed_documents([query])[0]
        contract_chunks = self.repository.search_contract_chunks(
            context["contract_document_id"], query_vector, 3
        )
        policy_chunks = self.repository.search_policy_chunks(query_vector, 5)
        self.repository.record_retrieval(
            node_run_id,
            query,
            {"document_id": str(context["contract_document_id"]), "chunk_type": "CONTRACT_CLAUSE"},
            contract_chunks,
            settings.embedding_model,
        )
        self.repository.record_retrieval(
            node_run_id,
            query,
            {"document_type": "POLICY", "is_current": True, "chunk_type": "POLICY_SECTION"},
            policy_chunks,
            settings.embedding_model,
        )

        if not contract_chunks or not policy_chunks:
            missing = "合同相关条款" if not contract_chunks else "制度依据"
            decision = ModelRiskDecision(
                status="INSUFFICIENT_INFORMATION",
                severity=check_item["default_severity"],
                title=f"{check_item['name']}信息不足",
                description=f"未检索到足够的{missing}，无法形成可靠结论。",
                suggestion=f"请补充或确认{missing}后重新审查。",
                confidence=0,
                contract_refs=["C1"] if contract_chunks else [],
                policy_refs=["P1"] if policy_chunks else [],
            )
        else:
            decision, usage = self.model_provider.judge(
                check_item, contract_chunks, policy_chunks
            )
            self.repository.record_llm_call(
                node_run_id,
                settings.review_model,
                code,
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

        finding_id = self.repository.save_finding(
            state["review_run_id"],
            check_item["id"],
            decision,
            selected_contract,
            selected_policy,
        )
        self.repository.finish_node(
            node_run_id,
            {
                "finding_id": str(finding_id),
                "status": decision.status,
                "severity": decision.severity,
                "contract_evidence_count": len(selected_contract),
                "policy_evidence_count": len(selected_policy),
            },
        )
        self.repository.update_check_progress(state["job_id"], state["review_run_id"])
        return {
            "finding_id": str(finding_id),
            "check_code": code,
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
