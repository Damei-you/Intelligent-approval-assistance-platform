from __future__ import annotations

import os
from dataclasses import dataclass
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
