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
| GET | `/api/v1/contracts/imports/{document_id}` | - | 查询导入记录及向量化数量 |
| GET | `/api/v1/contracts/imports/{document_id}/vectorization` | - | 查询向量化任务状态和进度 |
| POST | `/api/v1/policies/imports/preview/file` | `multipart/form-data` | 解析制度文件并返回待确认 JSON |
| POST | `/api/v1/policies/imports/confirm/file` | `multipart/form-data` | 确认制度 JSON 并正式入库 |
| POST | `/api/v1/policies/imports/json` | `application/json` | 直接提交结构化制度章节 |
| GET | `/api/v1/policies/imports/{document_id}` | - | 查询制度导入详情 |
| GET | `/api/v1/policies/imports/{document_id}/vectorization` | - | 查询制度向量化进度 |

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

## 5. 查询导入结果

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

## 6. 查询向量化进度

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

## 7. 制度依据导入

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

## 8. 错误响应

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

FastAPI 自身的请求体校验错误仍使用标准 `422` 响应。

## 9. 本地运行

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
