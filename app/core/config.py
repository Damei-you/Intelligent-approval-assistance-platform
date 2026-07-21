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
