<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Bot,
  Check,
  CheckCircle2,
  CircleAlert,
  FileText,
  FileSearch,
  LoaderCircle,
  MessageCircle,
  RefreshCw,
  Scale,
  Send,
  ShieldAlert,
  Sparkles,
  User,
  X,
} from '@lucide/vue'
import {
  createRiskChatSession,
  createRiskReview,
  getRiskChatSession,
  getRiskReview,
  listReviewContracts,
  sendRiskChatMessage,
} from '../api/contracts'

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
const activeChatFinding = ref(null)
const chatSession = ref(null)
const chatLoading = ref(false)
const chatSending = ref(false)
const chatError = ref('')
const chatInput = ref('')
const chatIntent = ref('AUTO')
const chatRetryRequest = ref(null)
const chatMessagesElement = ref(null)
const chatInputElement = ref(null)
const chatDialogElement = ref(null)
let pollingTimer = null
let chatPollingTimer = null
let chatOpenSequence = 0
let reviewRequestSequence = 0
let chatTriggerElement = null
let bodyScrollLocked = false
let previousBodyOverflow = ''

const QUICK_CHAT_ACTIONS = [
  {
    intent: 'EXPLAIN',
    label: '解释风险结论',
    description: '说明风险为何成立，以及结论如何得出',
    prompt: '请解释这一项风险结论，并结合已有证据说明判断依据。',
  },
  {
    intent: 'EVIDENCE_QUERY',
    label: '查询条款与制度',
    description: '查找相关合同条款和企业制度依据',
    prompt: '请列出与这一风险相关的合同条款和制度依据，并说明它们之间的关系。',
  },
  {
    intent: 'DRAFT_CLAUSE',
    label: '生成修改草案',
    description: '根据风险建议起草可供人工确认的条款',
    prompt: '请根据这一风险的修改建议，生成一份条款修改草案。',
  },
]

const selectedContract = computed(() => (
  contracts.value.find((item) => item.contract_id === selectedContractId.value)
))
const isRunning = computed(() => ['PENDING', 'RUNNING'].includes(review.value?.status))
const riskCount = computed(() => review.value?.findings?.filter((item) => item.status === 'RISK').length || 0)
const insufficientCount = computed(() => review.value?.findings?.filter((item) => item.status === 'INSUFFICIENT_INFORMATION').length || 0)
const chatMessages = computed(() => chatSession.value?.messages || [])
const chatFinding = computed(() => chatSession.value?.finding || activeChatFinding.value || {})
const chatHasPending = computed(() => (
  chatMessages.value.some((message) => message.role?.toUpperCase() === 'ASSISTANT' && message.status === 'PENDING')
))
const chatControlsDisabled = computed(() => (
  chatLoading.value || chatSending.value || chatHasPending.value || !chatSession.value
))

onMounted(() => {
  loadContracts()
  window.addEventListener('keydown', handleGlobalKeydown)
})
onBeforeUnmount(() => {
  stopPolling()
  stopChatPolling()
  unlockPageScroll()
  window.removeEventListener('keydown', handleGlobalKeydown)
})
watch(selectedContractId, () => {
  closeChat()
  loadLatestReview()
})

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
  const sequence = ++reviewRequestSequence
  const contractId = selectedContractId.value
  closeChat()
  stopPolling()
  review.value = null
  errorMessage.value = ''
  const latestReviewRunId = selectedContract.value?.latest_review_run_id
  if (!latestReviewRunId) return
  await refreshReview(latestReviewRunId, contractId, sequence)
  if (
    sequence === reviewRequestSequence
    && contractId === selectedContractId.value
    && ['PENDING', 'RUNNING'].includes(review.value?.status)
  ) {
    startReviewPolling(latestReviewRunId, contractId, sequence)
  }
}

async function startReview() {
  if (!selectedContract.value?.review_ready || starting.value) return
  const sequence = ++reviewRequestSequence
  const contractId = selectedContractId.value
  closeChat()
  stopPolling()
  starting.value = true
  review.value = null
  errorMessage.value = ''
  try {
    const created = await createRiskReview(contractId)
    if (sequence !== reviewRequestSequence || contractId !== selectedContractId.value) return
    const contract = contracts.value.find((item) => item.contract_id === contractId)
    if (contract) {
      contract.latest_review_run_id = created.review_run_id
      contract.latest_review_status = 'PENDING'
      contract.latest_review_is_current = true
    }
    await refreshReview(created.review_run_id, contractId, sequence)
    if (
      sequence === reviewRequestSequence
      && contractId === selectedContractId.value
      && ['PENDING', 'RUNNING'].includes(review.value?.status)
    ) {
      startReviewPolling(created.review_run_id, contractId, sequence)
    }
  } catch (error) {
    if (sequence === reviewRequestSequence && contractId === selectedContractId.value) {
      errorMessage.value = error.message || '风险审查任务创建失败。'
    }
  } finally {
    starting.value = false
  }
}

async function refreshReview(
  reviewRunId = review.value?.review_run_id,
  contractId = selectedContractId.value,
  sequence = reviewRequestSequence,
) {
  if (!reviewRunId) return
  try {
    const loadedReview = await getRiskReview(reviewRunId)
    if (sequence !== reviewRequestSequence || contractId !== selectedContractId.value) return
    review.value = loadedReview
    if (!['PENDING', 'RUNNING'].includes(loadedReview.status)) stopPolling()
  } catch (error) {
    if (sequence !== reviewRequestSequence || contractId !== selectedContractId.value) return
    stopPolling()
    errorMessage.value = error.message || '审查进度查询失败。'
  }
}

function startReviewPolling(reviewRunId, contractId, sequence) {
  stopPolling()
  pollingTimer = window.setInterval(
    () => refreshReview(reviewRunId, contractId, sequence),
    1500,
  )
}

function stopPolling() {
  if (pollingTimer !== null) {
    window.clearInterval(pollingTimer)
    pollingTimer = null
  }
}

async function openChat(finding, event = null) {
  const sequence = ++chatOpenSequence
  stopChatPolling()
  if (event?.currentTarget) chatTriggerElement = event.currentTarget
  activeChatFinding.value = finding
  lockPageScroll()
  chatSession.value = null
  chatLoading.value = true
  chatSending.value = false
  chatError.value = ''
  chatInput.value = ''
  chatIntent.value = 'AUTO'
  chatRetryRequest.value = null
  await nextTick()
  chatDialogElement.value?.focus()
  try {
    const session = await createRiskChatSession(finding.id)
    if (sequence !== chatOpenSequence) return
    chatSession.value = session
    syncChatPolling(session.session_id, sequence)
    await scrollChatToBottom()
    if (!chatHasPending.value) chatInputElement.value?.focus()
  } catch (error) {
    if (sequence !== chatOpenSequence) return
    chatError.value = error.message || '对话加载失败，请稍后重试。'
  } finally {
    if (sequence === chatOpenSequence) chatLoading.value = false
  }
}

function closeChat() {
  const trigger = chatTriggerElement
  chatOpenSequence += 1
  stopChatPolling()
  activeChatFinding.value = null
  chatSession.value = null
  chatLoading.value = false
  chatSending.value = false
  chatError.value = ''
  chatInput.value = ''
  chatIntent.value = 'AUTO'
  chatRetryRequest.value = null
  chatTriggerElement = null
  unlockPageScroll()
  nextTick(() => {
    if (trigger?.isConnected) trigger.focus()
  })
}

function handleGlobalKeydown(event) {
  if (!activeChatFinding.value) return
  if (event.key === 'Escape') {
    closeChat()
    return
  }
  if (event.key === 'Tab') trapChatFocus(event)
}

function trapChatFocus(event) {
  const dialog = chatDialogElement.value
  if (!dialog) return
  const focusable = Array.from(dialog.querySelectorAll(
    'button:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hasAttribute('hidden'))
  if (!focusable.length) {
    event.preventDefault()
    dialog.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (document.activeElement === dialog || !dialog.contains(document.activeElement)) {
    event.preventDefault()
    const target = event.shiftKey ? last : first
    target.focus()
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function retryOpenChat() {
  if (activeChatFinding.value) openChat(activeChatFinding.value)
}

function stopChatPolling() {
  if (chatPollingTimer !== null) {
    window.clearTimeout(chatPollingTimer)
    chatPollingTimer = null
  }
}

function syncChatPolling(sessionId, sequence) {
  if (!chatHasPending.value) {
    stopChatPolling()
    return
  }
  if (chatPollingTimer !== null) return
  // 下一次查询只在上一次结束后安排，避免多个 GET 乱序覆盖较新的消息快照。
  chatPollingTimer = window.setTimeout(async () => {
    chatPollingTimer = null
    await refreshChatSession(sessionId, sequence)
    if (
      sequence === chatOpenSequence
      && chatSession.value?.session_id === sessionId
      && chatHasPending.value
    ) {
      syncChatPolling(sessionId, sequence)
    }
  }, 1200)
}

async function refreshChatSession(sessionId, sequence) {
  try {
    const refreshed = await getRiskChatSession(sessionId)
    if (sequence !== chatOpenSequence || chatSession.value?.session_id !== sessionId) return
    chatSession.value = refreshed
    chatError.value = ''
    if (chatHasPending.value) {
      syncChatPolling(sessionId, sequence)
    } else {
      stopChatPolling()
      if (
        chatRetryRequest.value
        && chatInput.value.trim() === chatRetryRequest.value.content
      ) {
        chatInput.value = ''
      }
      chatRetryRequest.value = null
      await scrollChatToBottom()
      chatInputElement.value?.focus()
    }
  } catch (error) {
    if (sequence !== chatOpenSequence) return
    chatError.value = error.message || '对话状态刷新失败，请稍后重试。'
    if (!chatHasPending.value) stopChatPolling()
  }
}

async function askQuickQuestion(action) {
  await submitChatMessage(action.prompt, action.intent, false)
}

async function submitComposer() {
  await submitChatMessage(chatInput.value, chatIntent.value, true)
}

async function submitChatMessage(content, intent, clearComposer) {
  const normalizedContent = content.trim()
  const sessionId = chatSession.value?.session_id
  if (!normalizedContent || !sessionId || chatSending.value || chatHasPending.value) return

  const sequence = chatOpenSequence
  const retryRequest = chatRetryRequest.value
  const clientRequestId = (
    retryRequest?.content === normalizedContent && retryRequest?.intent === intent
      ? retryRequest.client_request_id
      : createClientRequestId()
  )
  chatSending.value = true
  chatError.value = ''
  if (clearComposer) chatInput.value = ''
  try {
    const response = await sendRiskChatMessage(sessionId, {
      content: normalizedContent,
      intent,
      client_request_id: clientRequestId,
    })
    if (sequence !== chatOpenSequence || chatSession.value?.session_id !== sessionId) return

    const returnedMessages = [response.user_message, response.assistant_message]
      .filter(Boolean)
    chatSession.value = {
      ...chatSession.value,
      messages: upsertMessages(chatMessages.value, returnedMessages),
    }
    chatRetryRequest.value = null
    syncChatPolling(sessionId, sequence)
    await scrollChatToBottom()
    if (!chatHasPending.value) chatInputElement.value?.focus()
  } catch (error) {
    if (sequence !== chatOpenSequence) return
    // 网络中断或服务端仍在处理同一请求时复用 UUID，避免“响应丢失”造成重复消息。
    // 明确的生成失败则允许下次使用新 UUID 重新生成。
    chatRetryRequest.value = (!error.status || error.status === 409)
      ? { content: normalizedContent, intent, client_request_id: clientRequestId }
      : null
    chatError.value = error.status === 409
      ? '该问题仍在生成，正在自动刷新结果。'
      : (error.message || '消息发送失败，请稍后重试。')
    if (error.status === 409) {
      await refreshChatSession(sessionId, sequence)
      syncChatPolling(sessionId, sequence)
    }
    if (
      clearComposer
      && !chatInput.value
      && (error.status !== 409 || chatRetryRequest.value)
    ) {
      chatInput.value = normalizedContent
    }
  } finally {
    if (sequence === chatOpenSequence) chatSending.value = false
  }
}

function upsertMessages(existingMessages, returnedMessages) {
  const replacements = new Map(returnedMessages.map((message) => [message.id, message]))
  const merged = existingMessages.map((message) => replacements.get(message.id) || message)
  const existingIds = new Set(existingMessages.map((message) => message.id))
  for (const message of returnedMessages) {
    if (!existingIds.has(message.id)) merged.push(message)
  }
  return merged
}

function lockPageScroll() {
  if (bodyScrollLocked) return
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  bodyScrollLocked = true
}

function unlockPageScroll() {
  if (!bodyScrollLocked) return
  document.body.style.overflow = previousBodyOverflow
  bodyScrollLocked = false
}

function handleComposerKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    submitComposer()
  }
}

function createClientRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (character) => {
    const randomValue = Math.floor(Math.random() * 16)
    const value = character === 'x' ? randomValue : (randomValue & 0x3) | 0x8
    return value.toString(16)
  })
}

async function scrollChatToBottom() {
  await nextTick()
  if (chatMessagesElement.value) {
    chatMessagesElement.value.scrollTop = chatMessagesElement.value.scrollHeight
  }
}

function draftFor(message) {
  const output = message.structured_output
  const draft = output?.draft || output
  return draft?.proposed_text ? draft : null
}

function evidenceTypeText(value) {
  return value === 'POLICY' ? '制度依据' : '合同条款'
}

function formatMessageTime(value) {
  if (!value) return ''
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return ''
  return timestamp.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
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
          <select v-model="selectedContractId" :disabled="loadingContracts || starting || isRunning">
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
          <div v-for="(check, index) in CHECKS" :key="check.code" class="check-step" :class="findingFor(check.code)?.status?.toLowerCase()">
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
            <div class="finding-chat-entry">
              <div><MessageCircle :size="17" /><span><b>对结论还有疑问？</b><small>解释依据、查询条款，或生成修改草案</small></span></div>
              <button type="button" :aria-label="`就${finding.check_name}风险继续询问`" :disabled="review.status !== 'SUCCEEDED'" @click="openChat(finding, $event)">
                就此风险继续询问<ArrowRight :size="15" />
              </button>
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

    <Teleport to="body">
      <div v-if="activeChatFinding" class="chat-overlay" @click.self="closeChat">
        <aside ref="chatDialogElement" class="chat-drawer" role="dialog" aria-modal="true" aria-labelledby="risk-chat-title" tabindex="-1" :aria-busy="chatLoading || chatSending || chatHasPending">
          <header class="chat-header">
            <div class="chat-heading-icon"><MessageCircle :size="20" /></div>
            <div>
              <span>风险项多轮询问</span>
              <h2 id="risk-chat-title">{{ chatFinding.check_name || activeChatFinding.check_name }}</h2>
              <p>{{ chatFinding.title || activeChatFinding.title }}</p>
            </div>
            <button class="chat-close" type="button" aria-label="关闭风险对话" @click="closeChat"><X :size="20" /></button>
          </header>

          <div class="chat-context-strip">
            <span :class="(chatFinding.status || activeChatFinding.status || '').toLowerCase()">
              {{ statusText(chatFinding.status || activeChatFinding.status) }}
            </span>
            <p>{{ chatFinding.description || activeChatFinding.description }}</p>
          </div>

          <section class="chat-quick-actions" aria-label="快捷提问">
            <button
              v-for="action in QUICK_CHAT_ACTIONS"
              :key="action.intent"
              type="button"
              :disabled="chatControlsDisabled"
              @click="askQuickQuestion(action)"
            >
              <Scale v-if="action.intent === 'EXPLAIN'" :size="16" />
              <BookOpen v-else-if="action.intent === 'EVIDENCE_QUERY'" :size="16" />
              <FileText v-else :size="16" />
              <span><b>{{ action.label }}</b><small>{{ action.description }}</small></span>
            </button>
          </section>

          <div ref="chatMessagesElement" class="chat-messages" aria-live="polite">
            <div v-if="chatLoading" class="chat-state">
              <LoaderCircle class="spinner" :size="25" /><strong>正在加载对话</strong><p>正在绑定当前风险项与审查证据…</p>
            </div>
            <div v-else-if="chatError && !chatSession" class="chat-state error">
              <CircleAlert :size="25" /><strong>对话暂时无法打开</strong><p>{{ chatError }}</p>
              <button type="button" @click="retryOpenChat"><RefreshCw :size="14" />重新加载</button>
            </div>
            <div v-else-if="!chatMessages.length" class="chat-state">
              <Bot :size="28" /><strong>围绕本项风险继续追问</strong>
              <p>选择上方快捷问题，或直接输入你想了解的内容。回答会保留可追溯引用。</p>
            </div>

            <article
              v-for="message in chatMessages"
              :key="message.id"
              class="chat-message"
              :class="[
                message.role?.toUpperCase() === 'USER' ? 'user' : 'assistant',
                message.status?.toLowerCase(),
              ]"
            >
              <span class="message-avatar">
                <User v-if="message.role?.toUpperCase() === 'USER'" :size="15" />
                <Bot v-else :size="16" />
              </span>
              <div class="message-body">
                <div class="message-meta">
                  <b>{{ message.role?.toUpperCase() === 'USER' ? '你' : '审批辅助智能体' }}</b>
                  <time>{{ formatMessageTime(message.created_at) }}</time>
                </div>
                <div class="message-bubble"><p>{{ message.content }}</p></div>

                <section v-if="draftFor(message)" class="clause-draft">
                  <header><FileText :size="15" /><b>条款修改草案</b><span>需人工确认</span></header>
                  <div class="draft-comparison">
                    <div><small>原条款</small><p>{{ draftFor(message).original_text || '未提供原条款文本' }}</p></div>
                    <div class="proposed"><small>建议草案</small><p>{{ draftFor(message).proposed_text }}</p></div>
                  </div>
                  <dl v-if="draftFor(message).change_summary || draftFor(message).rationale">
                    <template v-if="draftFor(message).change_summary"><dt>修改摘要</dt><dd>{{ draftFor(message).change_summary }}</dd></template>
                    <template v-if="draftFor(message).rationale"><dt>修改理由</dt><dd>{{ draftFor(message).rationale }}</dd></template>
                  </dl>
                  <ul v-if="draftFor(message).warnings?.length" class="draft-warnings">
                    <li v-for="warning in draftFor(message).warnings" :key="warning">{{ warning }}</li>
                  </ul>
                </section>

                <details v-if="message.citations?.length" class="message-citations">
                  <summary><BookOpen :size="14" />查看引用依据（{{ message.citations.length }}）</summary>
                  <blockquote v-for="(citation, citationIndex) in message.citations" :key="`${message.id}-${citation.citation_label}-${citationIndex}`">
                    <header>
                      <b>{{ citation.citation_label }}</b>
                      <span>{{ evidenceTypeText(citation.evidence_type) }}</span>
                    </header>
                    <strong>{{ citation.document_title }}<template v-if="citation.clause_no || citation.title"> · {{ citation.clause_no }} {{ citation.title }}</template></strong>
                    <p>{{ citation.cited_text }}</p>
                  </blockquote>
                </details>
              </div>
            </article>

            <div v-if="chatSending || chatHasPending" class="assistant-thinking"><LoaderCircle class="spinner" :size="15" />正在结合本项风险与证据生成回答…</div>
          </div>

          <div v-if="chatError && chatSession" class="chat-inline-error" role="alert" aria-live="assertive"><CircleAlert :size="15" />{{ chatError }}</div>

          <form class="chat-composer" @submit.prevent="submitComposer">
            <label>
              <span>提问类型</span>
              <select v-model="chatIntent" :disabled="chatControlsDisabled">
                <option value="AUTO">自动识别</option>
                <option value="EXPLAIN">解释结论</option>
                <option value="EVIDENCE_QUERY">查询依据</option>
                <option value="DRAFT_CLAUSE">修改草案</option>
              </select>
            </label>
            <div class="composer-input">
              <textarea
                ref="chatInputElement"
                v-model="chatInput"
                rows="2"
                maxlength="2000"
                aria-label="输入风险追问内容"
                placeholder="继续追问，例如：为什么预付款比例被判定为高风险？"
                :disabled="chatControlsDisabled"
                @keydown="handleComposerKeydown"
              ></textarea>
              <button type="submit" aria-label="发送消息" :disabled="!chatInput.trim() || chatControlsDisabled">
                <LoaderCircle v-if="chatSending || chatHasPending" class="spinner" :size="17" /><Send v-else :size="17" />
              </button>
            </div>
            <p>Enter 发送，Shift + Enter 换行。修改草案不会直接写入合同。</p>
          </form>
        </aside>
      </div>
    </Teleport>
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
.finding-chat-entry { margin-top: 14px; padding-top: 13px; display: flex; align-items: center; justify-content: space-between; gap: 14px; border-top: 1px solid #e6ebe6; }.finding-chat-entry > div { display: flex; align-items: center; gap: 8px; color: var(--green); }.finding-chat-entry span { display: flex; flex-direction: column; gap: 2px; }.finding-chat-entry b { color: var(--navy); font-size: 9px; }.finding-chat-entry small { color: #87928b; font-size: 8px; }.finding-chat-entry button { padding: 8px 11px; display: flex; align-items: center; gap: 6px; border: 1px solid #acc6b4; border-radius: 8px; color: var(--green); background: #f3f8f4; font-size: 9px; font-weight: 700; cursor: pointer; }.finding-chat-entry button:hover:not(:disabled) { border-color: var(--green); background: #e5f0e8; }.finding-chat-entry button:disabled { cursor: not-allowed; opacity: .5; }
.review-empty { min-height: 290px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #86928a; }.review-empty strong { margin-top: 12px; color: var(--navy); font-size: 12px; }.review-empty p { margin: 7px 0; font-size: 9px; }
.review-summary { padding: 28px 23px; border-left: 1px solid #d8dfd8; background: #eef3ee; }.risk-orb { width: 132px; height: 132px; margin: 28px auto 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 1px solid #cdd9d0; border-radius: 50%; color: #718178; background: rgba(255,255,255,.65); }.risk-orb strong { margin-top: 6px; color: var(--navy); font-size: 15px; }.risk-orb span { margin-top: 3px; font-size: 8px; }.risk-orb.high { color: #b74f47; border-color: #e0aca8; background: #f8e6e4; }.risk-orb.medium { color: #a77b23; border-color: #e6cf93; background: #f8f0da; }.risk-orb.low { color: var(--green); border-color: #a9c8b3; background: #e1efe6; }
.summary-stats { display: grid; grid-template-columns: repeat(3, 1fr); border: 1px solid #d7dfd8; border-radius: 9px; overflow: hidden; }.summary-stats div { padding: 11px 5px; display: flex; flex-direction: column; align-items: center; gap: 3px; background: rgba(255,255,255,.52); }.summary-stats div + div { border-left: 1px solid #d7dfd8; }.summary-stats strong { color: var(--navy); font-size: 14px; }.summary-stats span { color: #849087; font-size: 8px; }.approval-advice { margin-top: 14px; padding: 14px; border-radius: 10px; color: white; background: var(--green); }.approval-advice span { font-size: 8px; opacity: .72; }.approval-advice strong { margin-top: 4px; display: block; font-size: 13px; }.approval-advice p { margin: 8px 0 0; font-size: 8px; line-height: 1.6; opacity: .78; }.summary-note { margin-top: 14px; padding: 12px; display: flex; gap: 8px; color: var(--green); background: #dde9e1; border-radius: 9px; }.summary-note p { margin: 0; display: flex; flex-direction: column; gap: 3px; }.summary-note b { font-size: 9px; }.summary-note span { color: #687c70; font-size: 8px; line-height: 1.5; }

.chat-overlay { position: fixed; inset: 0; z-index: 1000; display: flex; justify-content: flex-end; overscroll-behavior: contain; background: rgba(19, 33, 27, .42); backdrop-filter: blur(2px); }
.chat-drawer { width: min(660px, 100vw); height: 100dvh; display: flex; flex-direction: column; overflow: hidden; outline: none; color: #34463d; background: #fbfcfa; box-shadow: -24px 0 70px rgba(15, 35, 25, .18); animation: chat-drawer-in .22s ease-out; }
@keyframes chat-drawer-in { from { opacity: .4; transform: translateX(36px); } to { opacity: 1; transform: translateX(0); } }
.chat-header { padding: 20px 22px 17px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #dce4dd; background: #f3f7f3; }.chat-heading-icon { width: 39px; height: 39px; display: grid; flex: 0 0 auto; place-items: center; border-radius: 11px; color: white; background: var(--green); }.chat-header > div:nth-child(2) { min-width: 0; flex: 1; }.chat-header span { color: #7d8c83; font-size: 9px; font-weight: 700; letter-spacing: .04em; }.chat-header h2 { margin: 2px 0 0; color: var(--navy); font-size: 15px; }.chat-header p { margin: 3px 0 0; overflow: hidden; color: #6c7a72; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }.chat-close { width: 36px; height: 36px; display: grid; flex: 0 0 auto; place-items: center; border: 0; border-radius: 9px; color: #64736b; background: transparent; cursor: pointer; }.chat-close:hover { color: var(--navy); background: #e3ebe5; }
.chat-context-strip { padding: 11px 22px; display: flex; align-items: flex-start; gap: 9px; border-bottom: 1px solid #e2e8e3; background: white; }.chat-context-strip > span { padding: 4px 7px; flex: 0 0 auto; border-radius: 6px; color: #8e443e; background: #fae9e7; font-size: 8px; font-weight: 700; }.chat-context-strip > span.pass { color: var(--green); background: #e4f0e8; }.chat-context-strip > span.insufficient_information { color: #876526; background: #f7edda; }.chat-context-strip p { margin: 1px 0 0; display: -webkit-box; overflow: hidden; color: #68766e; font-size: 9px; line-height: 1.55; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.chat-quick-actions { padding: 12px 22px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; border-bottom: 1px solid #dfe6e0; background: #f7f9f7; }.chat-quick-actions button { min-width: 0; padding: 10px; display: flex; align-items: flex-start; gap: 7px; border: 1px solid #d8e2da; border-radius: 9px; color: var(--green); text-align: left; background: white; cursor: pointer; }.chat-quick-actions button:hover:not(:disabled) { border-color: #9fbea9; background: #eef6f0; }.chat-quick-actions button:disabled { cursor: not-allowed; opacity: .5; }.chat-quick-actions button > svg { margin-top: 1px; flex: 0 0 auto; }.chat-quick-actions span { min-width: 0; display: flex; flex-direction: column; gap: 3px; }.chat-quick-actions b { color: var(--navy); font-size: 9px; }.chat-quick-actions small { color: #859088; font-size: 7px; line-height: 1.4; }
.chat-messages { min-height: 0; padding: 20px 22px; flex: 1; overflow-y: auto; scroll-behavior: smooth; }.chat-state { min-height: 220px; padding: 25px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #789084; text-align: center; }.chat-state strong { margin-top: 10px; color: var(--navy); font-size: 13px; }.chat-state p { max-width: 330px; margin: 6px 0 0; color: #7c8982; font-size: 10px; line-height: 1.6; }.chat-state.error { color: #ad514a; }.chat-state button { margin-top: 12px; padding: 7px 10px; display: flex; align-items: center; gap: 5px; border: 1px solid #e0b8b4; border-radius: 7px; color: #9b403b; background: #fff5f4; cursor: pointer; }
.chat-message { margin-bottom: 18px; display: flex; align-items: flex-start; gap: 9px; }.chat-message.user { flex-direction: row-reverse; }.chat-message.pending .message-bubble { opacity: .72; }.chat-message.failed .message-bubble { border-color: #e5bbb7; color: #8f403b; background: #fff4f2; }.message-avatar { width: 30px; height: 30px; display: grid; flex: 0 0 auto; place-items: center; border-radius: 50%; color: var(--green); background: #e4efe7; }.chat-message.user .message-avatar { color: #4b6580; background: #e7edf2; }.message-body { min-width: 0; max-width: calc(100% - 42px); }.chat-message.user .message-body { display: flex; flex-direction: column; align-items: flex-end; }.message-meta { margin: 0 2px 5px; display: flex; align-items: center; gap: 8px; }.message-meta b { color: #52655a; font-size: 9px; }.message-meta time { color: #9aa39d; font-size: 8px; }.message-bubble { padding: 10px 12px; border: 1px solid #dce5de; border-radius: 3px 11px 11px 11px; background: white; box-shadow: 0 4px 13px rgba(33, 53, 42, .04); }.chat-message.user .message-bubble { border-color: #b7cdbc; border-radius: 11px 3px 11px 11px; color: white; background: var(--green); }.message-bubble p { margin: 0; font-size: 11px; line-height: 1.75; white-space: pre-wrap; word-break: break-word; }
.clause-draft { width: 100%; margin-top: 9px; overflow: hidden; border: 1px solid #cfdcd2; border-radius: 10px; background: white; }.clause-draft > header { padding: 9px 11px; display: flex; align-items: center; gap: 6px; color: var(--green); background: #edf5ef; }.clause-draft > header b { color: var(--navy); font-size: 9px; }.clause-draft > header span { margin-left: auto; padding: 3px 6px; border-radius: 9px; color: #8a681f; background: #f5e7bc; font-size: 7px; }.draft-comparison { display: grid; grid-template-columns: 1fr 1fr; }.draft-comparison > div { padding: 11px; background: #fafbfa; }.draft-comparison > div + div { border-left: 1px solid #e0e6e1; }.draft-comparison > div.proposed { background: #f2f7f3; }.draft-comparison small { color: #8a958e; font-size: 8px; font-weight: 700; }.draft-comparison p { margin: 6px 0 0; color: #59675f; font-size: 9px; line-height: 1.65; white-space: pre-wrap; }.draft-comparison .proposed p { color: #345844; }.clause-draft dl { margin: 0; padding: 9px 11px; display: grid; grid-template-columns: auto 1fr; gap: 5px 9px; border-top: 1px solid #e3e8e4; font-size: 8px; }.clause-draft dt { color: #748179; font-weight: 700; }.clause-draft dd { margin: 0; color: #59675f; line-height: 1.5; }.draft-warnings { margin: 0; padding: 8px 11px 8px 27px; border-top: 1px solid #eadab7; color: #8e6828; background: #fff9eb; font-size: 8px; line-height: 1.5; }
.message-citations { width: 100%; margin-top: 8px; border: 1px solid #dce5de; border-radius: 8px; background: #f7f9f7; }.message-citations summary { padding: 8px 10px; display: flex; align-items: center; gap: 6px; color: var(--green); font-size: 8px; font-weight: 700; cursor: pointer; list-style: none; }.message-citations summary::-webkit-details-marker { display: none; }.message-citations blockquote { margin: 0 8px 8px; padding: 9px; border: 1px solid #e0e6e1; border-radius: 7px; background: white; }.message-citations blockquote header { display: flex; align-items: center; gap: 5px; }.message-citations blockquote header b { padding: 2px 5px; border-radius: 5px; color: white; background: var(--green); font-size: 7px; }.message-citations blockquote header span { color: #809087; font-size: 7px; }.message-citations blockquote > strong { margin-top: 5px; display: block; color: #3f5549; font-size: 8px; }.message-citations blockquote p { margin: 5px 0 0; color: #66736c; font-size: 8px; line-height: 1.55; }.assistant-thinking { margin: 2px 0 10px 39px; display: flex; align-items: center; gap: 6px; color: #71837a; font-size: 9px; }
.chat-inline-error { padding: 8px 22px; display: flex; align-items: center; gap: 6px; border-top: 1px solid #efcbc8; color: #9b403b; background: #fff2f0; font-size: 9px; }
.chat-composer { padding: 12px 18px 15px; border-top: 1px solid #d8e1da; background: white; }.chat-composer > label { margin-bottom: 7px; display: flex; align-items: center; gap: 7px; }.chat-composer > label span { color: #7c8881; font-size: 8px; font-weight: 700; }.chat-composer select { padding: 4px 7px; border: 1px solid #d5dfd7; border-radius: 6px; outline: none; color: #4b5f54; background: #f8faf8; font-size: 8px; }.composer-input { display: flex; align-items: flex-end; gap: 8px; }.composer-input textarea { min-height: 58px; max-height: 130px; padding: 10px 11px; flex: 1; resize: vertical; border: 1px solid #cfdad1; border-radius: 10px; outline: none; color: var(--navy); background: #fbfcfb; font-family: inherit; font-size: 10px; line-height: 1.55; }.composer-input textarea:focus { border-color: #82ad91; box-shadow: 0 0 0 3px rgba(59, 111, 77, .09); }.composer-input textarea:disabled { cursor: not-allowed; opacity: .6; }.composer-input button { width: 39px; height: 39px; display: grid; flex: 0 0 auto; place-items: center; border: 0; border-radius: 9px; color: white; background: var(--green); cursor: pointer; }.composer-input button:disabled { cursor: not-allowed; opacity: .45; }.chat-composer > p { margin: 6px 2px 0; color: #929c96; font-size: 7px; }
@media (max-width: 980px) { .review-hero-row { align-items: flex-start; flex-direction: column; }.review-dashboard { grid-template-columns: 1fr; }.review-summary { border-left: 0; border-top: 1px solid #d8dfd8; }.contract-picker-row { grid-template-columns: 1fr 1fr; }.contract-select { grid-column: 1 / -1; } }
@media (max-width: 680px) { .review-page { width: min(100% - 24px, 1360px); }.review-hero { padding-top: 38px; }.agent-chip { width: 100%; }.review-launch-card, .review-main-column, .review-summary { padding: 20px 17px; }.contract-picker-row, .check-overview, .evidence-grid, .candidate-grid { grid-template-columns: 1fr; }.review-start-button, .contract-readiness { width: 100%; }.review-section-heading { align-items: flex-start; flex-direction: column; }.finding-header { align-items: flex-start; flex-wrap: wrap; }.finding-badges { margin-left: 45px; }.finding-chat-entry { align-items: stretch; flex-direction: column; }.finding-chat-entry button { justify-content: center; }.chat-header { padding: calc(15px + env(safe-area-inset-top)) 15px 15px; }.chat-context-strip { padding: 9px 15px; }.chat-quick-actions { padding: 9px 15px; grid-template-columns: 1fr; }.chat-quick-actions button { align-items: center; }.chat-quick-actions small { display: none; }.chat-messages { padding: 16px 14px; }.chat-composer { padding: 10px 12px calc(12px + env(safe-area-inset-bottom)); }.chat-composer select, .composer-input textarea { font-size: 16px; }.draft-comparison { grid-template-columns: 1fr; }.draft-comparison > div + div { border-top: 1px solid #e0e6e1; border-left: 0; } }
</style>
