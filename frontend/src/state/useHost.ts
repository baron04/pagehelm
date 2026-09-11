import {useCallback, useSyncExternalStore} from 'react'
import type {PanelHost, PanelSnapshot} from '../bridge/host'

/** Subscribe to the Runtime's store without a second copy of its state in React. */
export function useHostSnapshot(host: PanelHost): PanelSnapshot {
  const subscribe = useCallback((listener: () => void) => host.subscribe(listener), [host])
  const read = useCallback(() => host.snapshot(), [host])
  return useSyncExternalStore(subscribe, read, read)
}
