# RAG 自动评测工具

`tools/evaluate_rag.py` 用于评测“50 条合同 + 100 条制度”压力集，支持：

- 离线校验测试数据结构和标注。
- 调用真实 `text-embedding-v4` 与 pgvector 评测向量检索。
- 仅对制度侧调用 `qwen3-rerank`，比较重排序前后的结果。
- 对付款、质保、违约责任和争议解决四项风险审查执行端到端评测。

每次运行都会在 `output/evaluation` 中生成一份 JSON 原始结果和一份 Markdown 摘要。

## 1. 当前评测链路

实际风险审查与评测工具使用相同的检索范围：

```text
合同：当前合同文档 → 向量 Top 3 → LLM
制度：所有当前制度 → 向量 Top 10 → qwen3-rerank → Top 5 → LLM
```

合同侧暂不重排序。制度重排序失败时，正式风险审查会降级为向量 Top 5；检索评测命令
则直接报告错误，避免把降级结果误认为重排序实验结果。

当前本地数据库已经清理为只保留“一百条合同审查压力测试制度”，包含 100 个制度分块，
且均已向量化。清理过程中引用其他制度的历史审查也已删除，因此首次端到端评测需要重新
发起审查。

## 2. 运行前准备

启动 PostgreSQL、Redis、FastAPI 和 Celery Worker，并确保当前 PowerShell 与 Worker
都能读取同一个 `api-key` 环境变量。

已有数据库必须先应用制度重排序迁移：

```powershell
docker compose exec postgres psql `
  -U approval_user `
  -d approval_assistant `
  -f /migrations/005_policy_reranking.sql
```

本机数据库已经执行过该迁移，不需要重复执行。修改风险审查代码或模型配置后必须重启
Celery Worker，因为 LangGraph 风险审查实际运行在 Worker 进程中：

```powershell
.\.venv\Scripts\python.exe -m celery `
  -A app.core.celery_app:celery_app `
  worker `
  --loglevel=INFO `
  --pool=solo
```

可以用下面的 SQL 确认压力制度状态：

```sql
SELECT d.title,
       COUNT(dc.id) AS section_count,
       COUNT(dc.embedding) AS vectorized_count
FROM documents d
LEFT JOIN document_chunks dc ON dc.document_id = d.id
WHERE d.document_type = 'POLICY'
GROUP BY d.id, d.title;
```

预期只返回“一百条合同审查压力测试制度”，`section_count` 和 `vectorized_count` 均为
100。

## 3. 安全边界

默认模式只读取仓库中的 JSON 文件，不连接数据库，也不调用外部模型：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode validate `
  --label offline-check
```

以下操作会产生外部模型调用，必须显式添加 `--confirm-external-calls`：

- `--prepare`：缺少压力数据时执行导入，并通过 Celery 调用 Embedding。
- `--mode retrieval`：调用 Embedding 生成四个固定检查问题的查询向量。
- `--policy-rerank`：额外调用四次 `qwen3-rerank`。
- `--start-review`：通过 Celery 发起四分支审查，调用 Embedding、Rerank 和聊天模型。

测试数据全部为虚构内容。确认参数用于防止误操作产生外部调用和费用；不要把真实 API
Key 写入脚本、`.env.example` 或评测报告。

## 4. 准备压力数据

如果数据库中还没有压力合同或压力制度，可只执行准备，不立即运行检索指标：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode validate `
  --prepare `
  --confirm-external-calls `
  --label prepare-stress-data
```

`--prepare` 不会清空数据库。工具会按压力集中的制度编号和合同编号查找当前文档：

- 内容和压力集一致时直接复用。
- 同编号但内容不一致时通过现有导入服务创建新修订。
- 分块尚未向量化时投递 Celery 任务，并轮询 PostgreSQL 中的任务状态。

如果压力数据已经存在且 100 条制度均已向量化，后续评测不需要重复添加 `--prepare`。

## 5. 向量基线

下面的命令模拟引入重排序前的制度检索：合同向量 Top 3，制度向量 Top 5。

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode retrieval `
  --confirm-external-calls `
  --contract-top-k 3 `
  --policy-top-k 5 `
  --label vector-baseline-top5
```

此命令不会调用 Rerank。报告中的 `retrieval.ranking` 应为 `vector_baseline`。

## 6. 制度重排序实验

下面的命令使用当前正式链路：制度向量召回 Top 10，再重排序取 Top 5。合同侧参数与基线
保持一致，因此两份报告最终都以合同 Top 3、制度 Top 5 计算指标。

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode retrieval `
  --confirm-external-calls `
  --contract-top-k 3 `
  --policy-top-k 10 `
  --policy-rerank `
  --policy-final-top-k 5 `
  --label policy-rerank-top5
```

报告中的 `retrieval.ranking` 应为 `policy_rerank`，`rerank_model` 应为
`qwen3-rerank`。制度结果包含：

- `vector_candidates`：向量召回的 10 个原始候选。
- `hits`：重排序后进入最终 Top 5 的候选。
- `vector_rank_no`：候选原始向量排名。
- `rerank_rank_no`：重排序排名。
- `rerank_score`：只在本次查询候选集合内有比较意义的重排分数。

建议重点比较两份报告的制度 `MRR`、`NDCG@5`、`Recall@5`，以及各检查项中困难负样本
是否被移出前五。这里评测的是“扩大召回到 10 + Rerank”的完整新链路，而不是只隔离
Rerank 单一变量。

## 7. 端到端风险审查

由于旧审查已随非压力制度清理，第一次需要发起新的审查：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode e2e `
  --start-review `
  --confirm-external-calls `
  --label e2e-policy-rerank
```

之后可以评测压力合同最近一次成功审查，不再发起模型调用：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode e2e `
  --label latest-review
```

也可以通过 `--review-run-id` 指定已有任务。`--start-review` 依赖 Redis 和 Celery Worker；
只读取已有审查时不需要重新调用模型。

## 8. 指标说明

- `Recall@K`：标准答案中直接相关（2 分）条款进入前 K 的比例。
- `Precision@K`：前 K 项中直接相关条款的比例。
- `MRR`：第一个直接相关条款排名的倒数，越接近 1 越好。
- `NDCG@K`：同时考虑 2 分直接相关、1 分部分相关和 0 分无关项的排序质量。
- `status_accuracy`：四项风险结论与标准答案一致的比例。
- `contract_evidence_recall`：最终报告对标准合同证据的召回程度。
- `policy_evidence_recall`：最终报告对标准制度证据的召回程度。
- `forbidden_evidence_count`：最终报告引用 0 分干扰项的数量。
- `out_of_dataset_evidence_count`：最终报告引用压力制度之外证据的数量。

评测工具会同时校验 `document_id` 和条款编号，其他制度中相同的 `P025` 不会被误算为
正确命中。当前数据库只保留压力制度，因此 `out_of_dataset_evidence_count` 应为 0；以后
重新导入其他制度后，这一指标才能再次检验跨制度污染。

## 9. 输出与失败判定

默认输出：

```text
output/evaluation/<label>.json
output/evaluation/<label>.md
```

在 CI 中可添加 `--fail-on-mismatch`。该选项会在数据集校验失败，或端到端风险状态没有
全部匹配时返回退出码 1。普通本地实验默认只记录结果，不会因为模型输出波动中断命令。

如果检索评测提示未找到测试数据、分块未向量化或等待任务超时，应依次检查：

1. PostgreSQL、Redis 和 Celery Worker 是否已启动。
2. 当前 PowerShell 与 Worker 是否都能读取 `api-key`。
3. `005_policy_reranking.sql` 是否已经应用。
4. 压力合同和压力制度是否为当前修订版本。
5. `async_jobs.error_message` 和 Celery Worker 日志中的具体错误。
