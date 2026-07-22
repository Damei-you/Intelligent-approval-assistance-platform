<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  CircleAlert,
  FileSearch,
  LoaderCircle,
  RefreshCw,
  Scale,
  ShieldAlert,
  Sparkles,
} from '@lucide/vue'
import { createRiskReview, getRiskReview, listReviewContracts } from '../api/contracts'

const CHECKS = [
  { code: 'PAYMENT_TERMS', name: '付款条款' },
  { code: 'WARRANTY', name: '质保条款' },
  { code: 'BREACH_LIABILITY', name: '违约责任' },
  { code: 'DISPUTE_RESOLUTION', name: '争议解决' },
]

const contracts = ref([])
const selectedContractId = ref('')
const review = ref(null)
const loadingContracts = ref(false)
const starting = ref(false)
const errorMessage = ref('')
let pollingTimer = null

const selectedContract = computed(() => (
  contracts.value.find((item) => item.contract_id === selectedContractId.value)
))
const isRunning = computed(() => ['PENDING', 'RUNNING'].includes(review.value?.status))
const riskCount = computed(() => review.value?.findings?.filter((item) => item.status === 'RISK').length || 0)
const insufficientCount = computed(() => review.value?.findings?.filter((item) => item.status === 'INSUFFICIENT_INFORMATION').length || 0)

onMounted(loadContracts)
onBeforeUnmount(stopPolling)
watch(selectedContractId, loadLatestReview)

async function loadContracts() {
  loadingContracts.value = true
  errorMessage.value = ''
  try {
    const previousContractId = selectedContractId.value
    contracts.value = await listReviewContracts()
    if (!selectedContractId.value && contracts.value.length) {
      selectedContractId.value = contracts.value.find((item) => item.review_ready)?.contract_id || contracts.value[0].contract_id
    }
    if (selectedContractId.value && selectedContractId.value === previousContractId) {
      await loadLatestReview()
    }
  } catch (error) {
    errorMessage.value = error.message || '合同列表加载失败。'
  } finally {
    loadingContracts.value = false
  }
}

async function loadLatestReview() {
  stopPolling()
  review.value = null
  errorMessage.value = ''
  const latestReviewRunId = selectedContract.value?.latest_review_run_id
  if (!latestReviewRunId) return
  await refreshReview(latestReviewRunId)
  if (['PENDING', 'RUNNING'].includes(review.value?.status)) {
    pollingTimer = window.setInterval(() => refreshReview(latestReviewRunId), 1500)
  }
}

async function startReview() {
  if (!selectedContract.value?.review_ready || starting.value) return
  stopPolling()
  starting.value = true
  review.value = null
  errorMessage.value = ''
  try {
    const created = await createRiskReview(selectedContractId.value)
    if (selectedContract.value) {
      selectedContract.value.latest_review_run_id = created.review_run_id
      selectedContract.value.latest_review_status = 'PENDING'
      selectedContract.value.latest_review_is_current = true
    }
    await refreshReview(created.review_run_id)
    pollingTimer = window.setInterval(() => refreshReview(created.review_run_id), 1500)
  } catch (error) {
    errorMessage.value = error.message || '风险审查任务创建失败。'
  } finally {
    starting.value = false
  }
}

async function refreshReview(reviewRunId = review.value?.review_run_id) {
  if (!reviewRunId) return
  try {
    review.value = await getRiskReview(reviewRunId)
    if (!['PENDING', 'RUNNING'].includes(review.value.status)) stopPolling()
  } catch (error) {
    stopPolling()
    errorMessage.value = error.message || '审查进度查询失败。'
  }
}

function stopPolling() {
  if (pollingTimer !== null) {
    window.clearInterval(pollingTimer)
    pollingTimer = null
  }
}

function findingFor(code) {
  return review.value?.findings?.find((item) => item.check_code === code)
}

function statusText(status) {
  return {
    PASS: '通过',
    RISK: '发现风险',
    INSUFFICIENT_INFORMATION: '信息不足',
  }[status] || '等待检查'
}

function riskText(level) {
  return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[level] || '分析中'
}

function suggestionText(value) {
  return {
    APPROVE: '建议通过审批',
    APPROVE_AFTER_REVISION: '建议修改后审批',
    REJECT: '建议拒绝',
  }[value] || '等待汇总'
}

function candidatesFor(finding, evidenceType) {
  return finding.retrieval_candidates?.filter((item) => item.evidence_type === evidenceType) || []
}

function formatSimilarity(value) {
  return Number(value || 0).toFixed(4)
}
</script>

<template>
  <main class="review-page">
    <section class="review-hero">
      <div class="review-eyebrow"><Sparkles :size="15" /> RAG 风险审查智能体</div>
      <div class="review-hero-row">
        <div>
          <h1>让每一个风险结论，<br /><em>都能回到原始依据</em></h1>
          <p>基于当前合同条款与企业制度，执行付款、质保、违约责任和争议解决四项检查。</p>
        </div>
        <div class="agent-chip"><ShieldAlert :size="22" /><span><b>LangGraph Agent</b><small>合同证据 + 制度依据</small></span></div>
      </div>
    </section>

    <section class="review-launch-card">
      <div class="launch-copy">
        <span class="section-index">01</span>
        <div><h2>选择待审查合同</h2><p>审查固定使用所选合同的当前修订版本。</p></div>
      </div>
      <div class="contract-picker-row">
        <label class="contract-select">
          <span>当前合同</span>
          <select v-model="selectedContractId" :disabled="loadingContracts || isRunning">
            <option v-if="!contracts.length" value="">暂无已导入合同</option>
            <option v-for="contract in contracts" :key="contract.contract_id" :value="contract.contract_id">
              {{ contract.contract_no }} · {{ contract.contract_name }} · V{{ contract.revision_no }}
            </option>
          </select>
        </label>
        <div v-if="selectedContract" class="contract-readiness" :class="{ ready: selectedContract.review_ready }">
          <CheckCircle2 v-if="selectedContract.review_ready" :size="17" />
          <CircleAlert v-else :size="17" />
          <span>{{ selectedContract.vectorized_clause_count }}/{{ selectedContract.clause_count }} 条款已向量化</span>
        </div>
        <button class="review-start-button" type="button" :disabled="!selectedContract?.review_ready || starting || isRunning" @click="startReview">
          <LoaderCircle v-if="starting || isRunning" class="spinner" :size="18" />
          <FileSearch v-else :size="18" />
          {{ isRunning ? `四项并行审查 ${review?.progress || 0}%` : starting ? '正在创建…' : '开始风险审查' }}
          <ArrowRight v-if="!starting && !isRunning" :size="17" />
        </button>
      </div>
      <div v-if="errorMessage" class="review-error"><CircleAlert :size="18" />{{ errorMessage }}<button type="button" @click="loadContracts"><RefreshCw :size="14" />重试</button></div>
      <div v-if="review" class="review-progress"><i :style="{ width: `${review.progress}%` }"></i></div>
    </section>

    <section class="review-dashboard">
      <div class="review-main-column">
        <div class="review-section-heading">
          <div><span class="section-index">02</span><h2>四项风险检查</h2></div>
          <p>每项结论分别保存合同证据和制度依据。</p>
        </div>

        <div class="check-overview">
          <div v-for="check in CHECKS" :key="check.code" class="check-step" :class="findingFor(check.code)?.status?.toLowerCase()">
            <span class="check-number">
              <Check v-if="findingFor(check.code)?.status === 'PASS'" :size="15" />
              <AlertTriangle v-else-if="findingFor(check.code)" :size="15" />
              <LoaderCircle v-else-if="isRunning" class="spinner" :size="15" />
              <b v-else>{{ index + 1 }}</b>
            </span>
            <div><strong>{{ check.name }}</strong><small>{{ statusText(findingFor(check.code)?.status) }}</small></div>
          </div>
        </div>

        <div v-if="review?.findings?.length" class="finding-list">
          <article v-for="finding in review.findings" :key="finding.id" class="finding-card" :class="finding.status.toLowerCase()">
            <div class="finding-header">
              <span class="finding-icon"><CheckCircle2 v-if="finding.status === 'PASS'" :size="20" /><AlertTriangle v-else :size="20" /></span>
              <div><small>{{ finding.check_name }}</small><h3>{{ finding.title }}</h3></div>
              <div class="finding-badges"><b>{{ statusText(finding.status) }}</b><em>{{ riskText(finding.severity) }}</em></div>
            </div>
            <p class="finding-description">{{ finding.description }}</p>
            <div v-if="finding.suggestion" class="finding-suggestion"><Scale :size="16" /><span><b>修改建议</b>{{ finding.suggestion }}</span></div>
            <details v-if="finding.retrieval_candidates?.length" class="retrieval-candidates" open>
              <summary>
                <span><FileSearch :size="15" />本项检索候选</span>
                <small>合同 {{ candidatesFor(finding, 'CONTRACT').length }} 条 · 制度 {{ candidatesFor(finding, 'POLICY').length }} 条</small>
              </summary>
              <p class="candidate-explanation">合同保持向量 Top 3；制度先向量召回 Top 10，再重排取 Top 5 进入模型上下文。“已采纳”表示模型最终引用且后端已经保存。</p>
              <div class="candidate-grid">
                <section>
                  <h4><FileSearch :size="14" />合同候选</h4>
                  <article v-for="item in candidatesFor(finding, 'CONTRACT')" :key="item.chunk_id" :class="{ selected: item.selected_as_evidence }">
                    <div><b>#{{ item.rank_no }}</b><span>{{ item.clause_no }} {{ item.title }}</span><em>{{ formatSimilarity(item.similarity_score) }}</em><i v-if="item.selected_as_evidence">已采纳</i></div>
                    <p>{{ item.content }}</p>
                  </article>
                </section>
                <section>
                  <h4><BookOpen :size="14" />制度候选</h4>
                  <article v-for="item in candidatesFor(finding, 'POLICY')" :key="item.chunk_id" :class="{ selected: item.selected_as_evidence, contextual: item.selected_for_context }">
                    <div>
                      <b>向量 #{{ item.rank_no }}</b>
                      <b v-if="item.rerank_rank_no" class="rerank-rank">重排 #{{ item.rerank_rank_no }}</b>
                      <span>{{ item.document_title }} · {{ item.clause_no }} {{ item.title }}</span>
                      <em>V {{ formatSimilarity(item.similarity_score) }}<template v-if="item.rerank_score !== null && item.rerank_score !== undefined"> · R {{ formatSimilarity(item.rerank_score) }}</template></em>
                      <i v-if="item.selected_for_context" class="context-badge">入选</i>
                      <i v-if="item.selected_as_evidence">已采纳</i>
                    </div>
                    <p>{{ item.content }}</p>
                  </article>
                </section>
              </div>
            </details>
            <div class="evidence-grid">
              <div class="evidence-column">
                <h4><FileSearch :size="15" />合同条款</h4>
                <blockquote v-for="item in finding.evidence.filter((e) => e.evidence_type === 'CONTRACT')" :key="item.chunk_id">
                  <span>{{ item.clause_no }} {{ item.title }}</span><p>{{ item.cited_text }}</p>
                </blockquote>
                <p v-if="!finding.evidence.some((e) => e.evidence_type === 'CONTRACT')" class="no-evidence">未找到有效合同证据</p>
              </div>
              <div class="evidence-column policy">
                <h4><BookOpen :size="15" />制度依据</h4>
                <blockquote v-for="item in finding.evidence.filter((e) => e.evidence_type === 'POLICY')" :key="item.chunk_id">
                  <span>{{ item.document_title }} · {{ item.clause_no }} {{ item.title }}</span><p>{{ item.cited_text }}</p>
                </blockquote>
                <p v-if="!finding.evidence.some((e) => e.evidence_type === 'POLICY')" class="no-evidence">未找到有效制度依据</p>
              </div>
            </div>
          </article>
        </div>
        <div v-else class="review-empty">
          <FileSearch :size="30" /><strong>等待风险审查</strong><p>选择已完成向量化的合同，智能体将逐项生成可追溯结论。</p>
        </div>
      </div>

      <aside class="review-summary">
        <div class="summary-title"><span class="section-index">03</span><div><h2>审查汇总</h2><p>确定性规则汇总结论</p></div></div>
        <div class="risk-orb" :class="review?.overall_risk_level?.toLowerCase()">
          <ShieldAlert :size="31" /><strong>{{ riskText(review?.overall_risk_level) }}</strong><span>{{ review?.status === 'SUCCEEDED' ? '审查已完成' : isRunning ? '正在分析' : '等待开始' }}</span>
        </div>
        <div class="summary-stats">
          <div><strong>{{ riskCount }}</strong><span>风险项</span></div><div><strong>{{ insufficientCount }}</strong><span>信息不足</span></div><div><strong>{{ review?.findings?.length || 0 }}/4</strong><span>已完成</span></div>
        </div>
        <div class="approval-advice"><span>审批辅助建议</span><strong>{{ suggestionText(review?.approval_suggestion) }}</strong><p>{{ review?.summary || '四项检查完成后生成总体建议。' }}</p></div>
        <div class="summary-note"><BookOpen :size="16" /><p><b>证据优先</b><span>模型只能引用本次检索结果，引用文本由后端从 PostgreSQL 回查。</span></p></div>
      </aside>
    </section>
  </main>
</template>

<style scoped>
.review-page { width: min(1360px, calc(100% - 48px)); margin: 0 auto; padding-bottom: 26px; }
.review-hero { padding: 54px 4px 34px; }
.review-eyebrow { display: flex; align-items: center; gap: 7px; color: var(--green); font-size: 10px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
.review-hero-row { margin-top: 13px; display: flex; align-items: flex-end; justify-content: space-between; gap: 30px; }
.review-hero h1 { margin: 0; color: var(--navy); font-family: Georgia, 'Noto Serif SC', serif; font-size: clamp(36px, 4vw, 58px); line-height: 1.08; letter-spacing: -.035em; }
.review-hero h1 em { color: var(--green); font-style: normal; }
.review-hero p { max-width: 690px; margin: 17px 0 0; color: #68756e; font-size: 12px; line-height: 1.8; }
.agent-chip { min-width: 235px; padding: 15px 18px; display: flex; align-items: center; gap: 12px; border: 1px solid #cbd8cf; border-radius: 14px; color: var(--green); background: rgba(255,255,255,.56); }
.agent-chip span { display: flex; flex-direction: column; gap: 3px; }.agent-chip b { color: var(--navy); font-size: 11px; }.agent-chip small { color: #839087; font-size: 9px; }
.review-launch-card, .review-dashboard { border: 1px solid #d5ddd6; background: rgba(252,253,250,.9); box-shadow: 0 22px 60px rgba(31,52,42,.06); }
.review-launch-card { padding: 24px 28px; border-radius: 18px; }
.launch-copy, .review-section-heading > div, .summary-title { display: flex; align-items: center; gap: 13px; }.launch-copy h2, .review-section-heading h2, .summary-title h2 { margin: 0; color: var(--navy); font-size: 16px; }.launch-copy p, .summary-title p { margin: 3px 0 0; color: #89938d; font-size: 9px; }
.contract-picker-row { margin-top: 18px; display: grid; grid-template-columns: minmax(300px, 1fr) auto auto; align-items: end; gap: 13px; }
.contract-select { display: flex; flex-direction: column; gap: 7px; }.contract-select span { color: #52625a; font-size: 10px; font-weight: 700; }.contract-select select { width: 100%; padding: 12px 13px; border: 1px solid #d4dcd5; border-radius: 9px; outline: 0; color: var(--navy); background: white; font-size: 11px; }
.contract-readiness { height: 39px; padding: 0 12px; display: flex; align-items: center; gap: 7px; border-radius: 9px; color: #926837; background: #f7eddb; font-size: 9px; }.contract-readiness.ready { color: var(--green); background: #e3efe7; }
.review-start-button { height: 39px; padding: 0 17px; display: flex; align-items: center; justify-content: center; gap: 8px; border: 0; border-radius: 9px; color: white; background: var(--green); font-size: 10px; font-weight: 700; }.review-start-button:disabled { cursor: not-allowed; opacity: .52; }
.review-error { margin-top: 14px; padding: 10px 12px; display: flex; align-items: center; gap: 8px; border-radius: 8px; color: #9b403b; background: #fae9e7; font-size: 10px; }.review-error button { margin-left: auto; display: flex; gap: 4px; border: 0; color: inherit; background: transparent; }
.review-progress { height: 4px; margin: 18px -28px -24px; overflow: hidden; border-radius: 0 0 18px 18px; background: #e7ece8; }.review-progress i { height: 100%; display: block; background: var(--green); transition: width .35s ease; }
.review-dashboard { margin-top: 18px; display: grid; grid-template-columns: minmax(0, 1fr) 300px; border-radius: 18px; overflow: hidden; }.review-main-column { padding: 28px; }.review-section-heading { display: flex; justify-content: space-between; align-items: center; }.review-section-heading p { color: #8b958f; font-size: 9px; }
.check-overview { margin-top: 20px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 9px; }.check-step { padding: 12px; display: flex; align-items: center; gap: 9px; border: 1px solid #dde3dd; border-radius: 10px; background: #f7f9f6; }.check-number { width: 27px; height: 27px; display: grid; place-items: center; border-radius: 50%; color: #738078; background: #e7ece7; }.check-number b { font-size: 9px; }.check-step > div { display: flex; flex-direction: column; gap: 3px; }.check-step strong { color: var(--navy); font-size: 10px; }.check-step small { color: #879189; font-size: 8px; }.check-step.pass .check-number { color: white; background: var(--green); }.check-step.risk .check-number { color: white; background: #bb5148; }.check-step.insufficient_information .check-number { color: #8b681c; background: #f2dc9e; }
.finding-list { margin-top: 18px; display: flex; flex-direction: column; gap: 13px; }.finding-card { padding: 18px; border: 1px solid #dde3dd; border-left: 4px solid #bdc8c0; border-radius: 12px; background: white; }.finding-card.risk { border-left-color: #bd5149; }.finding-card.pass { border-left-color: var(--green); }.finding-card.insufficient_information { border-left-color: #d3a83e; }
.finding-header { display: flex; align-items: center; gap: 11px; }.finding-icon { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 9px; color: #a76c2c; background: #f7ead8; }.finding-card.pass .finding-icon { color: var(--green); background: #e4f0e8; }.finding-header > div:nth-child(2) { flex: 1; }.finding-header small { color: #8a958e; font-size: 8px; }.finding-header h3 { margin: 3px 0 0; color: var(--navy); font-size: 13px; }.finding-badges { display: flex; gap: 6px; }.finding-badges b, .finding-badges em { padding: 5px 7px; border-radius: 6px; font-size: 8px; font-style: normal; }.finding-badges b { color: #8e443e; background: #fae9e7; }.finding-badges em { color: #876526; background: #f7edda; }
.finding-description { margin: 14px 0; color: #58675f; font-size: 10px; line-height: 1.7; }.finding-suggestion { padding: 10px 12px; display: flex; gap: 8px; border-radius: 8px; color: var(--green); background: #e9f1eb; }.finding-suggestion span { display: flex; flex-direction: column; gap: 3px; color: #5e7568; font-size: 9px; line-height: 1.5; }.finding-suggestion b { color: var(--green); }
.retrieval-candidates { margin-top: 13px; border: 1px solid #dce4dd; border-radius: 10px; overflow: hidden; background: #fafbf9; }.retrieval-candidates summary { padding: 11px 13px; display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--navy); cursor: pointer; list-style: none; }.retrieval-candidates summary::-webkit-details-marker { display: none; }.retrieval-candidates summary > span { display: flex; align-items: center; gap: 7px; font-size: 9px; font-weight: 700; }.retrieval-candidates summary small { color: #849088; font-size: 8px; }.candidate-explanation { margin: 0; padding: 0 13px 10px; color: #8b958f; font-size: 8px; }.candidate-grid { padding: 0 10px 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }.candidate-grid section { padding: 10px; border-radius: 8px; background: #f1f4f1; }.candidate-grid h4 { margin: 0 0 8px; display: flex; align-items: center; gap: 6px; color: var(--navy); font-size: 9px; }.candidate-grid article { margin-top: 6px; padding: 8px; border: 1px solid #dfe5df; border-radius: 7px; background: white; }.candidate-grid article.contextual { border-color: #c5d8ca; }.candidate-grid article.selected { border-color: #8fbaa0; box-shadow: inset 2px 0 var(--green); }.candidate-grid article > div { display: flex; align-items: center; gap: 5px; }.candidate-grid article b { color: #809087; font-size: 8px; white-space: nowrap; }.candidate-grid article b.rerank-rank { color: #9a6b20; }.candidate-grid article span { min-width: 0; flex: 1; overflow: hidden; color: var(--green); font-size: 8px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }.candidate-grid article em { color: #89958e; font-size: 7px; font-style: normal; white-space: nowrap; }.candidate-grid article i { padding: 2px 5px; border-radius: 8px; color: white; background: var(--green); font-size: 7px; font-style: normal; white-space: nowrap; }.candidate-grid article i.context-badge { color: #7b5d1b; background: #f1dfaa; }.candidate-grid article p { margin: 5px 0 0; color: #68756e; font-size: 8px; line-height: 1.5; }
.evidence-grid { margin-top: 14px; display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }.evidence-column { padding: 12px; border-radius: 9px; background: #f3f6f3; }.evidence-column.policy { background: #f2f5ed; }.evidence-column h4 { margin: 0 0 9px; display: flex; align-items: center; gap: 6px; color: var(--navy); font-size: 9px; }.evidence-column blockquote { margin: 7px 0 0; padding: 9px; border: 1px solid #e0e6e0; border-radius: 7px; background: white; }.evidence-column blockquote span { color: var(--green); font-size: 8px; font-weight: 700; }.evidence-column blockquote p { margin: 5px 0 0; color: #5d6963; font-size: 9px; line-height: 1.55; }.no-evidence { color: #9b8a69; font-size: 9px; }
.review-empty { min-height: 290px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #86928a; }.review-empty strong { margin-top: 12px; color: var(--navy); font-size: 12px; }.review-empty p { margin: 7px 0; font-size: 9px; }
.review-summary { padding: 28px 23px; border-left: 1px solid #d8dfd8; background: #eef3ee; }.risk-orb { width: 132px; height: 132px; margin: 28px auto 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 1px solid #cdd9d0; border-radius: 50%; color: #718178; background: rgba(255,255,255,.65); }.risk-orb strong { margin-top: 6px; color: var(--navy); font-size: 15px; }.risk-orb span { margin-top: 3px; font-size: 8px; }.risk-orb.high { color: #b74f47; border-color: #e0aca8; background: #f8e6e4; }.risk-orb.medium { color: #a77b23; border-color: #e6cf93; background: #f8f0da; }.risk-orb.low { color: var(--green); border-color: #a9c8b3; background: #e1efe6; }
.summary-stats { display: grid; grid-template-columns: repeat(3, 1fr); border: 1px solid #d7dfd8; border-radius: 9px; overflow: hidden; }.summary-stats div { padding: 11px 5px; display: flex; flex-direction: column; align-items: center; gap: 3px; background: rgba(255,255,255,.52); }.summary-stats div + div { border-left: 1px solid #d7dfd8; }.summary-stats strong { color: var(--navy); font-size: 14px; }.summary-stats span { color: #849087; font-size: 8px; }.approval-advice { margin-top: 14px; padding: 14px; border-radius: 10px; color: white; background: var(--green); }.approval-advice span { font-size: 8px; opacity: .72; }.approval-advice strong { margin-top: 4px; display: block; font-size: 13px; }.approval-advice p { margin: 8px 0 0; font-size: 8px; line-height: 1.6; opacity: .78; }.summary-note { margin-top: 14px; padding: 12px; display: flex; gap: 8px; color: var(--green); background: #dde9e1; border-radius: 9px; }.summary-note p { margin: 0; display: flex; flex-direction: column; gap: 3px; }.summary-note b { font-size: 9px; }.summary-note span { color: #687c70; font-size: 8px; line-height: 1.5; }
@media (max-width: 980px) { .review-hero-row { align-items: flex-start; flex-direction: column; }.review-dashboard { grid-template-columns: 1fr; }.review-summary { border-left: 0; border-top: 1px solid #d8dfd8; }.contract-picker-row { grid-template-columns: 1fr 1fr; }.contract-select { grid-column: 1 / -1; } }
@media (max-width: 680px) { .review-page { width: min(100% - 24px, 1360px); }.review-hero { padding-top: 38px; }.agent-chip { width: 100%; }.review-launch-card, .review-main-column, .review-summary { padding: 20px 17px; }.contract-picker-row, .check-overview, .evidence-grid, .candidate-grid { grid-template-columns: 1fr; }.review-start-button, .contract-readiness { width: 100%; }.review-section-heading { align-items: flex-start; flex-direction: column; }.finding-header { align-items: flex-start; flex-wrap: wrap; }.finding-badges { margin-left: 45px; } }
</style>
