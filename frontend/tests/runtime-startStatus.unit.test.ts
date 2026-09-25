import {describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {PanelHost, type PanelActions} from '../src/runtime/panelHost'
import {START_FAILED, failureSentence, panelStartStatus} from '../src/runtime/startStatus'

function actions(): PanelActions {
  return {
    submit: vi.fn(), cancel: vi.fn(), decide: vi.fn(),
    newConversation: vi.fn(), loadOlder: vi.fn(),
  }
}

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

describe('a start is reported in the panel, the one place the assistant speaks', () => {
  it('says why it failed above the composer, keeps what was sent, and retries on Retry', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const status = panelStartStatus(host)
    host.setOptimistic({turnId: '', text: '列出订单', failed: false})
    const retry = vi.fn()

    status.unavailable({status: 403, retrying: false}, retry)

    const snapshot = host.snapshot()
    expect(snapshot.notice).toEqual({
      code: START_FAILED, retryable: true, message: '当前账号未获授权使用助手（403）。'})
    expect(snapshot.connection.state).toBe('error')
    // What the person sent is kept, marked as not delivered, to be sent again.
    expect(snapshot.optimistic).toEqual({turnId: '', text: '列出订单', failed: true})
    host.reload()
    expect(retry).toHaveBeenCalledTimes(1)
    expect(host.snapshot().notice).toBeNull()
  })

  it('clears only its own notice once a session is up, and Retry reloads again after', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const status = panelStartStatus(host)
    status.unavailable({status: null, retrying: true}, () => {})
    expect(host.snapshot().connection.state).toBe('reconnecting')
    status.started()
    expect(host.snapshot().notice).toBeNull()
    host.raise('offline', {retryable: false})
    status.started()
    expect(host.snapshot().notice?.code).toBe('offline')
  })
})

describe('the panel outlives sessions: a Runtime binds its verbs and gives them back', () => {
  it("answers with the page's verbs until a Runtime binds, and again after it releases", () => {
    const page = actions()
    const runtime = actions()
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: page})
    host.submit('一')
    const release = host.bind(runtime)
    host.submit('二')
    // A stale release from an earlier Runtime cannot unbind a newer one.
    const newer = actions()
    const releaseNewer = host.bind(newer)
    release()
    host.submit('三')
    releaseNewer()
    host.submit('四')
    expect(vi.mocked(page.submit).mock.calls).toEqual([['一'], ['四']])
    expect(vi.mocked(runtime.submit).mock.calls).toEqual([['二']])
    expect(vi.mocked(newer.submit).mock.calls).toEqual([['三']])
  })

  it("takes the server's expiry and keeps the copy it was built with", () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const seen = vi.fn()
    host.subscribe(seen)
    host.adoptServerConfiguration({
      ...DEFAULT_CONFIGURATION, session_expires_at: 5, principal_expires_at: 6})
    expect(host.configuration.session_expires_at).toBe(5)
    expect(host.configuration.principal_expires_at).toBe(6)
    expect(host.configuration.labels).toBe(DEFAULT_CONFIGURATION.labels)
    // The panel re-reads the configuration on the next render it is told about.
    expect(seen).toHaveBeenCalled()
    expect(() => host.adoptServerConfiguration({...DEFAULT_CONFIGURATION, protocol: 999}))
      .toThrow()
  })
})
