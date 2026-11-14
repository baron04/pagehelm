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
  mode: 'create' | 'update'
  record_id: string
  title: string
  check: string
  withheld: string[]
  /** Form items the application currently marks as failing its own validation. */
  invalid: string[]
}
export interface RowRecordRead {record_id: string; name: string; title: string; mode: 'action'}

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

export function normalizeFormRead(raw: unknown): FormModelRead | null {
  if (!raw || typeof raw !== 'object') return null
  const read = raw as {model?: unknown; items?: unknown; mode?: unknown; record_id?: unknown;
    title?: unknown; withheld?: unknown; invalid?: unknown}
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
    check: formCheck(text + '|' + JSON.stringify(withheld)), withheld}
}

export function normalizeRowRead(raw: unknown): RowRecordRead | null {
  if (!raw || typeof raw !== 'object') return null
  const {id, name} = raw as {id?: unknown; name?: unknown}
  if (typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$/.test(id)) return null
  return {record_id: id, name: clip(name, 200), title: '', mode: 'action'}
}

export interface ConfirmBoxRead {open: boolean; title: string; text: string; buttons: string[]}

const BOX = '.el-message-box'

/** The application's own confirmation box, if one is open: what it says and what it offers. */
export function readConfirmBox(doc: Document = document): ConfirmBoxRead {
  const box = Array.from(doc.querySelectorAll<HTMLElement>(BOX)).find(node => {
    const rect = node.getBoundingClientRect()
    return rect.width > 0 && rect.height > 0
  })
  if (!box) return {open: false, title: '', text: '', buttons: []}
  const text = (selector: string) => clip((box.querySelector(selector)?.textContent ?? '').replace(/\s+/g, ' ').trim(), 400)
  return {open: true, title: text('.el-message-box__title'), text: text('.el-message-box__message'),
    buttons: Array.from(box.querySelectorAll<HTMLElement>('.el-message-box__btns button'))
      .map(button => clip((button.textContent ?? '').replace(/\s+/g, ' ').trim(), 40)).filter(Boolean).slice(0, 4)}
}

/** Press one button of the open confirmation box by its label; false if it is not offered. */
export function pressConfirmButton(label: string, doc: Document = document): boolean {
  const box = Array.from(doc.querySelectorAll<HTMLElement>(BOX)).find(node => node.getBoundingClientRect().width > 0)
  const matches = Array.from(box?.querySelectorAll<HTMLButtonElement>('.el-message-box__btns button') ?? [])
    .filter(button => (button.textContent ?? '').replace(/\s+/g, ' ').trim() === label && !button.disabled)
  if (matches.length !== 1) return false
  matches[0]!.click()
  return true
}

const shown = (node: Element) => {
  const rect = node.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

/**
 * After the approved click, give the application the time its own submit takes: validation,
 * the request, its answer. Done when nothing is loading and a quiet moment has passed, or
 * when a confirmation box is waiting for the person's answer; bounded either way.
 */
export async function waitForSubmitToSettle(doc: Document = document,
                                            pause: (ms: number) => Promise<void> = sleep): Promise<void> {
  const until = Date.now() + 6000
  await pause(2200)
  let quiet = 0
  while (Date.now() < until && quiet < 4) {
    const busy = Array.from(doc.querySelectorAll('.el-loading-mask')).some(shown)
    const asking = Array.from(doc.querySelectorAll('.el-message-box')).some(shown)
    if (asking) return
    quiet = busy ? 0 : quiet + 1
    await pause(150)
  }
}
