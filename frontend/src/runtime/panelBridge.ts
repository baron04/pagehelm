/**
 * Where the panel lives on the host page, and how it gets there.
 *
 * The container exists before PageController does, so the agent's own view of the page
 * can exclude it. It sits under the document root, removed from layout, because
 * application frameworks own and sometimes replace body children -- and reattaching it
 * afterwards still shows a blink where page and panel both repaint.
 */
import {contained} from './contain'
import type {PanelHost} from './panelHost'

/** What `mountPanel` returns: the one handle the Runtime keeps on the panel it mounted. */
export interface PanelHandle { destroy(): void }

interface PanelModule {
  mountPanel(options: {host: PanelHost; allowedOrigins: string[]; container: Element}): PanelHandle
}

export function createPanelContainer(): HTMLDivElement {
  const container = document.createElement('div')
  container.id = 'woa-assistant-root'
  container.setAttribute('data-page-agent-not-interactive', 'true')
  // A non-interactive parent does not exclude its descendants or shadow text.
  container.setAttribute('data-page-agent-ignore', 'true')
  // Explicitly removed from layout: an unstyled sibling made Odoo's body lose part of the
  // viewport where its root stylesheet treated document children as flex items. The
  // mounted shadow child stays fixed to the viewport and opts into pointer events itself.
  for (const [name, value] of Object.entries({
    position: 'fixed', inset: '0', width: '0', height: '0',
    'min-width': '0', 'min-height': '0', flex: 'none', overflow: 'visible',
    'pointer-events': 'none',
  })) {
    container.style.setProperty(name, value, 'important')
  }
  ;(document.documentElement || document.body)?.appendChild(container)
  return container
}

/**
 * Keep the container attached under the document root, whatever the host does to it.
 * Returns the observer so the Runtime can disconnect it when it stops.
 */
export function keepAttached(container: HTMLElement, alive: () => boolean): MutationObserver | null {
  if (typeof MutationObserver === 'undefined') return null
  const observer = new MutationObserver(contained('panel attachment', () => {
    if (!alive()) return
    const parent = document.documentElement || document.body
    if (!parent) return
    if (!container.isConnected || container.parentElement !== parent) {
      parent.appendChild(container)
    }
  }))
  if (document.documentElement) {
    observer.observe(document.documentElement, {childList: true})
  }
  return observer
}

/** Load the panel module and mount it into the container, fed by `host`. */
export async function mountPanel(moduleUrl: string, host: PanelHost,
                                 container: Element): Promise<PanelHandle> {
  const panel = await import(/* @vite-ignore */ moduleUrl) as PanelModule
  return panel.mountPanel({host, allowedOrigins: [location.origin], container})
}
