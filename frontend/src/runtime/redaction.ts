/**
 * What never leaves the page: credentials in URLs, secret-looking fields, and the values
 * typed into them.
 *
 * The patterns were module-level variables that one call reconfigured for everything that
 * read them later. They now belong to a `Redactor`, created with the deployment's extra
 * query names and handed to whatever needs it, so a second Runtime or a test cannot change
 * what the first one redacts. The patterns themselves are unchanged, byte for byte: this
 * is the security boundary, and a refactor is not the place to improve it.
 */
import type {JsonObject} from './types'
import browserContract from '@contract/browser_contract.json'

export const MAX_CONTENT_CHARS = browserContract.observation.max_content_chars

const GENERIC_SENSITIVE_NAME = '(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)'
const URL_SECRET_ASSIGNMENT = /((?:^|[?&#;\s"'<>\\]|%(?:25)*(?:26|23|3[bBfF]))(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|authorization|password|passwd|pwd|secret|token)(?:=|%(?:25)*3[dD]))(.+?)(?=(?:[&;/?#\s"'<>\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))/gi
const NOTHING = /(?!)/g

function sensitiveFieldPattern(pattern: string): RegExp {
  return new RegExp(`\\b(?:type|name|id|autocomplete)=(?:"[^"]*(?:${pattern})[^"]*"|'[^']*(?:${pattern})[^']*'|[^\\s>]*(?:${pattern})[^\\s>]*)`, 'i')
}

export interface Redactor {
  /** A field or query name whose value is never sent. */
  isSensitiveName(name: string): boolean
  /** A URL or text with every credential assignment replaced, or `[redacted]` whole. */
  redactString(value: unknown): string
  /** PageController's page text, with sensitive fields' values removed and a length bound. */
  sanitizePageContent(content: unknown): string
  /** Action arguments as the server may log them: typed values never included. */
  safeArgs(args: JsonObject | null | undefined): JsonObject
}

/** Validate a deployment's extra sensitive query names; the same rule the server applies. */
export function sensitiveQueryKeys(value: unknown): string[] {
  const keys = Array.isArray(value) ? value : []
  if (keys.length > 32 || keys.some(key =>
    typeof key !== 'string' || !/^[A-Za-z][A-Za-z0-9_.-]{1,63}$/.test(key))) {
    throw new Error('sensitiveQueryKeys must contain at most 32 safe query names')
  }
  return keys as string[]
}

/** A redactor for this deployment; `queryKeys` is validated by `sensitiveQueryKeys`. */
export function createRedactor(queryKeys: unknown = []): Redactor {
  const escaped = [...new Set(sensitiveQueryKeys(queryKeys).map(key => key.toLowerCase()))]
    .map(key => key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const namePattern = escaped.length
    ? `${GENERIC_SENSITIVE_NAME}|(?:${escaped.join('|')})` : GENERIC_SENSITIVE_NAME
  const sensitiveName = new RegExp(namePattern, 'i')
  const sensitiveField = sensitiveFieldPattern(namePattern)
  const handoffAssignment = escaped.length
    ? new RegExp(`((?:${escaped.join('|')})(?:=|%(?:25)*3[dD]))(.+?)(?=(?:[&;/?#\\s"<>\\\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))`, 'gi')
    : NOTHING

  const hasResidualEncodedCredential = (value: unknown): boolean => {
    let current = String(value ?? '')
    for (let depth = 0; depth <= 3; depth += 1) {
      const redacted = current.replace(handoffAssignment, '$1[redacted]')
        .replace(URL_SECRET_ASSIGNMENT, '$1[redacted]')
      if (redacted !== current) return true
      try {
        const decoded = decodeURIComponent(current)
        if (decoded === current) break
        current = decoded
      } catch { break }
    }
    return false
  }

  const redactString = (value: unknown): string => {
    let out = String(value ?? '')
    // The expression understands repeatedly encoded '=' and delimiters. Keep the rest of the
    // URL byte-for-byte intact because link allowlisting compares its exact target.
    for (let i = 0; i < 3; i += 1) {
      const next = out.replace(handoffAssignment, '$1[redacted]')
        .replace(URL_SECRET_ASSIGNMENT, '$1[redacted]')
      if (next === out) break
      out = next
    }
    return hasResidualEncodedCredential(out) ? '[redacted]' : out
  }

  const sanitizePageContent = (content: unknown): string => {
    let out = redactString(content)
    // PageController sometimes serializes an unquoted input value containing spaces. Once a
    // record identifies a sensitive field, consume the whole value tail rather than guessing
    // where an unquoted value ends. Losing later presentation attributes is safer than
    // sending part of a credential to the server.
    out = out.replace(/(?:^|\n)\s*\*?\[\d+]<[\w-]+[\s\S]*?\s*\/>?(?=\n|$)/g, record => {
      if (!sensitiveField.test(record)) return record
      let clean = record.replace(/\bvalue=(?:"[^"]*"|'[^']*'|[\s\S]*?)(?=\s*\/?>\s*$)/gi,
        'value=[redacted]')
      const separator = clean.indexOf('>')
      const close = clean.lastIndexOf('/>')
      if (separator >= 0 && close > separator) {
        clean = `${clean.slice(0, separator + 1)}[redacted] ${clean.slice(close)}`
      }
      return clean
    })
    return out.slice(0, MAX_CONTENT_CHARS)
  }

  const sanitize = (key: string, value: unknown): unknown => {
    if (key === 'value' || sensitiveName.test(key)) return '[redacted]'
    if (Array.isArray(value)) return value.map(item => sanitize('', item))
    if (value && typeof value === 'object') {
      return Object.fromEntries(
        Object.entries(value as JsonObject).map(([child, item]) => [child, sanitize(child, item)]))
    }
    return typeof value === 'string' ? redactString(value) : value
  }

  return {
    isSensitiveName: name => sensitiveName.test(name),
    redactString,
    sanitizePageContent,
    safeArgs: args => Object.fromEntries(
      Object.entries(args ?? {}).map(([key, value]) => [key, sanitize(key, value)])),
  }
}
