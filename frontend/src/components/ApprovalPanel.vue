<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  ArrowRight,
  BriefcaseBusiness,
  Check,
  CheckCircle2,
  CircleAlert,
  FileCheck2,
  Gavel,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Scale,
  ShieldAlert,
  XCircle,
} from '@lucide/vue'
import {
  createApproval,
  getApproval,
  listApprovalCandidates,
  takeApprovalAction,
} from '../api/contracts'

const candidates = ref([])
const selectedReviewId = ref('')
const approval = ref(null)
const approverName = ref('演示审批人')
const comment = ref('')
const loading = ref(false)
const submitting = ref(false)
const errorMessage = ref('')

const selectedCandidate = computed(() => (
  candidates.value.find((item) => item.review_run_id === selectedReviewId.value)
))
const currentStep = computed(() => (
  approval.value?.steps?.find((step) => step.step_no === approval.value.current_step_no)
))
const isCompleted = computed(() => approval.value && approval.value.status !== 'IN_PROGRESS')
const canStart = computed(() => (
  selectedCandidate.value?.approval_ready && !selectedCandidate.value?.approval_instance_id
))

onMounted(loadCandidates)
watch(selectedReviewId, loadSelectedApproval)

async function loadCandidates() {
  loading.value = true
  errorMessage.value = ''
  try {
    const previous = selectedReviewId.value
    candidates.value = await listApprovalCandidates()
    selectedReviewId.value = candidates.value.some((item) => item.review_run_id === previous)
      ? previous
      : candidates.value[0]?.review_run_id || ''
    if (selectedReviewId.value === previous) await loadSelectedApproval()
  } catch (error) {
    errorMessage.value = error.message || '待审批合同加载失败。'
  } finally {
    loading.value = false
  }
}

async function loadSelectedApproval() {
  approval.value = null
  errorMessage.value = ''
  const instanceId = selectedCandidate.value?.approval_instance_id
  if (!instanceId) return
  try {
    approval.value = await getApproval(instanceId)
  } catch (error) {
    errorMessage.value = error.message || '审批详情加载失败。'
  }
}

async function startApproval() {
  if (!canStart.value || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    approval.value = await createApproval(selectedReviewId.value)
    await refreshCandidateState()
  } catch (error) {
    errorMessage.value = error.message || '审批流程创建失败。'
  } finally {
    submitting.value = false
  }
}

async function submitDecision(decision) {
  if (!approval.value || isCompleted.value || submitting.value) return
  if (!approverName.value.trim()) {
    errorMessage.value = '请填写审批人姓名。'
    return
  }
  if (['REJECTED', 'RETURNED'].includes(decision) && !comment.value.trim()) {
    errorMessage.value = '驳回或退回时必须填写审批意见。'
    return
  }
  submitting.value = true
  errorMessage.value = ''
  try {
    approval.value = await takeApprovalAction(approval.value.approval_instance_id, {
      approver_name: approverName.value.trim(),
      decision,
      comment: comment.value.trim() || null,
    })
    comment.value = ''
    await refreshCandidateState()
  } catch (error) {
    errorMessage.value = error.message || '审批操作失败。'
  } finally {
    submitting.value = false
  }
}

async function refreshCandidateState() {
  const rows = await listApprovalCandidates()
  candidates.value = rows
}

function stepStatusText(status) {
  return {
    PENDING: '等待前序节点',
    IN_PROGRESS: '当前待处理',
    COMPLETED: '已处理',
    SKIPPED: '已跳过',
  }[status] || status
}

function decisionText(decision) {
  return {
    APPROVED: '通过',
    REJECTED: '驳回',
    RETURNED: '退回修改',
  }[decision] || '尚未决定'
}

function riskText(level) {
  return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[level] || '未评估'
}

function suggestionText(value) {
  return {
    APPROVE: '智能体建议通过',
    APPROVE_AFTER_REVISION: '智能体建议修改后通过',
    REJECT: '智能体建议拒绝',
  }[value] || '暂无建议'
}

function findingStatusText(status) {
  return {
    PASS: '通过',
    RISK: '发现风险',
    INSUFFICIENT_INFORMATION: '信息不足',
  }[status] || status
}
</script>

<template>
  <main class="approval-page">
    <section class="approval-hero">
      <div class="approval-eyebrow"><Gavel :size="15" /> HUMAN-IN-THE-LOOP APPROVAL</div>
      <div class="approval-hero-row">
        <div>
          <h1>让智能体提供依据，<br /><em>由人作出最终决定</em></h1>
          <p>当前演示采用固定两级流程：业务审批负责业务条件，法务审批负责法律风险。</p>
        </div>
        <div class="role-summary">
          <span><BriefcaseBusiness :size="20" /><b>01 业务审批</b></span>
          <ArrowRight :size="16" />
          <span><Scale :size="20" /><b>02 法务审批</b></span>
        </div>
      </div>
    </section>

    <section class="approval-select-card">
      <div class="select-copy"><span class="approval-index">01</span><div><h2>选择风险审查报告</h2><p>只能审批合同当前修订版本对应的成功报告。</p></div></div>
      <div class="approval-select-row">
        <label>
          <span>待审批合同</span>
          <select v-model="selectedReviewId" :disabled="loading || submitting">
            <option v-if="!candidates.length" value="">暂无已完成风险审查的合同</option>
            <option v-for="item in candidates" :key="item.review_run_id" :value="item.review_run_id">
              {{ item.contract_no }} · {{ item.contract_name }} · V{{ item.revision_no }} · {{ riskText(item.overall_risk_level) }}
            </option>
          </select>
        </label>
        <div v-if="selectedCandidate" class="candidate-state" :class="{ blocked: !selectedCandidate.is_current_revision }">
          <CheckCircle2 v-if="selectedCandidate.is_current_revision" :size="17" />
          <CircleAlert v-else :size="17" />
          <span>{{ selectedCandidate.is_current_revision ? '当前合同版本' : '报告版本已过期' }}</span>
        </div>
        <button v-if="!approval" class="start-approval" type="button" :disabled="!canStart || submitting" @click="startApproval">
          <LoaderCircle v-if="submitting" class="spinner" :size="18" />
          <FileCheck2 v-else :size="18" />
          {{ selectedCandidate?.approval_instance_id ? '加载审批流程' : '发起两级审批' }}
        </button>
        <div v-else class="instance-state" :class="approval.status.toLowerCase()">
          <span>流程状态</span><strong>{{ approval.status === 'IN_PROGRESS' ? `进行中 · 第 ${approval.current_step_no} 级` : decisionText(approval.final_decision) }}</strong>
        </div>
      </div>
      <div v-if="errorMessage" class="approval-error"><CircleAlert :size="17" />{{ errorMessage }}<button type="button" @click="loadCandidates"><RefreshCw :size="14" />刷新</button></div>
    </section>

    <section v-if="selectedCandidate" class="approval-workspace">
      <div class="approval-main">
        <div class="approval-heading"><div><span class="approval-index">02</span><h2>审批链路</h2></div><p>后端只允许处理当前节点，不能跳过业务审批直接操作法务节点。</p></div>
        <div class="step-timeline">
          <article v-for="step in approval?.steps || [{ step_no: 1, step_type: 'BUSINESS', step_name: '业务审批', status: 'PENDING' }, { step_no: 2, step_type: 'LEGAL', step_name: '法务审批', status: 'PENDING' }]" :key="step.step_no" class="approval-step" :class="step.status.toLowerCase()">
            <span class="step-icon">
              <Check v-if="step.status === 'COMPLETED'" :size="17" />
              <LoaderCircle v-else-if="step.status === 'IN_PROGRESS'" class="spinner" :size="17" />
              <b v-else>{{ step.step_no }}</b>
            </span>
            <div class="step-copy"><small>{{ step.step_type === 'BUSINESS' ? 'BUSINESS' : 'LEGAL' }}</small><h3>{{ step.step_name }}</h3><p>{{ stepStatusText(step.status) }}</p></div>
            <div v-if="step.decision" class="step-result"><strong>{{ decisionText(step.decision) }}</strong><span>{{ step.approver_name }}</span><p v-if="step.comment">{{ step.comment }}</p></div>
          </article>
        </div>

        <div class="approval-heading risk-heading"><div><span class="approval-index">03</span><h2>风险审查摘要</h2></div><p>智能建议只提供辅助信息，不会替代人工审批决定。</p></div>
        <div class="risk-banner" :class="(approval?.overall_risk_level || selectedCandidate.overall_risk_level).toLowerCase()">
          <ShieldAlert :size="24" />
          <div><small>{{ riskText(approval?.overall_risk_level || selectedCandidate.overall_risk_level) }}</small><strong>{{ suggestionText(approval?.approval_suggestion || selectedCandidate.approval_suggestion) }}</strong><p>{{ approval?.review_summary || selectedCandidate.review_summary }}</p></div>
        </div>
        <div v-if="approval?.findings?.length" class="approval-findings">
          <article v-for="finding in approval.findings" :key="finding.check_code" :class="finding.status.toLowerCase()">
            <div><span>{{ finding.check_name }}</span><b>{{ findingStatusText(finding.status) }}</b></div>
            <h3>{{ finding.title }}</h3><p>{{ finding.suggestion || '无需额外修改建议。' }}</p>
          </article>
        </div>
      </div>

      <aside class="action-panel">
        <div class="action-title"><span class="approval-index">04</span><div><h2>人工决策</h2><p>{{ currentStep?.step_name || (isCompleted ? '审批已结束' : '发起后可操作') }}</p></div></div>
        <template v-if="approval && !isCompleted">
          <label><span>审批人姓名</span><input v-model="approverName" maxlength="128" placeholder="请输入操作人姓名" /></label>
          <label><span>审批意见</span><textarea v-model="comment" maxlength="2000" rows="6" placeholder="通过时可选；退回或驳回时必填。"></textarea><small>{{ comment.length }}/2000</small></label>
          <div class="decision-actions">
            <button type="button" class="approve" :disabled="submitting" @click="submitDecision('APPROVED')"><CheckCircle2 :size="17" />通过</button>
            <button type="button" class="return" :disabled="submitting" @click="submitDecision('RETURNED')"><RotateCcw :size="17" />退回修改</button>
            <button type="button" class="reject" :disabled="submitting" @click="submitDecision('REJECTED')"><XCircle :size="17" />驳回</button>
          </div>
          <p class="action-hint">通过业务审批后自动进入法务审批；退回或驳回会立即结束本次流程。</p>
        </template>
        <div v-else-if="isCompleted" class="final-result" :class="approval.status.toLowerCase()"><CheckCircle2 v-if="approval.status === 'APPROVED'" :size="34" /><XCircle v-else :size="34" /><strong>{{ decisionText(approval.final_decision) }}</strong><p>本次审批已结束，所有节点意见已持久化保存。</p></div>
        <div v-else class="action-empty"><Gavel :size="30" /><strong>尚未发起审批</strong><p>确认风险报告版本后创建固定两级审批流程。</p></div>
      </aside>
    </section>

    <section v-else class="approval-empty-page"><FileCheck2 :size="34" /><strong>暂无待审批报告</strong><p>请先在“风险审查”页面完成合同检查。</p></section>
  </main>
</template>

<style scoped>
.approval-page { width: min(1360px, calc(100% - 48px)); margin: 0 auto; padding-bottom: 26px; }
.approval-hero { padding: 54px 4px 34px; }.approval-eyebrow { display: flex; align-items: center; gap: 7px; color: var(--green); font-size: 10px; font-weight: 700; letter-spacing: .09em; }.approval-hero-row { margin-top: 13px; display: flex; align-items: flex-end; justify-content: space-between; gap: 30px; }.approval-hero h1 { margin: 0; color: var(--navy); font-family: Georgia, 'Noto Serif SC', serif; font-size: clamp(36px, 4vw, 58px); line-height: 1.08; letter-spacing: -.035em; }.approval-hero h1 em { color: var(--green); font-style: normal; }.approval-hero p { margin: 17px 0 0; color: #68756e; font-size: 12px; }.role-summary { padding: 15px 18px; display: flex; align-items: center; gap: 14px; border: 1px solid #cbd8cf; border-radius: 14px; background: rgba(255,255,255,.58); color: #819087; }.role-summary span { display: flex; align-items: center; gap: 8px; color: var(--green); }.role-summary b { color: var(--navy); font-size: 10px; }
.approval-select-card, .approval-workspace { border: 1px solid #d5ddd6; background: rgba(252,253,250,.92); box-shadow: 0 22px 60px rgba(31,52,42,.06); }.approval-select-card { padding: 24px 28px; border-radius: 18px; }.select-copy, .approval-heading > div, .action-title { display: flex; align-items: center; gap: 13px; }.approval-index { width: 29px; height: 29px; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; border: 1px solid #a9c7b2; border-radius: 8px; color: var(--green); font-size: 9px; font-weight: 800; }.select-copy h2, .approval-heading h2, .action-title h2 { margin: 0; color: var(--navy); font-size: 16px; }.select-copy p, .action-title p { margin: 4px 0 0; color: #89938d; font-size: 9px; }.approval-select-row { margin-top: 20px; display: grid; grid-template-columns: minmax(300px, 1fr) auto auto; gap: 12px; align-items: end; }.approval-select-row label, .action-panel label { display: flex; flex-direction: column; gap: 6px; color: #75817a; font-size: 9px; }.approval-select-row select, .action-panel input, .action-panel textarea { width: 100%; border: 1px solid #d5ddd6; border-radius: 9px; color: var(--navy); background: white; outline: none; }.approval-select-row select { height: 42px; padding: 0 12px; }.action-panel input { height: 41px; padding: 0 11px; }.action-panel textarea { padding: 10px 11px; resize: vertical; font: inherit; }.candidate-state, .instance-state, .start-approval { min-height: 42px; padding: 0 15px; display: flex; align-items: center; gap: 8px; border-radius: 9px; }.candidate-state { color: var(--green); background: #e1eee5; }.candidate-state.blocked { color: #a46e24; background: #f5ead5; }.candidate-state span { font-size: 9px; font-weight: 700; }.start-approval { border: 0; color: white; background: var(--green); font-size: 10px; font-weight: 700; cursor: pointer; }.start-approval:disabled { cursor: not-allowed; opacity: .46; }.instance-state { min-width: 155px; align-items: flex-start; justify-content: center; flex-direction: column; gap: 2px; background: #e6eee8; }.instance-state span { color: #829087; font-size: 8px; }.instance-state strong { color: var(--green); font-size: 10px; }.approval-error { margin-top: 13px; padding: 10px 12px; display: flex; align-items: center; gap: 8px; border-radius: 8px; color: #a8403b; background: #fae9e7; font-size: 9px; }.approval-error button { margin-left: auto; display: flex; gap: 5px; border: 0; color: inherit; background: transparent; cursor: pointer; }
.approval-workspace { margin-top: 18px; display: grid; grid-template-columns: minmax(0, 1fr) 330px; border-radius: 18px; overflow: hidden; }.approval-main { padding: 28px 30px; }.approval-heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; }.approval-heading p { margin: 0; color: #89938d; font-size: 8px; }.step-timeline { margin-top: 20px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }.approval-step { min-height: 116px; padding: 17px; display: flex; align-items: flex-start; gap: 12px; border: 1px solid #dde3dd; border-radius: 12px; background: #f6f8f5; }.approval-step.in_progress { border-color: #8eb59a; box-shadow: inset 3px 0 var(--green); background: #edf5ef; }.approval-step.completed { border-color: #b7d2bf; background: #eef6f0; }.approval-step.skipped { opacity: .58; }.step-icon { width: 31px; height: 31px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; border-radius: 50%; color: #829087; background: #e3e9e4; }.in_progress .step-icon, .completed .step-icon { color: white; background: var(--green); }.step-copy small { color: #8a958e; font-size: 7px; letter-spacing: .08em; }.step-copy h3 { margin: 4px 0; color: var(--navy); font-size: 12px; }.step-copy p { margin: 0; color: #849088; font-size: 8px; }.step-result { margin-left: auto; max-width: 180px; text-align: right; }.step-result strong { display: block; color: var(--green); font-size: 10px; }.step-result span { display: block; margin-top: 4px; color: #718079; font-size: 8px; }.step-result p { margin: 6px 0 0; color: #849088; font-size: 8px; line-height: 1.5; }.risk-heading { margin-top: 28px; }.risk-banner { margin-top: 18px; padding: 16px 18px; display: flex; gap: 13px; border-radius: 11px; color: #a1782d; background: #f6ecd8; }.risk-banner.high { color: #b14a44; background: #f8e5e3; }.risk-banner.low { color: var(--green); background: #e5f0e8; }.risk-banner small { font-size: 8px; }.risk-banner strong { display: block; margin-top: 3px; color: var(--navy); font-size: 11px; }.risk-banner p { margin: 6px 0 0; color: #68766f; font-size: 9px; }.approval-findings { margin-top: 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }.approval-findings article { padding: 13px; border: 1px solid #e0e5e0; border-radius: 9px; background: white; }.approval-findings article > div { display: flex; justify-content: space-between; color: #75817a; font-size: 8px; }.approval-findings article b { color: var(--green); }.approval-findings article.risk b { color: #b14a44; }.approval-findings h3 { margin: 8px 0 5px; color: var(--navy); font-size: 10px; }.approval-findings p { margin: 0; color: #849088; font-size: 8px; line-height: 1.5; }
.action-panel { padding: 28px 23px; border-left: 1px solid #d8dfd8; background: #eef3ee; }.action-panel > label { margin-top: 18px; }.action-panel label small { align-self: flex-end; color: #9aa39d; font-size: 7px; }.decision-actions { margin-top: 18px; display: grid; gap: 8px; }.decision-actions button { height: 41px; display: flex; align-items: center; justify-content: center; gap: 7px; border-radius: 8px; font-size: 9px; font-weight: 700; cursor: pointer; }.decision-actions button:disabled { opacity: .5; cursor: wait; }.decision-actions .approve { border: 0; color: white; background: var(--green); }.decision-actions .return { border: 1px solid #c79c51; color: #936820; background: #f7ecd7; }.decision-actions .reject { border: 1px solid #d59a96; color: #a9443e; background: #f9e7e5; }.action-hint { margin: 13px 0 0; color: #849088; font-size: 8px; line-height: 1.6; }.final-result, .action-empty { min-height: 300px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; color: var(--green); }.final-result strong, .action-empty strong { margin-top: 12px; color: var(--navy); font-size: 14px; }.final-result p, .action-empty p { max-width: 230px; margin: 7px 0 0; color: #849088; font-size: 9px; line-height: 1.6; }.final-result.rejected, .final-result.returned { color: #a9443e; }.approval-empty-page { min-height: 380px; margin-top: 18px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 1px solid #d5ddd6; border-radius: 18px; color: #849088; background: rgba(252,253,250,.9); }.approval-empty-page strong { margin-top: 12px; color: var(--navy); }.approval-empty-page p { font-size: 9px; }.spinner { animation: spin .9s linear infinite; }@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 980px) { .approval-hero-row { align-items: flex-start; flex-direction: column; }.approval-workspace { grid-template-columns: 1fr; }.action-panel { border-left: 0; border-top: 1px solid #d8dfd8; }.approval-select-row { grid-template-columns: 1fr 1fr; }.approval-select-row label { grid-column: 1 / -1; } }
@media (max-width: 680px) { .approval-page { width: min(100% - 24px, 1360px); }.approval-hero { padding-top: 38px; }.role-summary { width: 100%; justify-content: center; }.approval-select-card, .approval-main, .action-panel { padding: 20px 17px; }.approval-select-row, .step-timeline, .approval-findings { grid-template-columns: 1fr; }.approval-heading { align-items: flex-start; flex-direction: column; } }
</style>
