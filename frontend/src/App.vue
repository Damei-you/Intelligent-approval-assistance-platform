<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  ArrowRight,
  Braces,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clipboard,
  CloudUpload,
  Database,
  FileBraces,
  FileCheck2,
  FileText,
  Fingerprint,
  Layers3,
  LoaderCircle,
  ScanText,
  ShieldCheck,
  Sparkles,
  Undo2,
  Upload,
  X,
} from '@lucide/vue'
import {
  checkHealth,
  confirmContractFile,
  getImportDetail,
  importJsonContract,
  previewContractFile,
} from './api/contracts'

const sampleJson = `{
  "contract_no": "CG-2026-001",
  "name": "办公设备采购合同",
  "contract_type_code": "PURCHASE",
  "counterparty": "示例科技有限公司",
  "amount": 120000,
  "currency": "CNY",
  "clauses": [
    {
      "clause_no": "第一条",
      "title": "合同标的",
      "content": "供应方按照采购清单提供办公设备。"
    },
    {
      "clause_no": "第二条",
      "title": "付款方式",
      "content": "设备验收合格后十个工作日内付款。"
    }
  ]
}`

const activeMode = ref('file')
const selectedFile = ref(null)
const isDragging = ref(false)
const isSubmitting = ref(false)
const errorMessage = ref('')
const result = ref(null)
const resultDetail = ref(null)
const preview = ref(null)
const previewJson = ref('')
const copiedField = ref('')
const healthStatus = ref('checking')
const fileInput = ref(null)
const jsonText = ref(sampleJson)

const form = reactive({
  contract_no: '',
  name: '',
  contract_type_code: 'PURCHASE',
  counterparty: '',
  amount: '',
  currency: 'CNY',
  document_title: '',
})

const fileExtension = computed(() => {
  const name = selectedFile.value?.name || ''
  return name.includes('.') ? name.split('.').pop().toUpperCase() : ''
})

const isJsonFile = computed(() => fileExtension.value === 'JSON')
const acceptedFile = computed(() => ['PDF', 'TXT', 'JSON'].includes(fileExtension.value))
const canSubmitFile = computed(() => {
  if (!selectedFile.value || !acceptedFile.value) return false
  if (isJsonFile.value) return true
  return Boolean(form.contract_no.trim() && form.name.trim() && form.contract_type_code)
})

const pipelineSteps = computed(() => [
  { label: '文件校验', detail: 'PDF · TXT · JSON', complete: Boolean(selectedFile.value) },
  { label: '文本解析', detail: isSubmitting.value && !preview.value ? '正在处理' : preview.value ? '解析完成' : '等待解析', complete: Boolean(preview.value) },
  { label: '条款切分', detail: preview.value ? `${preview.value.clause_count} 个条款` : '自动识别', complete: Boolean(preview.value) },
  { label: '人工确认 JSON', detail: result.value ? '已确认' : preview.value ? '等待确认' : '解析后确认', complete: Boolean(result.value), active: Boolean(preview.value && !result.value) },
  { label: '写入 PostgreSQL', detail: result.value ? `修订版本 V${result.value.revision_no}` : '确认后提交', complete: Boolean(result.value) },
])

onMounted(async () => {
  try {
    await checkHealth()
    healthStatus.value = 'online'
  } catch {
    healthStatus.value = 'offline'
  }
})

function switchMode(mode) {
  activeMode.value = mode
  errorMessage.value = ''
  result.value = null
  resultDetail.value = null
  preview.value = null
  previewJson.value = ''
}

function openFilePicker() {
  fileInput.value?.click()
}

function onFileInput(event) {
  setFile(event.target.files?.[0])
}

function onDrop(event) {
  isDragging.value = false
  setFile(event.dataTransfer.files?.[0])
}

function setFile(file) {
  errorMessage.value = ''
  result.value = null
  resultDetail.value = null
  preview.value = null
  previewJson.value = ''
  selectedFile.value = file || null
  if (!file) return
  const extension = file.name.split('.').pop()?.toUpperCase()
  if (!['PDF', 'TXT', 'JSON'].includes(extension)) {
    errorMessage.value = '仅支持 PDF、TXT 和 JSON 文件。'
  }
}

function removeFile() {
  selectedFile.value = null
  result.value = null
  resultDetail.value = null
  preview.value = null
  previewJson.value = ''
  errorMessage.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

async function submitFile() {
  if (!canSubmitFile.value || isSubmitting.value) return
  isSubmitting.value = true
  errorMessage.value = ''
  result.value = null
  resultDetail.value = null
  try {
    const metadata = isJsonFile.value ? null : { ...form }
    preview.value = await previewContractFile(selectedFile.value, metadata)
    previewJson.value = JSON.stringify(preview.value.payload, null, 2)
  } catch (error) {
    errorMessage.value = error.message || '导入失败，请检查文件内容。'
  } finally {
    isSubmitting.value = false
  }
}

async function confirmPreview() {
  if (!preview.value || !selectedFile.value || isSubmitting.value) return
  isSubmitting.value = true
  errorMessage.value = ''
  try {
    const payload = JSON.parse(previewJson.value)
    result.value = await confirmContractFile(
      selectedFile.value,
      preview.value.preview_hash,
      payload,
    )
    await refreshDetail()
  } catch (error) {
    errorMessage.value = error instanceof SyntaxError
      ? `JSON 格式错误：${error.message}`
      : error.message || '确认导入失败，请检查 JSON 内容。'
  } finally {
    isSubmitting.value = false
  }
}

function backToSource() {
  preview.value = null
  previewJson.value = ''
  result.value = null
  resultDetail.value = null
  errorMessage.value = ''
}

async function submitJson() {
  if (isSubmitting.value) return
  isSubmitting.value = true
  errorMessage.value = ''
  result.value = null
  resultDetail.value = null
  try {
    const payload = JSON.parse(jsonText.value)
    result.value = await importJsonContract(payload)
    await refreshDetail()
  } catch (error) {
    errorMessage.value = error instanceof SyntaxError
      ? `JSON 格式错误：${error.message}`
      : error.message || '导入失败，请检查 JSON 内容。'
  } finally {
    isSubmitting.value = false
  }
}

async function refreshDetail() {
  if (!result.value?.document_id) return
  try {
    resultDetail.value = await getImportDetail(result.value.document_id)
  } catch {
    resultDetail.value = null
  }
}

async function copyValue(value, field) {
  await navigator.clipboard.writeText(value)
  copiedField.value = field
  window.setTimeout(() => {
    if (copiedField.value === field) copiedField.value = ''
  }, 1400)
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" aria-label="智审台首页">
        <span class="brand-mark"><ShieldCheck :size="20" stroke-width="2.2" /></span>
        <span class="brand-name">智审台</span>
        <span class="brand-divider"></span>
        <span class="brand-product">合同审批辅助平台</span>
      </a>
      <div class="topbar-actions">
        <div class="api-status" :class="healthStatus">
          <span class="status-dot"></span>
          <span>{{ healthStatus === 'online' ? 'API 已连接' : healthStatus === 'offline' ? 'API 未连接' : '正在连接' }}</span>
        </div>
        <div class="demo-badge">演示环境</div>
        <button class="avatar" type="button" aria-label="演示用户">DE</button>
      </div>
    </header>

    <main>
      <section class="hero">
        <div class="eyebrow"><Sparkles :size="15" /> 合同数据准备</div>
        <div class="hero-row">
          <div>
            <h1>把合同，变成<br /><em>可审查的条款数据</em></h1>
            <p>导入合同正文，自动解析条款结构并安全写入知识库，为后续风险检查准备可信数据。</p>
          </div>
          <div class="hero-metrics" aria-label="能力概览">
            <div><strong>3</strong><span>支持格式</span></div>
            <div><strong>20<small>MB</small></strong><span>单文件上限</span></div>
            <div><strong>0</strong><span>当前向量数</span></div>
          </div>
        </div>
      </section>

      <section class="workspace">
        <div class="workspace-main">
          <div class="section-heading">
            <div>
              <span class="section-index">01</span>
              <h2>导入合同</h2>
            </div>
            <p>选择原始文件，或直接提交结构化条款。</p>
          </div>

          <div class="mode-tabs" role="tablist" aria-label="导入方式">
            <button
              type="button"
              role="tab"
              :aria-selected="activeMode === 'file'"
              :class="{ active: activeMode === 'file' }"
              @click="switchMode('file')"
            >
              <Upload :size="17" /> 文件导入
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="activeMode === 'json'"
              :class="{ active: activeMode === 'json' }"
              @click="switchMode('json')"
            >
              <Braces :size="17" /> JSON 条款
            </button>
          </div>

          <div v-if="activeMode === 'file'" class="mode-panel">
            <template v-if="!preview">
            <div
              class="dropzone"
              :class="{ dragging: isDragging, selected: selectedFile }"
              role="button"
              tabindex="0"
              @click="openFilePicker"
              @keydown.enter="openFilePicker"
              @keydown.space.prevent="openFilePicker"
              @dragover.prevent="isDragging = true"
              @dragleave.prevent="isDragging = false"
              @drop.prevent="onDrop"
            >
              <input ref="fileInput" type="file" accept=".pdf,.txt,.json" hidden @change="onFileInput" />
              <template v-if="!selectedFile">
                <div class="upload-icon"><CloudUpload :size="29" /></div>
                <strong>拖放合同到这里</strong>
                <span>或点击选择本地文件</span>
                <div class="format-pills"><b>PDF</b><b>TXT</b><b>JSON</b><i>最大 20 MB</i></div>
              </template>
              <template v-else>
                <div class="selected-file-icon" :class="fileExtension.toLowerCase()">
                  <FileBraces v-if="isJsonFile" :size="25" />
                  <FileText v-else :size="25" />
                </div>
                <div class="selected-file-copy">
                  <strong>{{ selectedFile.name }}</strong>
                  <span>{{ fileExtension }} · {{ formatBytes(selectedFile.size) }}</span>
                </div>
                <span class="file-ready"><Check :size="14" /> 已选择</span>
                <button class="remove-file" type="button" aria-label="移除文件" @click.stop="removeFile"><X :size="18" /></button>
              </template>
            </div>

            <div v-if="isJsonFile" class="inline-note">
              <FileBraces :size="18" />
              <div><strong>结构化 JSON 文件</strong><span>合同元数据和 clauses 条款数组将直接从文件读取。</span></div>
            </div>

            <div v-else class="metadata-form">
              <div class="form-intro">
                <h3>合同基础信息</h3>
                <span><i>*</i> 为必填项</span>
              </div>
              <div class="form-grid">
                <label>
                  <span>合同编号 <i>*</i></span>
                  <input v-model="form.contract_no" type="text" placeholder="例如：CG-2026-001" />
                </label>
                <label>
                  <span>合同名称 <i>*</i></span>
                  <input v-model="form.name" type="text" placeholder="请输入合同名称" />
                </label>
                <label>
                  <span>合同类型 <i>*</i></span>
                  <div class="select-wrap">
                    <select v-model="form.contract_type_code">
                      <option value="PURCHASE">采购合同</option>
                      <option value="SALES">销售合同</option>
                    </select>
                    <ChevronDown :size="16" />
                  </div>
                </label>
                <label>
                  <span>合同相对方</span>
                  <input v-model="form.counterparty" type="text" placeholder="请输入公司名称" />
                </label>
                <label>
                  <span>合同金额</span>
                  <div class="amount-input"><b>¥</b><input v-model="form.amount" min="0" type="number" placeholder="0.00" /></div>
                </label>
                <label>
                  <span>文档标题</span>
                  <input v-model="form.document_title" type="text" placeholder="默认使用合同名称" />
                </label>
              </div>
            </div>

            <div v-if="errorMessage" class="error-banner"><CircleAlert :size="18" /><span>{{ errorMessage }}</span></div>

            <div class="submit-row">
              <span><ShieldCheck :size="15" /> 原始文件将保存在本地演示环境</span>
              <button class="primary-button" type="button" :disabled="!canSubmitFile || isSubmitting" @click="submitFile">
                <LoaderCircle v-if="isSubmitting" class="spinner" :size="18" />
                <ScanText v-else :size="18" />
                {{ isSubmitting ? '正在解析…' : '解析为 JSON' }}
                <ArrowRight v-if="!isSubmitting" :size="17" />
              </button>
            </div>
            </template>

            <div v-else class="preview-confirmation">
              <div class="preview-banner">
                <span><CheckCircle2 :size="22" /></span>
                <div>
                  <strong>解析完成，等待确认</strong>
                  <p>已识别 {{ preview.clause_count }} 个条款，目前尚未写入数据库。</p>
                </div>
                <em>{{ preview.source_format }} · {{ formatBytes(preview.file_size) }}</em>
              </div>

              <div class="json-toolbar preview-toolbar">
                <div><FileBraces :size="18" /><strong>解析后的标准 JSON</strong></div>
                <span>{{ result ? '已确认并写入数据库' : '可以直接修改后再确认' }}</span>
              </div>
              <textarea v-model="previewJson" class="preview-editor" :readonly="Boolean(result)" aria-label="待确认的合同 JSON" spellcheck="false"></textarea>

              <div v-if="preview.warnings?.length" class="warning-list">
                <CircleAlert :size="17" />
                <span>{{ preview.warnings.join('；') }}</span>
              </div>
              <div v-if="errorMessage" class="error-banner"><CircleAlert :size="18" /><span>{{ errorMessage }}</span></div>

              <div class="submit-row confirmation-actions">
                <button class="secondary-button" type="button" :disabled="isSubmitting" @click="backToSource">
                  <Undo2 :size="16" /> {{ result ? '继续导入其他合同' : '返回修改来源' }}
                </button>
                <div>
                  <span><ShieldCheck :size="15" /> 点击确认后才会写入 PostgreSQL</span>
                  <button class="primary-button" type="button" :disabled="isSubmitting || Boolean(result)" @click="confirmPreview">
                    <LoaderCircle v-if="isSubmitting" class="spinner" :size="18" />
                    <Database v-else :size="18" />
                    {{ result ? '已经导入' : isSubmitting ? '正在导入…' : '确认并导入' }}
                    <ArrowRight v-if="!isSubmitting && !result" :size="17" />
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div v-else class="mode-panel json-panel">
            <div class="json-toolbar">
              <div><FileBraces :size="18" /><strong>结构化合同数据</strong></div>
              <span>application/json</span>
            </div>
            <textarea v-model="jsonText" aria-label="合同 JSON 内容" spellcheck="false"></textarea>
            <div v-if="errorMessage" class="error-banner"><CircleAlert :size="18" /><span>{{ errorMessage }}</span></div>
            <div class="submit-row">
              <span><Braces :size="15" /> clauses 至少包含一个有效条款</span>
              <button class="primary-button" type="button" :disabled="isSubmitting" @click="submitJson">
                <LoaderCircle v-if="isSubmitting" class="spinner" :size="18" />
                <Database v-else :size="18" />
                {{ isSubmitting ? '正在写入…' : '确认并导入' }}
                <ArrowRight v-if="!isSubmitting" :size="17" />
              </button>
            </div>
          </div>
        </div>

        <aside class="workspace-aside">
          <div class="aside-header">
            <span class="section-index">02</span>
            <div><h2>处理状态</h2><p>从原始文件到条款数据</p></div>
          </div>

          <div class="pipeline">
            <div v-for="(step, index) in pipelineSteps" :key="step.label" class="pipeline-step" :class="{ complete: step.complete, active: step.active || (isSubmitting && index === 1) }">
              <div class="step-marker">
                <Check v-if="step.complete" :size="14" />
                <span v-else>{{ index + 1 }}</span>
              </div>
              <div><strong>{{ step.label }}</strong><span>{{ step.detail }}</span></div>
            </div>
            <div class="pipeline-step future">
              <div class="step-marker"><Sparkles :size="13" /></div>
              <div><strong>生成向量</strong><span>后续阶段</span></div>
              <em>暂未启用</em>
            </div>
          </div>

          <div v-if="result" class="result-card">
            <div class="result-title">
              <span><CheckCircle2 :size="20" /></span>
              <div><strong>导入完成</strong><small>{{ result.contract_no }} · V{{ result.revision_no }}</small></div>
            </div>
            <div class="result-stats">
              <div><strong>{{ result.clause_count }}</strong><span>识别条款</span></div>
              <div><strong>{{ resultDetail?.vectorized_clause_count ?? 0 }}</strong><span>已向量化</span></div>
            </div>
            <div class="result-identifiers">
              <label>
                <span><Fingerprint :size="13" /> Document ID</span>
                <button type="button" @click="copyValue(result.document_id, 'document')">
                  <code>{{ result.document_id.slice(0, 8) }}…{{ result.document_id.slice(-6) }}</code>
                  <Check v-if="copiedField === 'document'" :size="14" />
                  <Clipboard v-else :size="14" />
                </button>
              </label>
              <label>
                <span><Layers3 :size="13" /> Contract ID</span>
                <button type="button" @click="copyValue(result.contract_id, 'contract')">
                  <code>{{ result.contract_id.slice(0, 8) }}…{{ result.contract_id.slice(-6) }}</code>
                  <Check v-if="copiedField === 'contract'" :size="14" />
                  <Clipboard v-else :size="14" />
                </button>
              </label>
            </div>
          </div>

          <div v-else-if="preview" class="confirmation-card">
            <div><FileBraces :size="25" /></div>
            <strong>JSON 等待人工确认</strong>
            <p>你可以检查或修改左侧内容。确认前不会产生合同、文档和条款记录。</p>
            <span><i></i>{{ preview.clause_count }} 个待确认条款</span>
          </div>

          <div v-else class="empty-result">
            <div><FileCheck2 :size="25" /></div>
            <strong>等待导入结果</strong>
            <p>成功导入后，这里会展示条款数量、修订版本和数据标识。</p>
          </div>

          <div class="assurance">
            <ShieldCheck :size="17" />
            <p><strong>数据边界清晰</strong><span>本阶段仅解析和入库，不调用 Embedding 模型。</span></p>
          </div>
        </aside>
      </section>
    </main>

    <footer>
      <span>Intelligent Approval Assistance Platform</span>
      <div><i></i> PostgreSQL / FastAPI <b>·</b> Vue 3</div>
    </footer>
  </div>
</template>
