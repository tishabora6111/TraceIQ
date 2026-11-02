import { ApiError, getHealth, type HealthResponse } from './client.ts'

export type ApiStatus =
  | { state: 'checking' }
  | { state: 'online'; health: HealthResponse }
  | { state: 'unavailable' }
  | { state: 'unexpected' }

/** Resolve the API status for display. Rejects only when the request was aborted. */
export async function checkApiStatus(signal?: AbortSignal): Promise<ApiStatus> {
  try {
    return { state: 'online', health: await getHealth(signal) }
  } catch (error) {
    if (signal?.aborted) {
      throw error
    }
    console.warn('API health check failed', error)
    if (error instanceof ApiError && error.kind === 'unexpected_response') {
      return { state: 'unexpected' }
    }
    return { state: 'unavailable' }
  }
}
