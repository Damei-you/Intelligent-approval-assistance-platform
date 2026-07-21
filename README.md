# 智能审批辅助平台

基于 FastAPI、LangChain、LangGraph、PostgreSQL/pgvector、Redis 和 Celery 的演示项目。

当前仓库已提供 PostgreSQL/pgvector 容器及数据库初始化脚本。数据库包含合同知识库、风险检查、两级审批、合同问答、LangGraph 可观测记录和 Celery 任务记录等表。

## 启动 PostgreSQL

前置条件：Docker Desktop 已启动。

```powershell
docker compose up -d postgres
docker compose ps
```

默认连接信息：

| 配置 | 默认值 |
|---|---|
| Host | `localhost` |
| Port | `5432` |
| Database | `approval_assistant` |
| Username | `approval_user` |
| Password | `L123456` |

应用连接串：

```text
postgresql+psycopg://approval_user:L123456@localhost:5432/approval_assistant
```

这些值可以通过 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD` 和 `POSTGRES_PORT` 环境变量覆盖。

## 验证数据库

```powershell
docker compose exec postgres psql -U approval_user -d approval_assistant -c "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pgcrypto');"
docker compose exec postgres psql -U approval_user -d approval_assistant -c "SELECT COUNT(*) AS table_count FROM information_schema.tables WHERE table_schema = 'public';"
docker compose exec postgres psql -U approval_user -d approval_assistant -c "SELECT code, name FROM contract_types ORDER BY code;"
```

## 初始化行为

容器首次创建数据卷时，会按文件名顺序执行：

1. `database/init/001_schema.sql`：扩展、表、索引、约束和触发器。
2. `database/init/002_seed.sql`：合同类型与默认风险检查项。

PostgreSQL 官方镜像只会在空数据目录上运行初始化脚本。后续修改 SQL 时应通过迁移工具升级已有数据库；开发阶段如果明确要销毁现有演示数据，可以删除 Compose 数据卷后重新创建。

完整表结构说明见 [docs/database-design.md](docs/database-design.md)。

## 合同与条款导入接口

当前已支持 PDF、TXT 和 JSON 合同导入。文件会先解析成标准 JSON 供用户检查修改，确认后才保存合同、原始文档和条款分块；暂不生成向量。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Swagger UI：`http://127.0.0.1:8000/docs`

接口规范和请求示例见 [docs/contract-import-api.md](docs/contract-import-api.md)。

## Vue 展示页面

前端位于 `frontend`，用于演示 PDF/TXT/JSON 合同导入、解析进度和导入结果。

先启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

再启动前端：

```powershell
Set-Location frontend
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。开发服务器会把 `/api` 和 `/health` 请求代理到 `http://127.0.0.1:8000`。
