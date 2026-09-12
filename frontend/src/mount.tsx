/**
 * Mount the panel into the host application's page.
 *
 * There is no iframe: the panel lives in the host page's realm, isolated only for style by
 * a shadow root. SECURITY.md states what that costs. What it does *not* cost is the host
 * page's ability to lock itself down, so nothing here writes `innerHTML`, injects a
 * `<style>` element or evaluates a string -- the stylesheet is adopted as a constructed
 * sheet, which `style-src` does not govern, and React builds every node.
 */
import {StrictMode} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import css from './styles/panel.scss?inline'
import {requirePanelHost, type PanelHost} from './bridge/host'
import {PANEL_PROTOCOL} from './contract/panel'
import {ErrorBoundary, guarded, PanelFailure, reportPanelFailure} from './components/ErrorBoundary'
import {PanelWindow} from './components/PanelWindow'

export interface PanelHandle {
  element: HTMLElement
  destroy(): void
}

export interface MountOptions {
  host: unknown
  /** Origins this session is bound to; links outside them are never made clickable. */
  allowedOrigins?: readonly string[]
  container?: HTMLElement
  documentRef?: Document
}

function adopt(root: ShadowRoot, text: string): void {
  try {
    const sheet = new CSSStyleSheet()
    sheet.replaceSync(text)
    root.adoptedStyleSheets = [sheet]
  } catch {
    // Older engines without constructable stylesheets fall back to an element. A host page
    // running `style-src 'self'` would block it, which is the correct, visible failure.
    const style = root.ownerDocument.createElement('style')
    style.textContent = text
    root.appendChild(style)
  }
}

export function mountPanel(options: MountOptions): PanelHandle {
  const host: PanelHost = requirePanelHost(options.host)
  if (host.configuration.protocol !== PANEL_PROTOCOL) {
    throw new Error(
      `panel protocol ${PANEL_PROTOCOL} cannot serve a session speaking ${host.configuration.protocol}`)
  }
  const documentRef = options.documentRef ?? document
  const element = documentRef.createElement('div')
  element.id = 'woa-assistant'
  // The executor must never treat its own UI as part of the application page.
  element.setAttribute('data-page-agent-not-interactive', 'true')
  element.setAttribute('role', 'complementary')
  element.setAttribute('aria-label', host.configuration.labels.assistant);
  (options.container ?? documentRef.body).appendChild(element)

  // `:host-context()` is not portable, so the document's explicit theme choice is copied
  // onto the host element and kept in step. With no choice made, the stylesheet's
  // `prefers-color-scheme` block decides, which is what most viewers get.
  const mirrorTheme = () => {
    const chosen = documentRef.documentElement.dataset.theme
    if (chosen === 'dark' || chosen === 'light') element.dataset.theme = chosen
    else delete element.dataset.theme
  }
  mirrorTheme()
  const themeWatch = new MutationObserver(guarded('theme mirror', mirrorTheme))
  themeWatch.observe(documentRef.documentElement,
                     {attributes: true, attributeFilter: ['data-theme']})

  const shadow = element.attachShadow({mode: 'open'})
  adopt(shadow, css)
  const mountPoint = documentRef.createElement('div')
  mountPoint.className = 'woa-root'
  shadow.appendChild(mountPoint)

  // React's defaults for these send an uncaught error to `reportError()`, which is the
  // page's `window.onerror` -- the host application's. Naming them keeps this root's
  // failures inside this root without the panel touching anything global.
  const root: Root = createRoot(mountPoint, {
    onUncaughtError: (error, info) => reportPanelFailure('uncaught', error, info),
    onCaughtError: (error, info) => reportPanelFailure('caught', error, info),
    onRecoverableError: (error, info) => reportPanelFailure('recovered from', error, info),
  })
  const labels = host.configuration.labels
  root.render(
    <StrictMode>
      <ErrorBoundary fallback={
        <PanelFailure text={labels.panelFailed} reloadLabel={labels.reload} />}>
        <PanelWindow host={host} allowedOrigins={options.allowedOrigins ?? []} />
      </ErrorBoundary>
    </StrictMode>,
  )

  return {
    element,
    destroy() {
      themeWatch.disconnect()
      root.unmount()
      element.remove()
    },
  }
}
