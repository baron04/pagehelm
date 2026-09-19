import {afterEach, describe, expect, it, vi} from 'vitest'
import {ENTRY_ID, failureSentence, standingEntry} from '../src/runtime/entry'

afterEach(() => { document.body.innerHTML = '' })

describe('failureSentence says the cause, then what happens next, then the status', () => {
  it('tells a refusal the person cannot retry away from an outage that retries itself', () => {
    expect(failureSentence({status: 403, retrying: false}))
      .toBe('当前账号未获授权使用助手（403）。')
    expect(failureSentence({status: 503, retrying: true}))
      .toBe('助手暂时不可用，正在重试（503）。')
    expect(failureSentence({status: 409, retrying: false}))
      .toBe('助手正忙，可以点重试（409）。')
    expect(failureSentence({status: null, retrying: false}))
      .toBe('助手暂时不可用，可以点重试。')
  })
})

describe('the standing entry', () => {
  const shadow = () => document.getElementById(ENTRY_ID)!.shadowRoot!

  it('is created on first use, labelled, and removed without a trace', () => {
    const entry = standingEntry(document)
    expect(document.getElementById(ENTRY_ID)).toBeNull()
    entry.connecting()
    expect(shadow().querySelector('.badge')!.getAttribute('aria-label')).toBe('AI 助手正在连接')
    expect(shadow().querySelector<HTMLButtonElement>('.note button')!.hidden).toBe(true)
    entry.remove()
    expect(document.getElementById(ENTRY_ID)).toBeNull()
  })

  it('offers a retry that reaches the supervisor once per click', () => {
    const entry = standingEntry(document)
    const retry = vi.fn()
    entry.unavailable({status: 503, retrying: true}, retry)
    const root = shadow()
    root.querySelector<HTMLButtonElement>('.badge')!.click()
    expect(root.querySelector<HTMLElement>('.note')!.hidden).toBe(false)
    expect(root.querySelector('.note p')!.textContent).toContain('正在重试（503）')
    const button = root.querySelector<HTMLButtonElement>('.note button')!
    button.click()
    expect(retry).toHaveBeenCalledTimes(1)
    expect(button.disabled).toBe(true)
    entry.unavailable({status: 503, retrying: false}, retry)
    expect(button.disabled).toBe(false)
    expect(document.querySelectorAll(`#${ENTRY_ID}`)).toHaveLength(1)
  })

  it('keeps a failing click handler away from the host page', () => {
    const entry = standingEntry(document)
    const hostErrors: unknown[] = []
    const onError = (event: ErrorEvent) => { hostErrors.push(event.error) }
    window.addEventListener('error', onError)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    entry.unavailable({status: 500, retrying: false}, () => { throw new Error('boom') })
    shadow().querySelector<HTMLButtonElement>('.note button')!.click()
    window.removeEventListener('error', onError)
    expect(hostErrors).toEqual([])
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
