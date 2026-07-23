from __future__ import annotations

import secrets
import sys
from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parent
EXAMPLE_PATH = DEPLOY_DIR / ".env.example"
TARGET_PATH = DEPLOY_DIR / ".env"


def set_api_key_from_stdin() -> None:
    """从标准输入接收 API Key，避免密钥出现在命令参数和终端历史中。"""

    api_key = sys.stdin.read().strip()
    if not api_key:
        raise ValueError("标准输入中没有可写入的 API Key。")
    if not TARGET_PATH.exists():
        raise FileNotFoundError("deploy/.env 不存在，请先生成服务器环境文件。")

    lines = TARGET_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("api-key="):
            lines[index] = f"api-key={api_key}"
            replaced = True
            break
    if not replaced:
        raise ValueError("deploy/.env 中缺少 api-key 配置项。")

    TARGET_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    TARGET_PATH.chmod(0o600)
    print("已安全写入 api-key，未显示密钥内容。")


def main() -> None:
    """首次部署时生成仅保存在服务器上的环境变量文件。"""

    if TARGET_PATH.exists():
        print("deploy/.env 已存在，为避免覆盖真实配置，本次未修改。")
        return

    # 数据库密码在服务器本地随机生成，不输出到终端，也不经过 Git 或部署日志。
    database_password = secrets.token_hex(24)
    content = EXAMPLE_PATH.read_text(encoding="utf-8")
    content = content.replace(
        "replace-with-a-strong-random-password",
        database_password,
    )
    # API Key 必须由部署人员在服务器上填写，脚本不会读取或生成外部服务密钥。
    content = content.replace("api-key=replace-with-your-api-key", "api-key=")
    TARGET_PATH.write_text(content, encoding="utf-8")
    TARGET_PATH.chmod(0o600)
    print("已生成 deploy/.env，并设置为仅当前用户可读写。")


if __name__ == "__main__":
    if "--set-api-key-stdin" in sys.argv:
        set_api_key_from_stdin()
    else:
        main()
