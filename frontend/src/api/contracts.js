const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, options)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    const message = payload?.message || detail?.message || detail?.[0]?.msg || '请求失败，请稍后重试。'
    throw new ApiError(message, response.status, payload?.code || detail?.code)
  }
  return payload
}

export function checkHealth() {
  return request('/health')
}

export function importJsonContract(payload) {
  return request('/api/v1/contracts/imports/json', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function previewContractFile(file, metadata) {
  const formData = new FormData()
  formData.append('file', file)
  if (metadata) {
    Object.entries(metadata).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) {
        formData.append(key, value)
      }
    })
  }
  return request('/api/v1/contracts/imports/preview/file', {
    method: 'POST',
    body: formData,
  })
}

export function confirmContractFile(file, previewHash, payload) {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('preview_hash', previewHash)
  formData.append('payload', JSON.stringify(payload))
  return request('/api/v1/contracts/imports/confirm/file', {
    method: 'POST',
    body: formData,
  })
}

export function getImportDetail(documentId) {
  return request(`/api/v1/contracts/imports/${documentId}`)
}

export function getVectorizationStatus(documentId) {
  return request(`/api/v1/contracts/imports/${documentId}/vectorization`)
}

export function importJsonPolicy(payload) {
  return request('/api/v1/policies/imports/json', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function previewPolicyFile(file, metadata) {
  const formData = new FormData()
  formData.append('file', file)
  if (metadata) {
    Object.entries(metadata).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) formData.append(key, value)
    })
  }
  return request('/api/v1/policies/imports/preview/file', {
    method: 'POST',
    body: formData,
  })
}

export function confirmPolicyFile(file, previewHash, payload) {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('preview_hash', previewHash)
  formData.append('payload', JSON.stringify(payload))
  return request('/api/v1/policies/imports/confirm/file', {
    method: 'POST',
    body: formData,
  })
}

export function getPolicyImportDetail(documentId) {
  return request(`/api/v1/policies/imports/${documentId}`)
}

export function getPolicyVectorizationStatus(documentId) {
  return request(`/api/v1/policies/imports/${documentId}/vectorization`)
}

export function listReviewContracts() {
  return request('/api/v1/risk-reviews/contracts')
}

export function createRiskReview(contractId) {
  return request('/api/v1/risk-reviews', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contract_id: contractId }),
  })
}

export function getRiskReview(reviewRunId) {
  return request(`/api/v1/risk-reviews/${reviewRunId}`)
}
