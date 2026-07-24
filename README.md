# 智能审批辅助平台

基于 FastAPI、LangChain、LangGraph、PostgreSQL/pgvector、Redis 和 Celery 的演示项目。

> [!IMPORTANT]
> 本项目及下方截图中的合同、企业制度、主体名称、金额和审批记录均为虚构演示数据，仅用于功能展示。请勿上传、录入或提交任何真实合同、内部制度及其他敏感数据。

当前仓库已提供 PostgreSQL/pgvector 容器及数据库初始化脚本。数据库包含合同知识库、风险检查、两级审批、合同问答、LangGraph 可观测记录和 Celery 任务记录等表。

风险审查智能体已实现付款、质保、违约责任和争议解决四项并行 RAG 检查。合同侧向量
召回 Top 20、制度侧向量召回 Top 10，两侧经 `qwen3-rerank` 重排序后取 Top 5；合同使用
查询级门槛识别整类条款缺失，制度使用候选阈值排除低相关依据。审查通过
Celery 异步执行 LangGraph 四分支工作流；每项检查在证据不足时使用固定补充查询，仅对
缺失来源补检一次，仍不足才生成信息不足结论。每项结论均保存合同条款、制度依据、检索
轮次和重排轨迹，四项结束后统一汇总，前端提供进度、汇总和证据对照展示。
审查页面还提供可展开的 Agent 执行轨迹，以同一时间轴展示上下文加载、四分支并行、
证据补检、Rerank 降级、模型耗时与 Token，并对 Prompt、合同全文和内部状态进行脱敏。

辅助审批已实现固定两级人工流程：业务审批通过后进入法务审批，两级均通过后合同批准；
任一级可退回修改或驳回。审批页面会展示风险审查摘要和 AI 建议，但最终决定、操作人和
意见均由人工提交并持久化保存。

风险项多轮问答已实现第一阶段闭环：可以解释已有风险结论、查询合同条款与制度依据，或
根据风险建议生成不会写回合同正文的条款修改草案。会话固定绑定风险项、审查任务和合同修订版本，
每轮使用最近 10 条成功历史消息及其草案、引用快照，并保存模型回答、真实分块引用和
LangGraph 运行记录。前端会自动恢复生成中的回答，后端会拦截伪造引用和错误草案目标。
外部模型请求默认 60 秒超时，遗留超过 10 分钟的生成占位会自动转为失败并允许重试。

## 界面预览

### 合同与制度知识导入

支持合同、制度依据两类知识导入。文件解析后先展示标准 JSON，用户确认后才写入数据库并创建异步向量化任务。

![合同与制度知识导入界面](docs/images/contract-import.png)

### 四项风险审查

针对付款、质保、违约责任和争议解决执行并行检查，集中展示风险等级、修改建议及合同与制度依据。

![四项风险审查与审查汇总界面](docs/images/risk-review.png)

### 可追溯证据检索

风险结论保留合同候选、制度召回与重排序结果，并标记最终采用的合同条款和制度依据。

![合同条款与制度依据检索详情](docs/images/risk-evidence.png)

### 绑定风险项的多轮问答

可以在当前风险项范围内解释风险结论、查询引用依据并生成需要人工确认的条款修改草案。

![风险项多轮问答与条款修改草案](docs/images/risk-chat.png)

### 固定两级人工审批

业务审批完成后进入法务审批；审批人可以通过、退回修改或驳回，AI 建议仅作为人工决策的辅助信息。

![业务与法务两级人工审批界面](docs/images/manual-approval.png)

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
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/005_policy_reranking.sql
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/006_contract_chat.sql
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

前端位于 `frontend`，用于演示 PDF/TXT/JSON 合同与制度依据导入、人工确认、向量化进度、合同风险审查报告、风险项多轮追问与两级审批。

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

## 生产部署

Ubuntu 服务器可使用 `compose.prod.yaml` 同时运行 PostgreSQL、Redis、FastAPI、Celery Worker 和 Vue/Nginx。生产环境变量、启动、更新与日志检查方法见 [远程服务器部署说明](docs/server-deployment.md)。
