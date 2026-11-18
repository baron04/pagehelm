/**
 * The panel as the page has it: up from the moment the script runs, whether or not a session
 * ever starts.
 *
 * A shell is a container, a `PanelHost` and the mounted panel. It outlives every Runtime that
 * binds to it: a session that starts takes the panel's intents, and one that ends gives them
 * back, while the launcher and the conversation on screen stay where they are. So there is
 * one entry to the assistant, the panel's own launcher, and never a second thing standing in
 * for it while a session is being made or after one failed.
 */
import type {PanelExample} from '../bridge/host'
import {DEFAULT_CONFIGURATION, type PanelConfiguration} from '../contract/panel'
import {createPanelContainer, keepAttached, mountPanel} from './panelBridge'
import {PanelHost, type PanelActions} from './panelHost'

export interface PanelShell {
  readonly host: PanelHost
  readonly container: HTMLDivElement
  destroy(): void
}

/** Verbs for a shell nothing answers yet: a Runtime that owns its shell binds before use. */
const INERT_ACTIONS: PanelActions = {
  submit() {}, cancel() {}, decide() {}, newConversation() {}, loadOlder() {},
}

export function createShell(actions: PanelActions = INERT_ACTIONS,
                            configuration: PanelConfiguration = DEFAULT_CONFIGURATION,
                            examples: readonly PanelExample[] = []): PanelShell {
  const container = createPanelContainer()
  let alive = true
  const observer = keepAttached(container, () => alive)
  const host = new PanelHost({configuration, actions, examples})
  const handle = mountPanel(host, container)
  return {
    host,
    container,
    destroy() {
      if (!alive) return
      alive = false
      observer?.disconnect()
      handle.destroy()
      container.remove()
    },
  }
}
