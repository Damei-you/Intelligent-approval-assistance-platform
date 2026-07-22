# RAG 自动评测工具

`tools/evaluate_rag.py` 用于评测 50 条合同、100 条制度压力集，支持离线数据校验、
真实 pgvector 检索评测和风险审查端到端评测。每次运行同时输出 JSON 原始结果和
Markdown 摘要，默认目录为 `output/evaluation`。

## 安全边界

默认模式只读取仓库中的 JSON 文件，不连接数据库，也不调用外部模型：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode validate `
  --label offline-check
```

以下操作会使用当前进程的 `api-key` 调用百炼 API，必须显式添加
`--confirm-external-calls`：

- `--mode retrieval`：调用 `text-embedding-v4` 生成四个固定检查问题的向量。
- `--prepare`：导入缺失或内容不一致的压力集，并通过 Celery 异步生成文档向量。
- `--start-review`：通过 Celery 发起新审查，四个 LangGraph 分支会调用审查模型。

测试数据全部为虚构内容。该确认参数用于防止误操作产生外部调用和费用，不应在脚本中
写入真实 API Key。

## 首次准备并评测检索

先启动 PostgreSQL、Redis 和 Celery worker，并保证 worker 与当前 PowerShell 都能读取
`api-key` 环境变量。随后执行：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode retrieval `
  --prepare `
  --confirm-external-calls `
  --contract-top-k 20 `
  --policy-top-k 30 `
  --label vector-baseline
```

`--prepare` 不会清理数据库。若同编号当前文档和压力集内容不同，工具会按现有导入服务
创建新修订；若内容相同则直接复用。工具会检查每个分块是否已有向量，并在需要时投递
Celery 任务，然后轮询 PostgreSQL 中的持久化任务状态。

## 端到端审查评测

评测该压力合同最近一次成功审查，不发起新的模型调用：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode e2e `
  --label latest-review
```

也可以通过 `--review-run-id` 指定已有任务。若要新发起审查：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode e2e `
  --start-review `
  --confirm-external-calls `
  --label e2e-new-run
```

首次准备、检索和新审查也可以一次执行：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode all `
  --prepare `
  --start-review `
  --confirm-external-calls `
  --label full-baseline
```

## 指标说明

- `Recall@K`：标准答案中“直接相关（2 分）”条款进入候选集的比例。
- `Precision@K`：前 K 项中直接相关条款的比例，适合比较排序方案。
- `MRR`：第一个直接相关条款排名的倒数，越接近 1，关键依据越靠前。
- `NDCG@K`：同时考虑 2 分直接相关、1 分部分相关和 0 分无关项的排序质量。
- `status_accuracy`：付款、质保、违约责任、争议解决四项结论与标准答案一致的比例。
- `contract_evidence_recall` / `policy_evidence_recall`：最终报告引用标准证据的完整程度。
- `forbidden_evidence_count`：最终报告引用明确标为 0 分干扰项的数量。
- `out_of_dataset_evidence_count`：最终报告引用其他制度文档证据的数量。

制度向量检索本身面向全部当前制度。评测工具会同时校验 `document_id` 和条款编号，
因此其他制度中同名的 `P025` 不会被算成压力集正确命中。

## 用于后续重排序对比

当前输出标签建议使用 `vector-baseline`。加入 reranker 后保持数据集、候选 Top K 和模型
配置一致，用新标签输出第二份报告，例如 `rerank-v1`，即可对比两份 JSON 中各检查项和
汇总的 MRR、NDCG、最终证据召回。工具当前只实现向量基线，不会伪造尚未接入的重排序
结果。

在 CI 中可添加 `--fail-on-mismatch`。该选项会在数据集校验失败或端到端风险状态未全部
匹配时返回退出码 1；普通实验默认只记录结果，不因模型波动中断命令。
