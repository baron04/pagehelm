import {describe, expect, it} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {
  absoluteUrl, canonicalUrl, isSubmitControl, navigationWal, recoveryProvesNavigation,
  sameOriginUrl, scrubLegacyNavigationUrls, targetsCurrentPage,
} from '../src/runtime/navigation'
import type {ActionEnvelope} from '../src/runtime/types'

describe('URL helpers', () => {
  it('canonicalUrl drops the fragment: a hash change is not a page change', () => {
    expect(canonicalUrl('https://app.test/page#section')).toBe('https://app.test/page')
  })

  it('sameOriginUrl refuses a cross-origin target by naming its origin', () => {
    expect(() => sameOriginUrl('https://evil.test/x')).toThrow(/origin_refused/)
  })

  it('absoluteUrl resolves a relative path against the current location', () => {
    expect(absoluteUrl('/next')).toBe(new URL('/next', location.href).href)
  })

  it('targetsCurrentPage treats empty and _self as the current page only', () => {
    expect(targetsCurrentPage('')).toBe(true)
    expect(targetsCurrentPage('_self')).toBe(true)
    expect(targetsCurrentPage('_blank')).toBe(false)
    expect(targetsCurrentPage('reportWindow')).toBe(false)
  })
})

describe('isSubmitControl', () => {
  it('recognizes a plain button and submit/image inputs, and nothing else', () => {
    expect(isSubmitControl({tagName: 'BUTTON'})).toBe(true)
    expect(isSubmitControl({tagName: 'BUTTON', type: 'button'})).toBe(false)
    expect(isSubmitControl({tagName: 'INPUT', type: 'submit'})).toBe(true)
    expect(isSubmitControl({tagName: 'INPUT', type: 'image'})).toBe(true)
    expect(isSubmitControl({tagName: 'INPUT', type: 'text'})).toBe(false)
    expect(isSubmitControl({tagName: 'A'})).toBe(false)
  })
})

describe('navigationWal writes a digest ahead of the action, not a raw URL', () => {
  const redactor = createRedactor()

  it('digests both the started and expected URL and redacts their display form', () => {
    return navigationWal('https://app.test/a?password=x', 'https://app.test/b',
      'link', redactor).then(wal => {
      expect(wal.started_url_digest).toMatch(/^[0-9a-f]{64}$/)
      expect(wal.expected_url_digest).toMatch(/^[0-9a-f]{64}$/)
      expect(wal.started_url_display).toContain('[redacted]')
      expect(wal.navigation_kind).toBe('link')
    })
  })

  it('leaves the expected side empty when there is no navigation expectation', () => {
    return navigationWal('https://app.test/a', '', '', redactor).then(wal => {
      expect(wal.expected_url_digest).toBe('')
      expect(wal.expected_url_display).toBe('')
    })
  })
})

describe('scrubLegacyNavigationUrls upgrades an old stored action in place', () => {
  const redactor = createRedactor()

  it('replaces started_url/expected_url with their redacted display fields', () => {
    const legacy = {started_url: 'https://app.test/a?token=s', expected_url: 'https://app.test/b'} as
      unknown as ActionEnvelope
    const scrubbed = scrubLegacyNavigationUrls(legacy, redactor)
    expect(scrubbed).not.toBeNull()
    expect((scrubbed as ActionEnvelope).started_url).toBeUndefined()
    expect(scrubbed?.started_url_display).toContain('[redacted]')
    expect(scrubbed?.expected_url_display).toBe('https://app.test/b')
  })

  it('passes null and a record with neither legacy field straight through', () => {
    expect(scrubLegacyNavigationUrls(null, redactor)).toBeNull()
    const modern = {action_id: 'a1'} as unknown as ActionEnvelope
    expect(scrubLegacyNavigationUrls(modern, redactor)).toEqual(modern)
  })
})

describe('recoveryProvesNavigation only trusts a digest match, not what the page now shows', () => {
  it('a form is proven only by a new document id, never by URL', async () => {
    const action = {phase: 'claimed', navigation_kind: 'form', document_id: 'doc-1'} as
      unknown as ActionEnvelope
    expect(await recoveryProvesNavigation(action, 'https://app.test/anywhere', 'doc-2')).toBe(true)
    expect(await recoveryProvesNavigation(action, 'https://app.test/anywhere', 'doc-1')).toBe(false)
  })

  it('a link is proven only when the observed URL digests to the expected one', async () => {
    const expected = await (await import('../src/runtime/navigation')).urlDigest('https://app.test/next')
    const action = {phase: 'claimed', navigation_kind: 'link',
      expected_url_digest: expected, started_url_digest: 'different'} as unknown as ActionEnvelope
    expect(await recoveryProvesNavigation(action, 'https://app.test/next')).toBe(true)
    expect(await recoveryProvesNavigation(action, 'https://app.test/other')).toBe(false)
  })

  it('an action that was never claimed proves nothing', async () => {
    const action = {phase: 'prepared', navigation_kind: 'link'} as unknown as ActionEnvelope
    expect(await recoveryProvesNavigation(action, 'https://app.test/next')).toBe(false)
  })
})
