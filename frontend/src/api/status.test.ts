import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { API_BASE_URL, ApiError, getHealth } from './client.ts'
import { checkApiStatus } from './status.ts'

const HEALTHY = { status: 'ok', service: 'TraceIQ', version: '0.1.0', environment: 'test' }

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  fetchMock.mockReset()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('getHealth', () => {
  it('calls GET /health on the configured API base URL', async () => {
    fetchMock.mockResolvedValue(jsonResponse(HEALTHY))

    await expect(getHealth()).resolves.toEqual(HEALTHY)
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock.mock.calls[0][0]).toBe(`${API_BASE_URL}/health`)
  })

  it('classifies a network failure as unavailable', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    await expect(getHealth()).rejects.toMatchObject({ name: 'ApiError', kind: 'unavailable' })
  })

  it('rethrows the abort error when the request is aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    fetchMock.mockRejectedValue(new DOMException('Aborted', 'AbortError'))

    await expect(getHealth(controller.signal)).rejects.not.toBeInstanceOf(ApiError)
  })
})

describe('checkApiStatus', () => {
  it('returns online with health details on a valid response', async () => {
    fetchMock.mockResolvedValue(jsonResponse(HEALTHY))

    await expect(checkApiStatus()).resolves.toEqual({ state: 'online', health: HEALTHY })
  })

  it('returns unavailable when the API cannot be reached', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    await expect(checkApiStatus()).resolves.toEqual({ state: 'unavailable' })
  })

  it.each([502, 503, 504])('returns unavailable on HTTP %i', async (status) => {
    fetchMock.mockResolvedValue(jsonResponse({ error: {} }, status))

    await expect(checkApiStatus()).resolves.toEqual({ state: 'unavailable' })
  })

  it.each([
    ['HTTP 404', () => jsonResponse({ error: { code: 'http_error', message: 'Not Found' } }, 404)],
    ['HTTP 500', () => jsonResponse({ error: { code: 'internal_error' } }, 500)],
    ['a non-JSON body', () => new Response('<html>proxy page</html>', { status: 200 })],
    ['a body with the wrong shape', () => jsonResponse({ status: 'ok' })],
    ['a non-ok status field', () => jsonResponse({ ...HEALTHY, status: 'degraded' })],
  ])('returns unexpected for %s', async (_label, makeResponse) => {
    fetchMock.mockResolvedValue(makeResponse())

    await expect(checkApiStatus()).resolves.toEqual({ state: 'unexpected' })
  })

  it('rejects instead of reporting a status when aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    fetchMock.mockRejectedValue(new DOMException('Aborted', 'AbortError'))

    await expect(checkApiStatus(controller.signal)).rejects.toThrow('Aborted')
  })
})
