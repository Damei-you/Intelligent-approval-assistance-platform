# 智能审批辅助平台

基于 FastAPI、LangChain、LangGraph、PostgreSQL/pgvector、Redis 和 Celery 的演示项目。

当前仓库已提供 PostgreSQL/pgvector 容器及数据库初始化脚本。数据库包含合同知识库、风险检查、两级审批、合同问答、LangGraph 可观测记录和 Celery 任务记录等表。

风险审查智能体已实现付款、质保、违约责任和争议解决四项并行 RAG 检查。审查通过 Celery 异步执行 LangGraph 四分支工作流，每项结论均保存合同条款与制度依据，四项结束后统一汇总，前端提供进度、汇总和证据对照展示。

辅助审批已实现固定两级人工流程：业务审批通过后进入法务审批，两级均通过后合同批准；
任一级可退回修改或驳回。审批页面会展示风险审查摘要和 AI 建议，但最终决定、操作人和
意见均由人工提交并持久化保存。

## 启动 PostgreSQL 与 Redis

前置条件：Docker Desktop 已启动。

```powershell
docker compose up -d postgres redis
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

已有数据库启用风险审查四项检查时执行：

```powershell
docker compose up -d postgres
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/003_risk_review_agent.sql
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/004_approval_unique_review.sql
```

完整表结构说明见 [docs/database-design.md](docs/database-design.md)。

## 风险审查评测数据

`examples/evaluation` 提供两版虚构制度、12类合同测试场景、预期风险结论和重排序分级标注。数据覆盖明确风险、明确合规、信息不足、同义表达、困难负样本、跨合同隔离和制度版本过滤，可以直接通过 JSON 导入接口使用。`examples/evaluation/stress` 另提供一份 50 条合同与 100 条制度的重排序压力集。执行顺序和指标说明见 [examples/evaluation/README.md](examples/evaluation/README.md)。

可使用 `tools/evaluate_rag.py` 自动完成压力集离线校验、真实向量检索指标统计和风险审查
端到端对比，并输出 JSON 与 Markdown 报告。使用说明见
[docs/rag-evaluation.md](docs/rag-evaluation.md)。

## 合同与制度依据导入接口

当前已支持 PDF、TXT 和 JSON 合同、制度依据导入。文件会先解析成标准 JSON 供用户检查修改，确认后才保存原始文档及条款/章节分块，并创建异步向量化任务。向量模型固定为 `text-embedding-v4`，输出维度固定为 1536。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

另开一个 PowerShell 窗口启动 Celery Worker。API Key 必须同时存在于后端和 Worker 的环境中：

```powershell
.\.venv\Scripts\python.exe -m celery -A app.core.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

`--pool=solo` 适合 Windows 本地演示。API Key 只读取现有的 `api-key` 环境变量，不需要再设置其他同义变量，也不要把真实值写入 `.env.example` 或提交到 GitHub。未配置 API Key 时合同仍会正常导入，响应会明确标记 `NOT_CONFIGURED`，且不会创建任务。

Swagger UI：`http://127.0.0.1:8000/docs`

接口规范和请求示例见 [docs/contract-import-api.md](docs/contract-import-api.md)。

## Vue 展示页面

前端位于 `frontend`，用于演示 PDF/TXT/JSON 合同与制度依据导入、人工确认、向量化进度，以及合同风险审查报告。

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
