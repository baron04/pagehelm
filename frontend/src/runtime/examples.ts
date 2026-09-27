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
import {listen, report} from './contain'

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

/**
 * Call `onFound` the first moment the page's examples can be read, however long that takes.
 *
 * A harness (a demo, a test) injects the boot script ahead of the page, so the very first
 * read finds no tag yet: the parser has not reached it. A single retry once `DOMContentLoaded`
 * fires still raced a document still being parsed -- large enough, that event fired before the
 * tag existed -- so the one read some of the time missed it regardless. This watches the
 * document for the tag's own insertion instead of guessing when it is likely there, and gives
 * up (without calling `onFound`) once the document has finished loading with none to find.
 * Returns a function that stops watching early.
 */
export function watchForExamples(
  doc: Document, maxChars: number, onFound: (examples: PanelExample[]) => void,
): () => void {
  const found = examplesFromPage(doc, maxChars)
  if (found.length) {
    onFound(found)
    return () => {}
  }
  if (doc.readyState !== 'loading') return () => {}
  const tryNow = () => {
    const examples = examplesFromPage(doc, maxChars)
    if (!examples.length) return
    onFound(examples)
    stop()
  }
  const observer = new MutationObserver(tryNow)
  observer.observe(doc, {childList: true, subtree: true})
  const stopListening = listen('watch for examples', doc, 'DOMContentLoaded', tryNow, {once: true})
  const stop = () => {
    observer.disconnect()
    stopListening()
  }
  return stop
}
