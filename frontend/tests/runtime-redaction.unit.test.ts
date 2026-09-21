import {describe, expect, it} from 'vitest'
import {createRedactor, sensitiveQueryKeys} from '../src/runtime/redaction'
import sharedCases from '../../src/web_operation_agent/tests/golden/redaction_cases.json'

describe('browser and server redaction share one security corpus', () => {
  const redactor = createRedactor(sharedCases.extra_keys)

  it('agrees on string redaction and sensitive field names', () => {
    for (const testCase of sharedCases.string_cases) {
      expect(redactor.redactString(testCase.input), testCase.name).toBe(testCase.expected)
    }
    for (const testCase of sharedCases.name_cases) {
      expect(redactor.isSensitiveName(testCase.input), testCase.input).toBe(testCase.sensitive)
    }
  })
})

describe('sensitiveQueryKeys validates a deployment extra list', () => {
  it('accepts safe names and rejects anything else', () => {
    expect(sensitiveQueryKeys(['externalLoginKey', 'a.b-c_9'])).toEqual(
      ['externalLoginKey', 'a.b-c_9'])
    expect(sensitiveQueryKeys(undefined)).toEqual([])
    expect(() => sensitiveQueryKeys(['9leadingDigit'])).toThrow()
    expect(() => sensitiveQueryKeys(['has space'])).toThrow()
    expect(() => sensitiveQueryKeys(Array.from({length: 33}, (_, i) => `k${i}`))).toThrow()
  })
})

describe('a redactor never sends a credential assignment', () => {
  const redactor = createRedactor(['externalLoginKey'])

  it('redacts the generic sensitive names in a URL query', () => {
    expect(redactor.redactString('https://app.test/login?password=hunter2&ok=1'))
      .toBe('https://app.test/login?password=[redacted]&ok=1')
  })

  it('redacts a deployment-specific extra query name the same way', () => {
    expect(redactor.redactString('https://app.test/sso?externalLoginKey=abc123'))
      .toBe('https://app.test/sso?externalLoginKey=[redacted]')
  })

  it('collapses the whole string when a percent-encoded key only reveals a credential after decoding', () => {
    // The literal text has no recognizable key -- 'password' starts with an encoded 'p' --
    // so only decoding uncovers the assignment. A direct-only redaction would leak it.
    expect(redactor.redactString('https://app.test/x?%70assword=secret')).toBe('[redacted]')
  })

  it('leaves an ordinary URL untouched, byte for byte', () => {
    const clean = 'https://app.test/companies?stage=won&sort=name'
    expect(redactor.redactString(clean)).toBe(clean)
  })

  it('is per-instance: an unconfigured redactor does not know a deployment key', () => {
    const plain = createRedactor()
    const withKey = 'https://app.test/sso?externalLoginKey=abc123'
    expect(plain.redactString(withKey)).toBe(withKey)
    expect(plain.isSensitiveName('externalLoginKey')).toBe(false)
    expect(redactor.isSensitiveName('externalLoginKey')).toBe(true)
  })
})

describe('page content sanitization removes a sensitive field value only', () => {
  const redactor = createRedactor()

  it('redacts the value of a password field but keeps the rest of the record', () => {
    const content = '[0]<input type=password name=PASSWORD value=my super secret />'
    const out = redactor.sanitizePageContent(content)
    expect(out).toContain('[redacted]')
    expect(out).not.toContain('my super secret')
    expect(out).toContain('[0]<input')
  })

  it('leaves a field with no sensitive name or type alone', () => {
    const content = '[7]<input type=text name=note value=public />'
    expect(redactor.sanitizePageContent(content)).toBe(content)
  })

  it('bounds the output to MAX_CONTENT_CHARS', () => {
    const huge = 'x'.repeat(200_000)
    expect(redactor.sanitizePageContent(huge).length).toBe(120_000)
  })
})

describe('safeArgs never includes a typed value', () => {
  const redactor = createRedactor()

  it('redacts the value key unconditionally', () => {
    expect(redactor.safeArgs({ref: 'e0', value: 'hunter2'})).toEqual(
      {ref: 'e0', value: '[redacted]'})
  })

  it('redacts a field whose name looks like a secret', () => {
    expect(redactor.safeArgs({apiKey: 'sk-live-abc'})).toEqual({apiKey: '[redacted]'})
  })

  it('recurses into nested objects and arrays', () => {
    expect(redactor.safeArgs({options: {password: 'x'}, list: [{token: 'y'}]}))
      .toEqual({options: {password: '[redacted]'}, list: [{token: '[redacted]'}]})
  })
})
