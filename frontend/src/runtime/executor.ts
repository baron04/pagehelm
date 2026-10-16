/**
 * Doing one page action, and knowing afterwards whether it did what it was for.
 *
 * The server hands the Runtime a generic command -- click, fill, select, submit, goto,
 * back, scroll, wait. Some of those leave the page. For those the Runtime has to decide, before it
 * acts, where the page should end up, and after it acts, whether it got there: a navigation
 * it cannot see is `navigation_timeout`, never a success.
 */
import {every, later, listen, sleep} from './contain'
import {
  effectiveTarget, formGetUrl, isSameDocumentFragment, isSubmitControl, navigationCommitted,
  navigationWatch, sameOriginUrl, targetsCurrentPage, type NativeForm, type NavigationSignal,
  type NavigationWatch,
} from './navigation'
import type {Page} from './page'
import {explicitAttribute, type ElementLike} from './pageModel'
import type {ActionCommand, RuntimeConfig} from './types'
import browserContract from '@contract/browser_contract.json'

const MAX_WAIT_MS = 10_000
const MAX_SCROLL_PAGES = browserContract.scroll.max_pages
/** Time kept back from an action's deadline so its result can still reach the server. */
const RESULT_POST_RESERVE_MS = 500

export interface CommandOutcome {
  success?: boolean
  message?: string
  native_navigation_expected?: boolean
  [extra: string]: unknown
}

/** A link or a native form submission, as HTML would carry it out. */
export type NativeNavigation =
  | {kind: 'link'; url: string; target: string}
  | ({kind: 'form'; target: string} & NativeForm)

type FormElement = HTMLFormElement & ElementLike
type ControlElement = ElementLike & {formAction?: string; form?: FormElement | null}

/**
 * Where a link or submit control would take the page, or `null` if it takes it nowhere
 * HTML knows about. A target other than this page is refused: the Runtime cannot observe a
 * new window, so it could never tell the server what happened there.
 */
export function nativeNavigation(element: ElementLike | null | undefined): NativeNavigation | null {
  const tag = String(element?.tagName || '').toUpperCase()
  if (element && tag === 'A' && element.href) {
    // `element.href` is resolved; the raw attribute says whether a fragment was written.
    const written = explicitAttribute(element, 'href') ?? ''
    if (isSameDocumentFragment(written || String(element.href))) return null
    const target = effectiveTarget(element)
    if (!targetsCurrentPage(target)) {
      throw new Error('bad_arguments: link target must be the current page')
    }
    return {kind: 'link', url: sameOriginUrl(element.href), target}
  }
  const control = element as ControlElement | null | undefined
  const form = (tag === 'FORM' ? element : (isSubmitControl(element) ? control?.form : null)) as
    FormElement | null | undefined
  if (!form) return null
  const override = tag !== 'FORM' ? explicitAttribute(element, 'formaction') : null
  const action = override !== null
    ? (control?.formAction || override || location.href)
    : (form.action || location.href)
  const target = effectiveTarget(tag === 'FORM' ? null : element, form)
  if (!targetsCurrentPage(target)) {
    throw new Error('bad_arguments: submit target must be the current page')
  }
  const methodOverride = tag !== 'FORM' ? explicitAttribute(element, 'formmethod') : null
  const method = String(methodOverride || form.method || 'get').toLowerCase()
  return {kind: 'form', url: sameOriginUrl(action), target, form, method}
}

/**
 * Run a native submission and report whether the form actually submitted: a submit event
 * that was not cancelled. A form that failed its own validation did not submit, whatever
 * the click returned.
 */
async function withSubmitEvidence(native: NativeForm,
                                  invoke: () => Promise<unknown> | unknown): Promise<CommandOutcome> {
  let submitEvent: Event | null = null
  const stop = listen('submit evidence', native.form, 'submit', (event: Event) => {
    if (event.target === native.form) submitEvent = event
  }, true)
  try {
    const raw = await invoke()
    const result: CommandOutcome = raw && typeof raw === 'object' ? raw as CommandOutcome : {success: true}
    let validationFailed = false
    if (!submitEvent && typeof native.form.checkValidity === 'function') {
      try { validationFailed = native.form.checkValidity() === false } catch { /* opaque DOM */ }
    }
    if (validationFailed) {
      return {...result, success: false, message: 'form validation prevented submission',
        native_navigation_expected: false}
    }
    const event = submitEvent as Event | null
    return {...result, native_navigation_expected: Boolean(
      event && !event.defaultPrevented && native.method !== 'dialog')}
  } finally {
    stop()
  }
}

/**
 * The page is asked for only by the commands that touch it, so a Runtime whose page is not
 * loaded can still wait, navigate, and fail the rest as an ordinary `page_error`.
 */
export type PageAccess = () => Page

/**
 * The application opening one of its own pages in this document: true only when it now
 * shows another address. The Runtime still proves the navigation by the address, as for
 * any other, and loads the page itself when the application answers anything else.
 */
export type ApplicationOpen = (url: string) => Promise<boolean>

export async function executeCommand(command: ActionCommand, access: PageAccess,
                                     config: Pick<RuntimeConfig, 'navigationMode'>,
                                     open?: ApplicationOpen): Promise<CommandOutcome | unknown> {
  const args = command.args || {}
  switch (command.action) {
    case 'observe':
      return {success: true, message: 'page observed'}
    case 'click': {
      const page = access()
      const native = nativeNavigation(page.element(args.ref))
      if (native?.kind === 'form') return withSubmitEvidence(native, () => page.click(args.ref))
      return page.click(args.ref)
    }
    case 'apply_facet':
      return access().applyFacet(args.ref, args.active)
    case 'reveal':
      return access().reveal(args.ref)
    case 'submit': {
      const element = access().element(args.ref) as ControlElement | null | undefined
      const form = (String(element?.tagName || '').toUpperCase() === 'FORM'
        ? element : element?.form) as FormElement | null | undefined
      if (!form) throw new Error('bad_arguments: submit ref does not belong to a form')
      const native = nativeNavigation(form)
      if (native?.kind === 'form' && typeof form.requestSubmit === 'function') {
        return withSubmitEvidence(native, () => {
          form.requestSubmit()
          return {success: true, message: 'form submitted'}
        })
      }
      form.submit()
      return {success: true, message: 'form submitted',
        native_navigation_expected: native?.kind === 'form' && native.method !== 'dialog'}
    }
    case 'fill':
      if (args.value === undefined) throw new Error('bad_arguments: fill needs value')
      if (args.commit !== undefined && typeof args.commit !== 'boolean') {
        throw new Error('bad_arguments: fill commit must be boolean')
      }
      return access().input(args.ref, String(args.value), args.commit === true)
    case 'select':
      if (args.value === undefined) throw new Error('bad_arguments: select needs value')
      if (args.option_label !== undefined && typeof args.option_label !== 'string') {
        throw new Error('bad_arguments: select option label must be a string')
      }
      return access().select(args.ref, String(args.value), args.option_label as string | undefined)
    case 'wait_for':
      await sleep(Math.min(Math.max(Number(args.ms || 1000), 0), MAX_WAIT_MS))
      return {success: true, message: 'wait completed'}
    case 'scroll': {
      const direction = args.direction
      const pages = args.pages === undefined ? 1 : Number(args.pages)
      if (!['up', 'down'].includes(String(direction || ''))) {
        throw new Error('bad_arguments: scroll direction must be up or down')
      }
      if (!Number.isInteger(pages) || pages < 1 || pages > MAX_SCROLL_PAGES) {
        throw new Error(`bad_arguments: scroll pages must be an integer from 1 to ${MAX_SCROLL_PAGES}`)
      }
      // With a reference, the container that holds that element scrolls: a page whose
      // content scrolls inside its layout is not moved by scrolling the window.
      return args.ref === undefined ? access().scroll(direction === 'down', pages)
        : access().scroll(direction === 'down', pages, args.ref)
    }
    case 'goto': {
      if (!args.url) throw new Error('bad_arguments: goto needs url')
      const href = sameOriginUrl(args.url)
      // A route already shown is not a navigation. Loading it again destroys the
      // assistant and page state; the server verifies the current rendered route.
      if (href === window.location.href) {
        return {success: true, message: 'already on page'}
      }
      if (config.navigationMode === 'spa') {
        // The event must carry the state that was just pushed. A router is entitled to read
        // `event.state` and ignore a stateless popstate as "a click on an anchor" -- Odoo's
        // does exactly that -- so a bare PopStateEvent changes the address bar while the
        // application still shows the previous view, silently.
        const state = {woaNavigation: true}
        history.pushState(state, '', href)
        window.dispatchEvent(new PopStateEvent('popstate', {state: history.state ?? state}))
        return {success: true, message: 'SPA navigation requested'}
      }
      // An application that opens its own pages keeps this document, and with it the
      // assistant's open window and whatever the person had on screen. A page it does not
      // open is loaded, as every page was before.
      if (open && await open(href)) return {success: true, message: 'application navigation requested'}
      location.assign(href)
      return {success: true, message: 'navigation requested'}
    }
    case 'back':
      history.back()
      return {success: true, message: 'history navigation requested'}
    default:
      throw new Error(`bad_arguments: unsupported action ${command.action}`)
  }
}

/** Whether a command can leave the page, so the Runtime must wait to see if it did. */
export function mayNavigate(command: ActionCommand, access: PageAccess): boolean {
  if (command.action === 'goto') {
    return !command.args?.url || sameOriginUrl(command.args.url) !== window.location.href
  }
  if (command.action === 'back') return true
  if (command.expect_navigation) return true
  if (command.action !== 'click') return false
  try {
    return nativeNavigation(access().element(command.args?.ref))?.kind === 'link'
  } catch {
    // Invalid native targets are rejected when executing. Other JavaScript onclick
    // behaviour is opaque unless the server explicitly supplied expect_navigation.
    return false
  }
}

/** Where a command is expected to send the page, and the context to watch for it. */
export interface NavigationExpectation extends Partial<NavigationWatch> {
  kind: '' | 'goto' | 'link' | 'form-get' | 'form'
  url: string
}

export function navigationExpectation(command: ActionCommand, access: PageAccess): NavigationExpectation {
  try {
    if (command.action === 'goto' && command.args?.url) {
      return {kind: 'goto', url: sameOriginUrl(command.args.url),
        ...navigationWatch(globalThis.document?.documentElement as ElementLike | undefined)}
    }
    if (command.action === 'click' || command.action === 'submit') {
      const element = access().element(command.args?.ref)
      const native = nativeNavigation(element)
      const watch = navigationWatch(element)
      if (command.action === 'click' && native?.kind === 'link') {
        return {kind: 'link', url: native.url, ...watch}
      }
      if (native?.kind === 'form' && native.method === 'get') {
        const submitter = command.action === 'click' ? element as HTMLElement : null
        return {kind: 'form-get', url: formGetUrl(native, submitter), ...watch}
      }
      if (native?.kind === 'form') return {kind: 'form', url: '', ...watch}
    }
  } catch { /* executing returns the typed origin_refused receipt after claiming */ }
  // A form submission, history traversal, JavaScript click, or manual reload can have
  // arbitrary effects. A URL observed later cannot prove those effects completed.
  return {kind: '', url: ''}
}

export interface NavigationOutcome {
  navigated: boolean
  message: string
}

/**
 * Wait, within the action's deadline, for the navigation it was expected to cause.
 *
 * It does not resolve on `pagehide`: the old document is going away and cannot describe
 * the new one; the next document recovers the action from its write-ahead record instead.
 */
export function waitForNavigation({urlBefore, deadline, guardMs, context = globalThis.window,
                                   documentBefore = globalThis.document, frameElement = null,
                                   signal = null}: {
  urlBefore: string
  deadline: number
  guardMs: number | undefined
  context?: Window | null
  documentBefore?: Document | null
  frameElement?: HTMLIFrameElement | null
  signal?: NavigationSignal | null
}): Promise<NavigationOutcome> {
  if (signal?.committed || navigationCommitted(context, urlBefore, documentBefore, frameElement)) {
    return Promise.resolve({navigated: true, message: 'navigation committed'})
  }
  const configured = Math.min(Math.max(Number(guardMs || 45_000), 250), 60_000)
  const remaining = Math.max(0, Number(deadline) - Date.now() - RESULT_POST_RESERVE_MS)
  const cap = Math.min(configured, remaining)
  if (cap <= 0) return Promise.resolve({navigated: false, message: 'navigation guard exhausted'})

  return new Promise(resolve => {
    let settled = false
    const cleanups: Array<() => void> = []
    const stopAll = () => { for (const cleanup of cleanups.splice(0)) cleanup() }
    const finish = (value: NavigationOutcome) => {
      if (settled) return
      settled = true
      stopAll()
      resolve(value)
    }
    // Only a top-level unload destroys this Runtime and hands the action to the next
    // document. A same-origin iframe unload leaves this Runtime alive, so suppressing its
    // timer would strand the action forever.
    if (context === globalThis.window) {
      cleanups.push(listen('navigation pagehide', window, 'pagehide', () => {
        settled = true
        stopAll()
      }, {once: true}))
    }
    cleanups.push(every('navigation poll', 100, () => {
      if (navigationCommitted(context, urlBefore, documentBefore, frameElement)) {
        finish({navigated: true, message: 'navigation committed'})
      }
    }))
    cleanups.push(later('navigation guard', cap,
      () => finish({navigated: false, message: 'navigation guard elapsed'})))
    signal?.promise.then(finish, () => {})
  })
}
