/**
 * `assistant.js`: the one script a host page includes to have the assistant.
 *
 * Loading it boots the assistant the page asked for (`boot.ts`), declared either by the
 * script tag's own `data-woa-bootstrap` endpoint or by a configuration a harness placed in
 * `window.__WOA_EMBED_CONFIG__`. It also exports the Runtime, for conformance tests.
 *
 * `runtimeInternals` is the pure half of the Runtime, exported for conformance tests. It
 * keeps the one piece of state the original module had at top level -- the sensitive query
 * keys -- in a redactor that `configureSensitiveQueryKeys` replaces; each Runtime has its
 * own and never reads this one.
 */
import {autoBoot} from './boot'
import {formGetUrl, navigationWal, recoveryProvesNavigation, urlDigest, type NativeForm} from './navigation'
import {
  actionElementFingerprint, elementsFrom, hydrateButtonFacts, hydrateFormFacts, hydrateLinkFacts,
  type ElementLookup,
} from './pageModel'
import {createRedactor, type Redactor} from './redaction'
import {safeErrorDetail} from './results'
import type {ActionCommand, ElementRef, JsonObject} from './types'

export {EmbeddedWebRuntime, startEmbeddedRuntime} from './runtime'
export {PanelHost, backoffAfter, classifyTransportError} from './panelHost'
export {Supervisor, autoBoot} from './boot'

let internal: Redactor = createRedactor()

export const runtimeInternals = Object.freeze({
  redactString: (value: unknown) => internal.redactString(value),
  sanitizePageContent: (content: string) => internal.sanitizePageContent(content),
  safeArgs: (args: JsonObject | null | undefined) => internal.safeArgs(args),
  safeErrorDetail: (detail: unknown, command: ActionCommand | null | undefined) =>
    safeErrorDetail(detail, command, internal),
  elementsFrom: (content: string, epoch: number) => elementsFrom(content, epoch, internal),
  hydrateLinkFacts: (elements: ElementRef[], selectorMap: unknown, getByIndex: ElementLookup) =>
    hydrateLinkFacts(elements, selectorMap, getByIndex, internal),
  hydrateFormFacts: (elements: ElementRef[], selectorMap: unknown, getByIndex: ElementLookup) =>
    hydrateFormFacts(elements, selectorMap, getByIndex, internal),
  hydrateButtonFacts: (elements: ElementRef[], selectorMap: unknown, getByIndex: ElementLookup) =>
    hydrateButtonFacts(elements, selectorMap, getByIndex, internal),
  actionElementFingerprint,
  urlDigest,
  navigationWal: (startedUrl: string, expectedUrl: string, navigationKind: string) =>
    navigationWal(startedUrl, expectedUrl, navigationKind, internal),
  recoveryProvesNavigation,
  formGetUrl: (native: NativeForm | null | undefined, submitter: HTMLElement | null = null) =>
    formGetUrl(native, submitter),
  configureSensitiveQueryKeys: (value: unknown) => { internal = createRedactor(value) },
})

if (typeof window !== 'undefined' && typeof document !== 'undefined') autoBoot()
