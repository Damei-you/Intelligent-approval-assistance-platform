# 合同与制度依据导入接口

## 1. 模块边界

导入模块负责：

1. 校验 PDF、TXT、JSON 格式和文件大小。
2. 提取 PDF/TXT 文本或校验结构化 JSON。
3. 返回标准 JSON 预览，此时不保存文件、不写数据库。
4. 用户在前端检查或修改 JSON。
5. 用户确认后保存原文件，并写入 `contracts`、`documents` 和 `document_chunks`。
6. 导入事务提交后，通过 Redis/Celery 异步生成条款向量并写入 pgvector。

制度依据采用同样的“预览 - 人工确认 - 入库 - 异步向量化”流程。制度主文档写入 `documents`，其中 `document_type=POLICY`；章节写入 `document_chunks`，其中 `chunk_type=POLICY_SECTION`。制度编号、版本、发布部门和生效日期保存在文档 `metadata` 中。

向量模型为 `text-embedding-v4`，维度固定为 1536，API Key 只从 `api-key` 环境变量读取。预览阶段不调用模型；确认导入成功后才创建向量化任务。向量化失败不会回滚已经导入的合同。

默认单文件上限为 20 MB，可以通过 `MAX_UPLOAD_SIZE` 调整。PDF 仅支持包含文本层的文件，扫描版 PDF 暂不进行 OCR。

## 2. 接口总览

| 方法 | 路径 | Content-Type | 用途 |
|---|---|---|---|
| POST | `/api/v1/contracts/imports/preview/file` | `multipart/form-data` | 解析 PDF、TXT 或 JSON，返回待确认 JSON |
| POST | `/api/v1/contracts/imports/confirm/file` | `multipart/form-data` | 提交确认后的 JSON 并正式入库 |
| POST | `/api/v1/contracts/imports/json` | `application/json` | 直接提交结构化条款 |
| DELETE | `/api/v1/contracts/imports/demo` | - | 清理固定 50 条款示例合同及全部关联数据 |
| GET | `/api/v1/contracts/imports/{document_id}` | - | 查询导入记录及向量化数量 |
| GET | `/api/v1/contracts/imports/{document_id}/vectorization` | - | 查询向量化任务状态和进度 |
| POST | `/api/v1/policies/imports/preview/file` | `multipart/form-data` | 解析制度文件并返回待确认 JSON |
| POST | `/api/v1/policies/imports/confirm/file` | `multipart/form-data` | 确认制度 JSON 并正式入库 |
| POST | `/api/v1/policies/imports/json` | `application/json` | 直接提交结构化制度章节 |
| GET | `/api/v1/policies/imports/{document_id}` | - | 查询制度导入详情 |
| GET | `/api/v1/policies/imports/{document_id}/vectorization` | - | 查询制度向量化进度 |
| GET | `/api/v1/risk-reviews/contracts` | - | 查询可选择的当前合同版本 |
| POST | `/api/v1/risk-reviews` | `application/json` | 创建四项异步风险审查 |
| GET | `/api/v1/risk-reviews/{review_run_id}` | - | 查询审查进度、结论和证据 |
| POST | `/api/v1/risk-findings/{finding_id}/chat-sessions` | - | 为风险项创建或恢复问答会话 |
| GET | `/api/v1/chat-sessions/{session_id}` | - | 查询会话、历史消息和回答引用 |
| POST | `/api/v1/chat-sessions/{session_id}/messages` | `application/json` | 继续询问风险并生成回答或条款草案 |

解析预览使用 HTTP `200 OK`，且 `persisted` 固定为 `false`。确认导入使用 HTTP `201 Created`。重复导入同一个 `contract_no` 时，不创建新合同，而是创建新的合同文档修订版本，并将该版本设为当前版本。

## 3. PDF/TXT 文件解析与确认

```http
POST /api/v1/contracts/imports/preview/file
Content-Type: multipart/form-data
```

### 表单字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | `.pdf` 或 `.txt` 文件 |
| `contract_no` | 是 | 合同编号 |
| `name` | 是 | 合同名称 |
| `contract_type_code` | 是 | `PURCHASE` 或 `SALES` |
| `counterparty` | 否 | 合同相对方 |
| `amount` | 否 | 合同金额，必须大于等于 0 |
| `currency` | 否 | 币种，默认 `CNY` |
| `document_title` | 否 | 文档标题，默认使用合同名称 |

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/contracts/imports/preview/file" \
  -F "file=@examples/contract-import.txt" \
  -F "contract_no=CG-2026-001" \
  -F "name=办公设备采购合同" \
  -F "contract_type_code=PURCHASE" \
  -F "counterparty=示例科技有限公司" \
  -F "amount=120000"
```

预览响应中的 `payload` 是可以直接展示和编辑的标准合同 JSON，`preview_hash` 用于保证确认时提交的原始文件与预览文件一致。预览阶段不会创建任何业务记录。

确认时再次提交原始文件、`preview_hash` 和用户确认后的 `payload`：

```http
POST /api/v1/contracts/imports/confirm/file
Content-Type: multipart/form-data
```

| 字段 | 说明 |
|---|---|
| `file` | 与预览阶段相同的原始文件 |
| `preview_hash` | 预览接口返回的 SHA-256 |
| `payload` | 用户确认或修改后的完整 JSON 字符串 |

如果文件哈希不一致，接口返回 HTTP `409 PREVIEW_FILE_MISMATCH`，要求重新解析。

TXT 支持 UTF-8、UTF-8 with BOM 和 GB18030 编码。系统优先按“第一条”“第二条”或“1.”“2.”等标题切分；没有明显条款标题时，按段落和最大字符数形成分块。

PDF 按页提取文本并记录 `page_no`。跨页条款在当前轻量解析方案下可能拆成两个分块，后续可以替换为专业文档解析器而不改变接口。

## 4. JSON 条款导入

### 请求

```http
POST /api/v1/contracts/imports/json
Content-Type: application/json
```

```json
{
  "contract_no": "CG-2026-001",
  "name": "办公设备采购合同",
  "contract_type_code": "PURCHASE",
  "counterparty": "示例科技有限公司",
  "amount": 120000,
  "currency": "CNY",
  "document_title": "办公设备采购合同正文",
  "clauses": [
    {
      "clause_no": "第一条",
      "title": "合同标的",
      "content": "供应方按照采购清单向采购方提供办公设备。",
      "page_no": 1,
      "metadata": {}
    }
  ],
  "metadata": {
    "source": "demo"
  }
}
```

`clauses` 至少包含一项，每项的 `content` 必填。也可以把同样结构保存为 `.json` 文件后上传至 `/preview/file`，确认预览内容后再调用 `/confirm/file`；JSON 文件本身已经包含合同元数据，因此不要求额外的表单字段。

### 成功响应

```json
{
  "contract_id": "6184acc5-3171-468a-95a7-f0ad4e0f24a4",
  "document_id": "0478e113-fd81-4908-8131-b36fdcd3537a",
  "contract_no": "CG-2026-001",
  "revision_no": 1,
  "import_format": "JSON",
  "parse_status": "PARSED",
  "clause_count": 1,
  "vectorized": false,
  "vectorization_job_id": "4c5d5dd3-073d-4b5c-9ad1-280fd70c09d6",
  "vectorization_status": "QUEUED",
  "message": "合同及条款导入成功，向量化任务已进入队列。"
}
```

`vectorized` 表示同步响应产生时是否已完成向量化，因此当前固定为 `false`。后续状态通过向量化进度接口查询。未配置 `api-key` 时，`vectorization_job_id` 为 `null`，状态为 `NOT_CONFIGURED`。

## 5. 清理固定示例合同

```http
DELETE /api/v1/contracts/imports/demo
```

此接口仅用于公开演示环境，删除目标在后端固定为 `EVAL-STRESS-001`，不接受合同编号参数，
因此不能通过该接口删除其他合同。清理操作在一个数据库事务中完成，包含：

- 合同主记录及所有修订文档；
- 合同条款和 1536 维向量；
- 风险审查、风险项、引用依据和 LangGraph 执行轨迹；
- 合同风险问答、消息、引用和条款草案；
- 业务、法务审批实例及审批步骤；
- 文档向量化与风险审查的 `async_jobs` 记录。

接口是幂等的：合同不存在时仍返回 HTTP `200 OK`，但 `deleted` 为 `false`。成功清理示例：

```json
{
  "contract_no": "EVAL-STRESS-001",
  "deleted": true,
  "deleted_documents": 1,
  "deleted_clauses": 50,
  "deleted_reviews": 1,
  "deleted_approvals": 1,
  "deleted_chat_sessions": 1,
  "deleted_async_jobs": 2,
  "message": "示例合同及其文档、条款、风险审查、审批、问答、工作流轨迹和异步任务记录已全部清理。"
}
```

前端必须在调用前展示明确的永久删除确认。该接口不提供任意合同删除能力，也不应扩展为传入
`contract_no` 的通用删除接口。

## 6. 查询导入结果

```http
GET /api/v1/contracts/imports/{document_id}
```

响应中的关键字段：

| 字段 | 说明 |
|---|---|
| `clause_count` | 本文档的条款/分块数量 |
| `vectorized_clause_count` | 已写入 Embedding 的分块数 |
| `revision_no` | 当前导入生成的合同文档修订号 |
| `is_current` | 是否为该合同的当前版本 |

## 7. 查询向量化进度

```http
GET /api/v1/contracts/imports/{document_id}/vectorization
```

```json
{
  "document_id": "0478e113-fd81-4908-8131-b36fdcd3537a",
  "job_id": "4c5d5dd3-073d-4b5c-9ad1-280fd70c09d6",
  "status": "RUNNING",
  "progress": 50,
  "clause_count": 2,
  "vectorized_clause_count": 1,
  "model_name": "text-embedding-v4",
  "dimension": 1536,
  "error_message": null
}
```

状态包括 `NOT_CONFIGURED`、`NOT_STARTED`、`QUEUED`、`RUNNING`、`RETRYING`、`SUCCEEDED`、`FAILED` 和 `CANCELLED`。任务按最多 10 条条款分批调用模型，并在每批写入后更新进度。

## 8. 制度依据导入

PDF/TXT 制度预览需提供以下表单字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | `.pdf`、`.txt` 或 `.json` 文件 |
| `policy_no` | PDF/TXT 必填 | 制度编号 |
| `title` | PDF/TXT 必填 | 制度名称 |
| `version` | 否 | 制度版本，默认 `V1.0` |
| `issuer` | 否 | 发布部门 |
| `effective_date` | 否 | 生效日期，格式 `YYYY-MM-DD` |

结构化 JSON 示例：

```json
{
  "policy_no": "ZD-CG-2026-001",
  "title": "采购合同管理制度",
  "version": "V1.0",
  "issuer": "采购管理部",
  "effective_date": "2026-07-01",
  "sections": [
    {
      "section_no": "第三条",
      "title": "预付款控制",
      "content": "采购合同预付款比例原则上不得超过合同总价的百分之三十。"
    }
  ],
  "metadata": {}
}
```

同一 `policy_no` 再次导入时创建新的制度文档修订版本，并把旧版本标记为非当前版本。向量仍统一写入 `document_chunks.embedding`。

## 9. 错误响应

```json
{
  "code": "DOCUMENT_PARSE_ERROR",
  "message": "PDF 中未提取到文本，扫描件暂不支持，请先进行 OCR。"
}
```

| HTTP 状态 | 错误码 | 场景 |
|---|---|---|
| 413 | `FILE_TOO_LARGE` | 超过文件大小限制 |
| 415 | `UNSUPPORTED_FILE_TYPE` | 文件格式不支持 |
| 409 | `PREVIEW_FILE_MISMATCH` | 确认文件与预览文件不一致 |
| 422 | `DOCUMENT_PARSE_ERROR` | 内容解析或元数据校验失败 |
| 422 | `CONTRACT_TYPE_NOT_FOUND` | 合同类型不存在或未启用 |
| 404 | `IMPORT_RECORD_NOT_FOUND` | 查询的文档不存在 |
| 500 | `DEMO_CONTRACT_CLEANUP_ERROR` | 示例合同数据库事务清理失败 |

FastAPI 自身的请求体校验错误仍使用标准 `422` 响应。

## 10. 本地运行

```powershell
docker compose up -d postgres redis
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

另开一个终端启动 Worker：

```powershell
.\.venv\Scripts\python.exe -m celery -A app.core.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

启动后可访问：

- Swagger UI：`http://127.0.0.1:8000/docs`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`
- 健康检查：`http://127.0.0.1:8000/health`

## 11. 风险审查智能体

风险审查固定执行付款、质保、违约责任和争议解决四项检查。LangGraph 在加载合同上下文后并行扇出四个检查节点，等待四项全部结束后再执行汇总。每项检查完成以下步骤：

1. 只在当前合同文档中向量召回 Top 20，再由 `qwen3-rerank` 重排序并取 Top 5。重排第一名低于 `0.45` 时，系统认为当前检查项缺少可靠合同证据；`0.45～0.55` 记录为低置信度，但仍保留 Top 5 供模型判断。
2. 在当前有效制度中向量召回 Top 10，再由 `qwen3-rerank` 重排序并取 Top 5；最终候选低于 `0.60` 时不进入模型上下文。
3. 首次检索后通过 LangGraph 条件边检查两侧证据。两侧均有效时进入模型判断；任一侧不足时，使用固定同义词查询，仅对缺失来源补检一次。补检后仍不足则生成 `INSUFFICIENT_INFORMATION`，不调用聊天模型，也不会继续循环。
4. 证据充分时使用 `qwen-plus`（可通过 `REVIEW_MODEL` 修改）输出结构化结论。
5. 校验模型返回的 `C1/P1` 引用标签，并从数据库回查引用原文。
6. 保存风险项、证据、两轮向量/重排记录、条件路由摘要、模型调用记录和节点执行记录。

合同和制度章节必须先完成向量化。聊天模型、向量模型和重排序模型共用 `api-key`
环境变量。重排序调用百炼专用接口，可通过 `RERANK_MODEL`、`RERANK_URL`、
`CONTRACT_RECALL_TOP_K`、`CONTRACT_FINAL_TOP_K`、`CONTRACT_RERANK_QUERY_MIN_SCORE`、
`CONTRACT_RERANK_LOW_CONFIDENCE_SCORE`、`POLICY_RECALL_TOP_K`、`POLICY_FINAL_TOP_K`、
`POLICY_RERANK_MIN_SCORE` 和 `RERANK_HIGH_CONFIDENCE_SCORE` 配置。任一来源重排序失败时审查
不会失败，对应来源会降级为向量前 5，并在检索运行记录中保存错误摘要。阈值只作用于
Rerank 成功的结果，不能直接套用到向量降级分数。

默认使用百炼当前推荐的 `qwen3-rerank` 兼容接口；`gte-rerank-v2` 已停止服务，不应再作为
新项目默认模型。接口格式以[百炼 Text Rerank API](https://help.aliyun.com/en/model-studio/text-rerank-api)
为准。

### 创建审查

```http
POST /api/v1/risk-reviews
Content-Type: application/json

{
  "contract_id": "2e2ad8e6-c315-45c2-90ff-f0e04fcdfef3"
}
```

成功返回 HTTP `202 Accepted`：

```json
{
  "review_run_id": "02ec19a2-f138-4dac-b1b8-c61c8bb835ac",
  "job_id": "c483e8e1-c063-47c2-8493-e39d5734dcdc",
  "celery_task_id": "3311c8df-9eb6-4af4-a808-f8af812c0cf4",
  "status": "QUEUED",
  "message": "风险审查任务已进入队列。"
}
```

前端每 1.5 秒查询 `/api/v1/risk-reviews/{review_run_id}`。任务完成后响应包含总体风险、审批建议、四项检查结论及每项合同/制度证据。

`GET /api/v1/risk-reviews/contracts` 还会返回每份合同最近一次审查的
`latest_review_run_id`、`latest_review_status`、`latest_review_created_at` 和
`latest_review_is_current`。前端重新进入风险审查页面或切换合同时，会据此自动恢复最近
一次结果；如果任务仍在运行则继续轮询。

每项 `finding` 的 `retrieval_candidates` 返回该 LangGraph 分支已经持久化的检索候选，
字段包括来源类型、文档标题、条款编号、正文、向量排名/相似度、重排排名/分数、
`retrieval_attempt`、`query_kind`、`selected_for_context`、`ranking_strategy` 和
`selected_as_evidence`。`retrieval_attempt=1` 表示首次检索，`2` 表示唯一一次补检；同一
条款可能在两轮中分别出现。候选来自现有
`retrieval_runs`、`retrieval_hits` 和
`document_chunks`，不需要新增数据表。`evidence` 仍只表示最终采纳证据，不能把所有候选
等同于结论依据。合同检索记录还会在 `filters` 中保存查询级分数、置信区间和门槛；制度
检索记录保存候选阈值和高置信度参考值；两侧记录还会保存 `attempt` 和 `query_kind`，便于
复核当次审查为什么接收候选、进入补检或结束为信息不足。

### 已有数据库迁移

新建数据卷会由 `database/init/002_seed.sql` 初始化四项检查。已有演示数据库需要执行：

```powershell
docker compose up -d postgres
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/003_risk_review_agent.sql
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/005_policy_reranking.sql
```

## 风险项多轮问答接口

第一阶段的问答入口绑定在每一项风险结论上，前端按钮文案为“就此风险继续询问”。会话不是
跨合同的通用聊天：后端从 `finding_id` 反查并固定风险项、所属审查任务和本次审查使用的
`contract_document_id`，调用方不能在后续消息中切换合同或合同修订版本。即使合同后来产生
新修订版，历史会话仍使用原审查版本，避免引用内容发生漂移。

### `POST /api/v1/risk-findings/{finding_id}/chat-sessions`

为指定风险项创建或恢复会话。前端打开风险项的问答面板时调用此接口，并保存响应中的
`session_id`。同一风险项再次打开时返回已有会话及其历史内容，不需要由前端重复创建上下文。

只有所属风险审查已经成功完成的 `finding_id` 才能创建会话。会话上下文自动包含风险结论、风险建议、
原审查采用的合同/制度证据，以及固定的合同文档修订版本。

### `GET /api/v1/chat-sessions/{session_id}`

查询会话及按时间排序的历史消息。助手消息同时返回可追溯引用；引用指向实际的合同条款或
制度分块，而不是只保存模型生成的文字。前端可以据此展示引用标签，并展开条款编号、文档标题
和引用原文。

### `POST /api/v1/chat-sessions/{session_id}/messages`

提交一轮问题：

```json
{
  "content": "为什么预付款比例被判断为高风险？",
  "intent": "EXPLAIN",
  "client_request_id": "a973ed34-909d-4f9b-b280-a8aa2dbecb34"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `content` | 是 | 本轮用户问题 |
| `intent` | 否 | `AUTO`、`EXPLAIN`、`EVIDENCE_QUERY` 或 `DRAFT_CLAUSE`；省略时默认为 `AUTO` |
| `client_request_id` | 是 | 客户端为本轮请求生成的 UUID；重试时必须复用完全相同的问题和意图 |

`intent` 的处理边界如下：

- `AUTO`：由后端根据用户问题选择下面三种处理方式，适合自由输入。
- `EXPLAIN`：解释已有风险结论，优先复用原审查保存的结论与证据，不重新改变风险等级。
- `EVIDENCE_QUERY`：查询相关合同条款和制度依据；合同检索范围固定在会话绑定的
  `contract_document_id`，引用必须回查数据库中的真实分块。
- `DRAFT_CLAUSE`：结合原条款、风险建议和制度依据生成修改草案，并返回目标条款、建议文本、
  修改说明、理由和引用等结构化内容。

第一阶段仅实现短期上下文：每次生成回答时携带当前风险上下文和最近 10 条已成功历史消息，
并恢复这些消息中的结构化草案及“当轮引用标签 → 引用原文”快照，因此可以理解“上一版草案”
或“刚才 P2”之类的指代。更早消息仍保存在数据库中并可由查询接口展示，但不自动放入模型
上下文。暂不生成长期摘要，也不实现跨风险项、跨合同的用户偏好记忆。

模型返回的引用标签必须由后端校验，并转换为 `chat_message_citations` 中的真实分块引用。
后端不会在模型漏引或伪造标签时自动补成 `C1/P1`；无效引用和无效草案目标会被阻止展示。
如果候选材料确实不足，模型必须显式返回 `insufficient_evidence=true`，此时可以保留一条说明
缺少哪些材料的零引用回答，但不能同时返回条款草案。
解释既有结论只使用审查当时的证据快照；依据查询和草案生成可以检索当前有效制度，提示词会
明确区分“审查结论原始依据”和“当前有效制度”，避免把不同时间范围的制度混为一谈。
追问“上一版草案”或“刚才 P2”时会把上一轮真实引用恢复为本轮候选；普通的历史风险结论解释
仍只使用原审查快照，不会因出现“之前”等时间指代而引入当前制度。
条款修改草案只作为助手消息的结构化结果保存和展示，不写回合同正文、不创建合同修订版，
也不会自动提交、通过、退回或驳回任何审批。用户需要在现有合同编辑和审批流程中人工确认。

同一会话同一时间只生成一轮回答。前端遇到 `PENDING` 会自动查询会话并替换占位消息；服务
进程中断留下的 `PENDING` 超过 10 分钟后会转为 `FAILED`，随后可以重新发送。相同
`client_request_id` 携带不同内容或意图时返回 `409 CHAT_IDEMPOTENCY_CONFLICT`。
聊天模型和 Embedding 默认 60 秒请求超时，可通过 `MODEL_TIMEOUT_SECONDS` 调整，但应明显
小于 10 分钟的占位回收时间。

| HTTP 状态 | 典型错误码 | 说明 |
|---|---|---|
| `404` | `FINDING_NOT_FOUND`、`CHAT_SESSION_NOT_FOUND`、`CHAT_TURN_NOT_FOUND` | 风险项、会话或消息不存在 |
| `409` | `REVIEW_NOT_READY`、`CHAT_TURN_IN_PROGRESS`、`CHAT_IDEMPOTENCY_CONFLICT`、`CHAT_TURN_EXPIRED` | 审查未完成、已有回答在生成、幂等键冲突或本轮已超时 |
| `422` | `CHAT_MODEL_NOT_CONFIGURED` 或 FastAPI 校验详情 | 未配置模型，或请求字段不符合约束 |
| `500` | `CHAT_INTERNAL_ERROR` | 数据库或框架异常的脱敏兜底响应 |
| `502` | `CHAT_GENERATION_FAILED` | 模型、Embedding 或重排序调用失败 |

已有演示数据库还需要执行问答字段迁移；新建数据卷会直接使用初始化脚本中的最新结构：

```powershell
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/006_contract_chat.sql
```

## 辅助审批接口

当前审批流程固定为两级：`BUSINESS` 业务审批、`LEGAL` 法务审批。项目暂不包含用户和
角色表，因此接口用 `approver_name` 保存演示操作人姓名。后端始终处理当前节点，前端
不能指定或跳过审批级次。

### `GET /api/v1/approvals/candidates`

查询每份合同最近一次成功风险审查。响应同时说明报告是否对应合同当前版本，以及是否
已经创建审批实例。旧版本报告可以查看，但不能创建审批。

### `POST /api/v1/approvals`

依据成功风险报告幂等创建审批实例和两个节点：

```json
{
  "review_run_id": "风险审查任务 UUID"
}
```

创建后业务节点状态为 `IN_PROGRESS`，法务节点为 `PENDING`，合同状态变为
`PENDING_APPROVAL`。同一 `review_run_id` 重复提交会返回已经存在的实例，不会产生重复
流程。

### `GET /api/v1/approvals/{approval_instance_id}`

返回合同和风险报告摘要、总体风险、AI 审批辅助建议、四项检查简报、两级审批状态以及
已经保存的操作人和审批意见。

### `POST /api/v1/approvals/{approval_instance_id}/actions`

处理当前审批节点：

```json
{
  "approver_name": "演示审批人",
  "decision": "APPROVED",
  "comment": "已核对风险依据，同意进入下一节点。"
}
```

`decision` 支持：

- `APPROVED`：业务通过后激活法务；法务通过后合同最终变为 `APPROVED`。
- `RETURNED`：结束当前流程，未处理节点变为 `SKIPPED`，合同变为 `RETURNED`。
- `REJECTED`：结束当前流程，未处理节点变为 `SKIPPED`，合同变为 `REJECTED`。

退回和驳回必须填写 `comment`。审批结束后重复操作返回 `409`。业务错误继续使用统一的
`{ "code": "...", "message": "..." }` 响应结构。

已有数据库需要执行审批幂等约束迁移：

```powershell
docker compose exec postgres psql -U approval_user -d approval_assistant -f /migrations/004_approval_unique_review.sql
```
