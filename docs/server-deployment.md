# 远程服务器部署

本文档说明如何在 Ubuntu 服务器上使用 Docker Compose 部署智能审批辅助平台。

后端镜像默认使用腾讯云 PyPI 软件源加速依赖下载；其他网络环境可通过 Docker 的 `PIP_INDEX_URL` 构建参数覆盖。

## 服务结构

- 宿主机 Nginx：负责 `contract.zhigu.site` 的 HTTPS 和反向代理。
- `web`：提供 Vue 静态资源，并将 `/api`、`/health` 和 API 文档转发给后端。
- `backend`：运行 FastAPI。
- `worker`：运行 Celery Worker，执行向量化、风险审查和合同问答任务。
- `postgres`：运行 PostgreSQL 17 和 pgvector，不向公网暴露端口。
- `redis`：运行 Celery Broker 和结果后端，不向公网暴露端口。

## 准备环境变量

在服务器项目目录执行：

```bash
python3 deploy/prepare_server_env.py
```

脚本会在服务器本地生成随机数据库密码，并同时写入 `POSTGRES_PASSWORD` 和 `DATABASE_URL`。随后只需填写 `api-key`；真实密钥不得提交到 Git。

## 启动与检查

```bash
docker compose --env-file deploy/.env -f compose.prod.yaml up -d --build
docker compose --env-file deploy/.env -f compose.prod.yaml ps
curl http://127.0.0.1:8080/health
```

首次创建 PostgreSQL 数据卷时，`database/init` 中的初始化 SQL 会自动执行。已有数据卷不会重复执行初始化脚本。

## 更新应用

更新源码后重新构建应用容器：

```bash
docker compose --env-file deploy/.env -f compose.prod.yaml up -d --build backend worker web
```

上传文件、PostgreSQL 和 Redis 分别保存在 Docker 命名卷中，重新构建容器不会删除这些数据。

## 查看日志

```bash
docker compose --env-file deploy/.env -f compose.prod.yaml logs --tail=200 backend
docker compose --env-file deploy/.env -f compose.prod.yaml logs --tail=200 worker
docker compose --env-file deploy/.env -f compose.prod.yaml logs --tail=200 web
```

Compose 已限制每个容器的本地 JSON 日志为最多 3 个、每个 10 MB，避免长期运行占满服务器磁盘。
