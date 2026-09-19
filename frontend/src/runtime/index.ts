/**
 * The embedded Runtime, as one module the host page imports.
 *
 * `runtimeInternals` is the pure half of the Runtime, exported for conformance tests. It
 * keeps the one piece of state the original module had at top level -- the sensitive query
 * keys -- in a redactor that `configureSensitiveQueryKeys` replaces; each Runtime has its
 * own and never reads this one.
 */
import {detach} from './contain'
import {formGetUrl, navigationWal, recoveryProvesNavigation, urlDigest, type NativeForm} from './navigation'
import {
  actionElementFingerprint, elementsFrom, hydrateFormFacts, hydrateLinkFacts,
  type ElementLookup,
} from './pageModel'
import {createRedactor, type Redactor} from './redaction'
import {safeErrorDetail} from './results'
import {startEmbeddedRuntime} from './runtime'
import type {ActionCommand, ElementRef, JsonObject} from './types'

export {EmbeddedWebRuntime, startEmbeddedRuntime} from './runtime'
export {PanelHost, backoffAfter, classifyTransportError} from './panelHost'

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
  actionElementFingerprint,
  urlDigest,
  navigationWal: (startedUrl: string, expectedUrl: string, navigationKind: string) =>
    navigationWal(startedUrl, expectedUrl, navigationKind, internal),
  recoveryProvesNavigation,
  formGetUrl: (native: NativeForm | null | undefined, submitter: HTMLElement | null = null) =>
    formGetUrl(native, submitter),
  configureSensitiveQueryKeys: (value: unknown) => { internal = createRedactor(value) },
})

if (typeof window !== 'undefined' && window.__WOA_EMBED_CONFIG__) {
  detach('embedded runtime', startEmbeddedRuntime(window.__WOA_EMBED_CONFIG__))
}
