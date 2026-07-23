# RAG 自动评测工具

`tools/evaluate_rag.py` 用于评测“50 条合同 + 100 条制度”压力集，支持：

- 离线校验测试数据结构和标注。
- 调用真实 `text-embedding-v4` 与 pgvector 评测向量检索。
- 对合同和制度候选调用 `qwen3-rerank`，比较重排序与阈值过滤前后的结果。
- 对付款、质保、违约责任和争议解决四项风险审查执行端到端评测。

每次运行都会在 `output/evaluation` 中生成一份 JSON 原始结果和一份 Markdown 摘要。

## 1. 当前评测链路

实际风险审查与评测工具使用相同的检索范围：

```text
合同：当前合同文档 → 向量 Top 20 → qwen3-rerank → Top 5 → 查询级 0.45 门槛 → LLM
制度：所有当前制度 → 向量 Top 10 → qwen3-rerank → Top 5 → 候选 0.60 阈值 → LLM
```

任一来源重排序失败时，正式风险审查会把该来源降级为向量 Top 5；检索评测命令则直接
报告错误，避免把降级结果误认为重排序实验结果。

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

下面的命令建立不调用 Rerank 的向量 Top 5 基线：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode retrieval `
  --confirm-external-calls `
  --contract-top-k 5 `
  --policy-top-k 5 `
  --label vector-baseline-top5
```

此命令不会调用 Rerank。报告中的 `retrieval.ranking` 应为 `vector_baseline`。

## 6. 合同与制度重排序实验

下面的命令使用当前正式链路：合同向量召回 Top 20、制度向量召回 Top 10，两侧重排序取
Top 5，并分别应用合同查询级 `0.45` 门槛和制度候选 `0.60` 阈值。

```powershell
.\.venv\Scripts\python.exe tools\evaluate_rag.py `
  --mode retrieval `
  --confirm-external-calls `
  --contract-top-k 20 `
  --contract-rerank `
  --contract-final-top-k 5 `
  --contract-query-min-score 0.45 `
  --policy-top-k 10 `
  --policy-rerank `
  --policy-final-top-k 5 `
  --policy-min-score 0.60 `
  --label contract-policy-rerank-thresholds
```

报告中的 `retrieval.ranking` 应为 `contract_policy_rerank`，`rerank_model` 应为
`qwen3-rerank`。合同和制度结果均包含：

- `vector_candidates`：向量召回并完成重排评分的全部候选。
- `hits`：通过对应阈值并进入最终上下文的候选。
- `vector_rank_no`：候选原始向量排名。
- `rerank_rank_no`：重排序排名。
- `rerank_score`：只在本次查询候选集合内有比较意义的重排分数。

建议重点比较两份报告的合同/制度 `MRR`、`NDCG@5`、`Recall@5`，以及各检查项中困难
负样本是否被移出前五。这里评测的是“扩大召回 + Rerank + 阈值”的完整正式链路，而不是
只隔离 Rerank 单一变量。

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

## 10. 相关性阈值实验记录

2026-07-23 使用 `text-embedding-v4` 和 `qwen3-rerank` 完成了一轮阈值实验。实验覆盖：

- 50 条合同、100 条制度的压力集；
- 主评测集中的 13 份合同和 52 个“合同 × 风险检查项”组合；
- 付款、质保、违约责任和争议解决四类查询；
- 关键条款完全缺失、语义模糊、历史条款、相似数字和困难负样本。

本节数值是当前模型、查询文本、重排序指令和测试数据下的实验结果，已作为风险审查默认
配置写入代码。更换模型、查询模板、分块方式或重排序指令后必须重新标定。

### 10.1 合同向量相似度

50 条压力合同的全量分数分布如下：

| 标注等级 | 样本数 | 最低分 | 中位数 | 最高分 |
| --- | ---: | ---: | ---: | ---: |
| 直接相关（2 分） | 4 | 0.614396 | 0.634900 | 0.660866 |
| 部分相关（1 分） | 8 | 0.451695 | 0.517939 | 0.596980 |
| 干扰项（0 分） | 188 | 0.344748 | 0.445435 | 0.628704 |

干扰项最高分高于部分直接相关条款，部分相关条款又与大量干扰项重叠。因此合同向量分数
不适合设置逐条硬过滤阈值，只用于扩大候选召回和排序。

| 合同向量候选数 | 直接证据召回率 | 全部有用证据召回率 |
| ---: | ---: | ---: |
| Top 3 | 100% | 41.7% |
| Top 10 | 100% | 75.0% |
| Top 20 | 100% | 83.3% |
| Top 50 | 100% | 100% |

当前测试建议把合同候选扩大到 Top 20，再通过合同侧 Rerank 取最终 Top 5。不要在 Rerank
之前按向量相似度删除候选。

### 10.2 合同重排序阈值

合同侧实验使用 Top 10/Top 20 候选，并向 `qwen3-rerank` 提供合同专用指令：

```text
Given a contract risk query, retrieve relevant clauses from the current contract.
```

主评测集结果中，关键条款完全缺失时四项检查的最高 Rerank 分数为
`0.404055～0.439069`；存在相关或语义模糊条款时，各检查最高分的最低值为 `0.459091`。
因此当前建议把 `0.45` 作为**查询级门槛**：

- 第一名低于 `0.45`：认为没有可靠合同证据，返回 `INSUFFICIENT_INFORMATION`；
- 第一名位于 `0.45～0.55`：标记为低置信度，仍把重排 Top 5 交给模型判断；
- 第一名不低于 `0.55`：按正常流程审查。

`0.45` 不应作为逐条删除所有候选的阈值。逐条使用 `0.45` 时，主评测集证据召回率为
92.9%，检查项覆盖率为 95.8%；提高到 `0.60` 后，证据召回率降至 75.0%，检查项覆盖率
降至 83.3%。因此低分补充条款仍应保留在已经通过查询级门槛的 Top 5 上下文中。

当前 `0.45` 与缺失样本最高分之间只有约 0.011 的间隔，属于需要持续观察的临界值。
增加更多“整类条款完全缺失”的负样本后，应再次检查该门槛。

### 10.3 制度重排序阈值

压力制度采用向量 Top 10、Rerank 后 Top 5。不同候选阈值的结果如下：

| Rerank 阈值 | 直接证据召回率 | 全部有用证据召回率 | 入选证据准确率 |
| ---: | ---: | ---: | ---: |
| 0.60 | 100% | 91.7% | 68.8% |
| 0.65～0.70 | 100% | 83.3% | 83.3% |
| 0.74 | 100% | 66.7% | 100% |

主评测制度中的违约责任直接规则得分为 `0.691446`，因此 `0.70` 不适合作为硬过滤阈值。
当前建议：

- Top 5 中低于 `0.60` 的制度候选不进入模型上下文；
- `0.60～0.70` 作为普通可信候选；
- 不低于 `0.70` 只标记为高置信度，不改变业务结论。

当前评测集没有“对应制度规则完全缺失”的负样本，所以暂时不能仅凭制度最高分自动判定
制度依据缺失。应先补充这种负样本，再决定是否增加制度查询级门槛。

### 10.4 当前默认配置

以下环境变量已经由风险审查服务读取，未显式设置时使用所列默认值：

```text
CONTRACT_RECALL_TOP_K=20
CONTRACT_FINAL_TOP_K=5
CONTRACT_RERANK_QUERY_MIN_SCORE=0.45
CONTRACT_RERANK_LOW_CONFIDENCE_SCORE=0.55
POLICY_RECALL_TOP_K=10
POLICY_FINAL_TOP_K=5
POLICY_RERANK_MIN_SCORE=0.60
RERANK_HIGH_CONFIDENCE_SCORE=0.70
```

本次模型调用生成的原始检索结果保存在：

```text
output/evaluation/threshold-analysis-full.json
output/evaluation/threshold-analysis-production-window.json
```
