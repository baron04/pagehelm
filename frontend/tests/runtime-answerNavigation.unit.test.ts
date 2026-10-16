import {afterEach, describe, expect, it, vi} from 'vitest'
import {hasLocalDraft, tryOpenAnswerInPlace} from '../src/runtime/answerNavigation'

afterEach(() => { document.body.replaceChildren() })

function fallbackWindow() {
  const tab = {closed: false, opener: window as Window | null,
    close: vi.fn(), location: {replace: vi.fn()}}
  const view = {open: vi.fn(() => tab)} as unknown as Window
  return {tab, view}
}

describe('a user click on a structured answer link', () => {
  it('preserves an unsubmitted input and an open application dialog', () => {
    const input = document.createElement('input')
    document.body.append(input)
    input.value = 'unsaved'
    const opener = vi.fn(async () => true)
    const {view} = fallbackWindow()
    expect(hasLocalDraft(document)).toBe(true)
    expect(tryOpenAnswerInPlace('/contacts/grace', opener, document, view)).toBe(false)
    expect(opener).not.toHaveBeenCalled()
    expect(view.open).not.toHaveBeenCalled()
    input.remove()
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.append(dialog)
    expect(hasLocalDraft(document)).toBe(true)
  })

  it('uses the application route when clean and closes its reserved fallback', async () => {
    const {tab, view} = fallbackWindow()
    const opener = vi.fn(async () => true)
    expect(tryOpenAnswerInPlace('/contacts/grace', opener, document, view)).toBe(true)
    await vi.waitFor(() => expect(tab.close).toHaveBeenCalledOnce())
    expect(opener).toHaveBeenCalledWith('/contacts/grace')
    expect(tab.location.replace).not.toHaveBeenCalled()
    expect(tab.opener).toBeNull()
  })

  it('sends the reserved tab to the application route when its router refuses', async () => {
    const {tab, view} = fallbackWindow()
    expect(tryOpenAnswerInPlace('/contacts/grace', async () => false, document, view)).toBe(true)
    await vi.waitFor(() => expect(tab.location.replace).toHaveBeenCalledWith('/contacts/grace'))
    expect(tab.close).not.toHaveBeenCalled()
  })
})
