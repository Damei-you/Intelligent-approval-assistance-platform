#!/usr/bin/env sh
set -eu

# 统一从项目根目录执行 Compose，确保构建上下文和环境文件使用正确路径。
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

exec docker compose \
    --env-file deploy/.env \
    -f compose.prod.yaml \
    up -d --build
