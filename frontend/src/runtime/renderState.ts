/** A route change is not proof that the destination view has replaced the old one. */
import {canonicalUrl} from './navigation'
import type {PageObservation} from './types'

type Projection = Pick<PageObservation, 'url' | 'title' | 'content'>
export interface PendingView { originUrl: string; projection: string }

function view(value: Projection): string {
  return `${value.title}\u0000${value.content}`
}

export function renderState(previous: Projection | null, current: Projection,
                            pendingView: PendingView | null = null,
                            applicationBlocked = false):
    {state: 'pending' | 'ready' | 'blocked'; pendingView: PendingView | null} {
  const currentView = view(current)
  const returnedToOrigin = Boolean(pendingView
    && canonicalUrl(current.url) === pendingView.originUrl)
  let pending = returnedToOrigin ? null : pendingView
  if (!pending && !returnedToOrigin && previous
      && canonicalUrl(previous.url) !== canonicalUrl(current.url)
      && view(previous) === currentView) {
    pending = {originUrl: canonicalUrl(previous.url), projection: currentView}
  }
  if (pending && currentView !== pending.projection) pending = null
  return {state: applicationBlocked ? 'blocked' : pending ? 'pending' : 'ready',
    pendingView: pending}
}
