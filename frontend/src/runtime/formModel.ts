/**
 * The data bound to a live native form, as the Runtime hands it to the server.
 *
 * The page controller reads the mounted form component's model; this module only checks
 * the shape, bounds it and gives it a short check value. The check is computed here and
 * compared here: the server returns it opaque with an approved submit, so a form changed
 * between the card and the click is noticed by the same code that read it, whatever the
 * server's own number formatting.
 */
import browserContract from '@contract/browser_contract.json'
import {sleep} from './contain'

export interface FormItemRead {prop: string; label: string; display: string}
export interface FormModelRead {
  model: Record<string, unknown>
  items: FormItemRead[]
  source?: string
  mode: 'create' | 'update'
  record_id: string
  title: string
  check: string
  withheld: string[]
  /** Form items the application currently marks as failing its own validation. */
  invalid: string[]
}
export interface RowRecordRead {record_id: string; name: string; title: string; mode: 'action'; check?: string; source?: string; toggle?: {field: string; before: unknown; after: unknown; label: string}}

const LIMIT = browserContract.form_model

/** JSON with sorted keys: the same data always gives the same text. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null'
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']'
  const object = value as Record<string, unknown>
  return '{' + Object.keys(object).sort().filter(key => object[key] !== undefined)
    .map(key => JSON.stringify(key) + ':' + canonicalJson(object[key])).join(',') + '}'
}

/** Two 32-bit FNV-1a passes over the text: a change detector, not a security boundary. */
export function formCheck(text: string): string {
  let a = 0x811c9dc5, b = 0x9747b28c
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index)
    a = Math.imul(a ^ code, 0x01000193) >>> 0
    b = Math.imul(b ^ (code + index), 0x01000193) >>> 0
  }
  return a.toString(16).padStart(8, '0') + b.toString(16).padStart(8, '0') + text.length.toString(16)
}

const clip = (value: unknown, max: number) => Array.from(String(value ?? ''))
  .map(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127 ? ' ' : char).join('').slice(0, max)

/** A controller's raw reading, checked and bounded; null when it is not a readable form. */
export function unreadableReason(raw: unknown): string {
  const reason = (raw as {unreadable?: unknown} | null | undefined)?.unreadable
  return typeof reason === 'string' ? reason.replace(/[^a-z_]/g, '').slice(0, 40) : ''
}

/**
 * The application facts a form read may carry but core does not presume: the page
 * controller module supplies these, core stays empty-handed. Without a hook the
 * answer fails closed -- a read without a vouched source reaches the server with
 * none, and no confirmation or settle handling exists at all.
 */
export interface FormIntegration {
  /** Whether the application vouches for `source` naming one of its own read
   * endpoints for this kind of read. Absent: no source is ever forwarded. */
  acceptReadSource?: ((source: string, kind: 'form' | 'row') => boolean) | undefined
  /** The application's own confirmation box, if it has one: its raw read is
   * bounded here like any other untrusted page fact. */
  readConfirmBox?: ((doc: Document) => unknown) | undefined
  /** Press one button of the open confirmation box by its label. */
  pressConfirmButton?: ((label: string, doc: Document) => boolean) | undefined
  /** Whether the application still shows its own submit-in-flight indicator. */
  readSubmitBusy?: ((doc: Document) => boolean) | undefined
}

export function normalizeFormRead(raw: unknown, integration?: FormIntegration): FormModelRead | null {
  if (!raw || typeof raw !== 'object') return null
  const read = raw as {model?: unknown; items?: unknown; mode?: unknown; record_id?: unknown;
    title?: unknown; withheld?: unknown; invalid?: unknown; source?: unknown}
  if (!read.model || typeof read.model !== 'object' || Array.isArray(read.model)) return null
  if (read.mode !== 'create' && read.mode !== 'update') return null
  const text = canonicalJson(read.model)
  if (text.length > LIMIT.max_bytes) return null
  const items: FormItemRead[] = []
  for (const item of Array.isArray(read.items) ? read.items.slice(0, 400) : []) {
    const {prop, label, display} = (item ?? {}) as Partial<FormItemRead>
    if (typeof prop === 'string' && prop) {
      items.push({prop: clip(prop, 120), label: clip(label, 120), display: clip(display, 200)})
    }
  }
  const withheld = Array.isArray(read.withheld)
    ? read.withheld.filter((name): name is string => typeof name === 'string').slice(0, 40).map(name => clip(name, 120)) : []
  const invalid = Array.isArray(read.invalid)
    ? read.invalid.filter((name): name is string => typeof name === 'string').slice(0, 40).map(name => clip(name, 120)) : []
  return {model: read.model as Record<string, unknown>, items, mode: read.mode, invalid,
    record_id: read.mode === 'update' ? clip(read.record_id, 40) : '', title: clip(read.title, 120),
    check: formCheck(text + '|' + JSON.stringify(withheld)), withheld,
    ...(typeof read.source === 'string' && integration?.acceptReadSource?.(read.source, 'form') === true
      ? {source: read.source} : {})}
}

export function normalizeRowRead(raw: unknown, integration?: FormIntegration): RowRecordRead | null {
  if (!raw || typeof raw !== 'object') return null
  const {id, name, source, toggle} = raw as {id?: unknown; name?: unknown; source?: unknown; toggle?: RowRecordRead["toggle"]}
  if (typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$/.test(id)) return null
  return {record_id: id, name: clip(name, 200), title: '', mode: 'action', check: formCheck(canonicalJson({id, source, toggle})),
    ...(typeof source === 'string' && integration?.acceptReadSource?.(source, 'row') === true
      ? {source} : {}),
    ...(toggle && typeof toggle.field === 'string' && /^[a-z][A-Za-z0-9]{0,59}$/.test(toggle.field) &&
      ['string', 'number', 'boolean'].includes(typeof toggle.before) &&
      ['string', 'number', 'boolean'].includes(typeof toggle.after)
      ? {toggle: {...toggle, label: clip(toggle.label, 80)}} : {})}
}

export interface ConfirmBoxRead {open: boolean; title: string; text: string; buttons: string[]}

const CLOSED: ConfirmBoxRead = {open: false, title: '', text: '', buttons: []}

/** An application's raw confirmation read, bounded like any page fact. */
export function normalizeConfirmRead(raw: unknown): ConfirmBoxRead {
  const read = raw as {open?: unknown; title?: unknown; text?: unknown; buttons?: unknown} | null | undefined
  if (!read || read.open !== true) return CLOSED
  const buttons = (Array.isArray(read.buttons) ? read.buttons : [])
    .map(button => clip(String(button ?? '').replace(/\s+/g, ' ').trim(), 40)).filter(Boolean).slice(0, 4)
  return {open: true, title: clip(String(read.title ?? '').replace(/\s+/g, ' ').trim(), 400),
    text: clip(String(read.text ?? '').replace(/\s+/g, ' ').trim(), 400), buttons}
}

/** The application's own confirmation box, if its integration can read one. */
export function readConfirmBox(doc: Document = document, integration?: FormIntegration): ConfirmBoxRead {
  return integration?.readConfirmBox ? normalizeConfirmRead(integration.readConfirmBox(doc)) : CLOSED
}

/** Press one button of the open confirmation box by its label; without an integration
 * presser no confirmation can ever be pressed, so the request fails closed. */
export function pressConfirmButton(label: string, doc: Document = document,
                                   integration?: FormIntegration): boolean {
  return integration?.pressConfirmButton ? integration.pressConfirmButton(label, doc) === true : false
}

/**
 * After the approved click, give the application the time its own submit takes: validation,
 * the request, its answer. Done when nothing is loading and a quiet moment has passed, or
 * when a confirmation box is waiting for the person's answer; bounded either way.
 * Without the application's own busy reader there is no honest settle detection:
 * the action fails rather than pretend the click settled.
 */
export async function waitForSubmitToSettle(doc: Document = document,
                                            pause: (ms: number) => Promise<void> = sleep,
                                            integration?: FormIntegration): Promise<void> {
  if (!integration?.readSubmitBusy) throw new Error('submit settle detection unavailable')
  const until = Date.now() + 6000
  await pause(120)
  let quiet = 0
  while (Date.now() < until && quiet < 3) {
    const asking = integration.readConfirmBox ? normalizeConfirmRead(integration.readConfirmBox(doc)).open : false
    if (asking) return
    const busy = integration.readSubmitBusy(doc) === true
    quiet = busy ? 0 : quiet + 1
    if (quiet < 3) await pause(100)
  }
}

/**
 * Whether the control about to be pressed is still the one the card named, by its own text.
 * A control with no text of its own (a switch, an icon button) is named by what the server read
 * from around it, so there is nothing here to compare: it is bound by its row or form instead.
 */
export function sameControlText(expected: unknown, actual: unknown): boolean {
  const plain = (value: unknown) => String(value ?? '').replace(/\s+/g, '')
  return !plain(expected) || !plain(actual) || plain(expected) === plain(actual)
}
