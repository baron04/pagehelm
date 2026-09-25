/**
 * The examples a page offers a new conversation, read from the tag that asked for the
 * assistant: `data-woa-examples`, a JSON array of `{kind, text}`.
 *
 * They are the application's words about its own records and features, so the application
 * declares them, on its page -- a new conversation shows them before any session exists, and
 * no server has spoken yet. They are page input, so they are checked, and a page that gets
 * them wrong gets none rather than a panel that fails: at most one per kind, each a single
 * line the composer would accept.
 */
import type {ExampleKind, PanelExample} from '../bridge/host'
import {report} from './contain'

const KINDS: readonly ExampleKind[] = ['query', 'find', 'operate']

export function parseExamples(value: string | undefined, maxChars: number): PanelExample[] {
  if (value === undefined || !value.trim()) return []
  try {
    const items: unknown = JSON.parse(value)
    if (!Array.isArray(items)) throw new Error('examples must be a JSON array')
    const examples: PanelExample[] = []
    for (const item of items) {
      const {kind, text} = (item ?? {}) as {kind?: unknown; text?: unknown}
      if (typeof kind !== 'string' || !KINDS.includes(kind as ExampleKind)) {
        throw new Error(`example kind must be one of ${KINDS.join(', ')}`)
      }
      if (examples.some(example => example.kind === kind)) {
        throw new Error(`more than one ${kind} example`)
      }
      const words = typeof text === 'string' ? text.trim() : ''
      // eslint-disable-next-line no-control-regex
      if (!words || words.length > maxChars || /[\u0000-\u001f]/.test(words)) {
        throw new Error(`an example must be one line of 1-${maxChars} characters`)
      }
      examples.push({kind: kind as ExampleKind, text: words})
    }
    return examples
  } catch (error) {
    report('examples', error)
    return []
  }
}

/** The examples on the tag that asked for the assistant, if it offers any. */
export function examplesFromPage(doc: Document, maxChars: number): PanelExample[] {
  const tag = doc.querySelector<HTMLScriptElement>('script[data-woa-bootstrap]')
  return parseExamples(tag?.dataset.woaExamples, maxChars)
}
