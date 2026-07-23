from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


# frozen=True 防止运行过程中意外修改配置，slots=True 减少这类纯配置对象的额外属性。
@dataclass(frozen=True, slots=True)
class Settings:
    """从环境变量读取的应用配置；未设置时使用本地演示默认值。"""

    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://approval_user:L123456@localhost:5432/approval_assistant",
    )
    upload_dir: Path = Path(
        os.getenv("UPLOAD_DIR", str(PROJECT_ROOT / "storage" / "uploads"))
    )
    max_upload_size: int = int(os.getenv("MAX_UPLOAD_SIZE", str(20 * 1024 * 1024)))
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    celery_result_backend: str = os.getenv(
        "CELERY_RESULT_BACKEND", "redis://localhost:6379/1"
    )
    # API Key 可能被 Settings 打印或调试，repr=False 可避免它出现在对象字符串中。
    api_key: str | None = field(default=os.getenv("api-key"), repr=False)
    dashscope_base_url: str = os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    # 模型和维度与数据库 VECTOR(1536) 结构绑定，演示项目中不开放环境变量覆盖。
    embedding_model: str = "text-embedding-v4"
    embedding_dimension: int = 1536
    embedding_batch_size: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
    # 外部模型请求必须早于问答 PENDING 的 10 分钟回收窗口结束，
    # 避免供应商连接挂起后持续占用 FastAPI 线程池。
    model_timeout_seconds: float = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    # 风险审查复用同一个百炼兼容接口和 api-key，只单独配置生成式模型名称。
    review_model: str = os.getenv("REVIEW_MODEL", "qwen-plus")
    # 合同和制度先扩大向量召回，再通过专用重排序模型筛选进入 LLM 上下文的候选。
    # 重排序接口不是 OpenAI 兼容接口，因此使用独立的服务地址。
    rerank_model: str = os.getenv("RERANK_MODEL", "qwen3-rerank")
    rerank_url: str = os.getenv(
        "RERANK_URL",
        "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
    )
    rerank_timeout_seconds: float = float(os.getenv("RERANK_TIMEOUT_SECONDS", "20"))
    # 合同侧使用较宽的 Top 20 保证长合同召回率，再重排取 Top 5。
    # 第一名低于查询级门槛时，表示当前风险项没有可靠合同证据。
    contract_recall_top_k: int = int(os.getenv("CONTRACT_RECALL_TOP_K", "20"))
    contract_final_top_k: int = int(os.getenv("CONTRACT_FINAL_TOP_K", "5"))
    contract_rerank_query_min_score: float = float(
        os.getenv("CONTRACT_RERANK_QUERY_MIN_SCORE", "0.45")
    )
    contract_rerank_low_confidence_score: float = float(
        os.getenv("CONTRACT_RERANK_LOW_CONFIDENCE_SCORE", "0.55")
    )
    policy_recall_top_k: int = int(os.getenv("POLICY_RECALL_TOP_K", "10"))
    policy_final_top_k: int = int(os.getenv("POLICY_FINAL_TOP_K", "5"))
    # 制度阈值逐条作用于重排后的 Top 5；高置信度值只用于追踪，不改变结论。
    policy_rerank_min_score: float = float(
        os.getenv("POLICY_RERANK_MIN_SCORE", "0.60")
    )
    rerank_high_confidence_score: float = float(
        os.getenv("RERANK_HIGH_CONFIDENCE_SCORE", "0.70")
    )
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,"
            "http://localhost:4173,http://127.0.0.1:4173",
        ).split(",")
        if origin.strip()
    )


settings = Settings()
