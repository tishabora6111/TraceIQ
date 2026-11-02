// Mirrors backend/app/modules/health/schemas.py
export interface HealthResponse {
  status: 'ok'
  service: string
  version: string
  environment: string
}

/**
 * `unavailable`: the API could not be reached or reported itself unavailable (network error, 502/503/504).
 * `unexpected_response`: the API answered, but not with what this client expects.
 */
export type ApiErrorKind = 'unavailable' | 'unexpected_response'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status?: number

  constructor(kind: ApiErrorKind, message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
  }
}

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/+$/, '')

const UNAVAILABLE_STATUSES = new Set([502, 503, 504])

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: { Accept: 'application/json', ...init?.headers },
    })
  } catch (error) {
    if (init?.signal?.aborted) {
      throw error
    }
    throw new ApiError('unavailable', `Request to ${path} failed: ${String(error)}`)
  }

  if (!response.ok) {
    const kind = UNAVAILABLE_STATUSES.has(response.status) ? 'unavailable' : 'unexpected_response'
    throw new ApiError(kind, `Request to ${path} failed with status ${response.status}`, response.status)
  }

  try {
    return await response.json()
  } catch {
    throw new ApiError('unexpected_response', `Response from ${path} is not valid JSON`, response.status)
  }
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const body = value as Record<string, unknown>
  return (
    body.status === 'ok' &&
    typeof body.service === 'string' &&
    typeof body.version === 'string' &&
    typeof body.environment === 'string'
  )
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const body = await request('/health', { signal })
  if (!isHealthResponse(body)) {
    throw new ApiError('unexpected_response', 'Response from /health does not match the expected schema')
  }
  return body
}
