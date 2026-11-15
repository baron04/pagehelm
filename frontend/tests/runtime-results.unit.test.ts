import {describe, expect, it, vi} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {completed, executionTraceOf, indeterminate, receipt, rejected, safeErrorDetail} from '../src/runtime/results'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import type {ActionEnvelope, ActionResultEnvelope, ObservationEnvelope} from '../src/runtime/types'

const redactor = createRedactor()

describe('private controller execution diagnostics', () => {
  const trace = {trace_id:'trace-1',events:[{sequence:1,operation:'fill',path:'ordinary'}]}
  it('carries the whitelist through dispatch and settlement without changing page observations', async () => {
    const before = observation(1), after = observation()
    const sent: ActionResultEnvelope[] = []
    const command = action()
    const runtime = Object.assign(Object.create(EmbeddedWebRuntime.prototype, {redactor:{value:redactor}}), {
      config:undefined, stored:{pageWorked:true,inFlight:command}, writeGuardUntil:0,
      currentObservation:before, _save:vi.fn(), _mayNavigate:()=>false,
      _execute:vi.fn(async()=>({success:true,execution_trace:trace})),
      observe:vi.fn(async()=>after), _deliver:vi.fn(async(result: ActionResultEnvelope)=>{sent.push(result)}),
    }) as {_dispatch(...args: unknown[]): Promise<unknown>; _settle(...args: unknown[]): Promise<void>}
    const outcome = await runtime._dispatch(command, command, {})
    await runtime._settle(command, before, outcome)
    expect(sent[0]?.receipt?.execution_trace).toEqual(trace)
    expect(sent[0]?.observation_after).toEqual(after)
    expect(sent[0]?.receipt?.observation).toEqual(after.observation)
    expect(JSON.stringify(after)).not.toContain('trace-1')
  })

  it('drops malformed or failing diagnostics without replacing the business result', () => {
    const facts = {ok:false,code:'page_error',detail:'original failure',observation:observation(),
      navigated:false,elapsedMs:1,urlBefore:''}
    const original = receipt(action(),facts,redactor)
    for (const diagnostic of [null,7,{...trace,events:[{sequence:1,operation:'click',path:'binding'}]},
      {...trace,events:[{sequence:1,operation:'fill',path:'unknown'}]}]) {
      expect(receipt(action(),{...facts,execution_trace:diagnostic},redactor)).toEqual(original)
    }
    const broken = {...facts,get execution_trace(): unknown {throw new Error('collector failure')}}
    expect(receipt(action(),broken,redactor)).toEqual(original)
    expect(executionTraceOf({get execution_trace(){throw new Error('collector failure')}},'fill')).toBeUndefined()
    expect(executionTraceOf({execution_trace:trace},'goto')).toBeUndefined()
    expect(executionTraceOf({execution_trace:{...trace,secret:'discard',events:[{...trace.events[0],secret:'discard'}]}},'fill'))
      .toEqual(trace)
  })
})

function action(overrides: Partial<ActionEnvelope> = {}): ActionEnvelope {
  return {
    schema_version: 1, session_id: 's1', turn_id: 't1', action_id: 'a1',
    based_on_observation_id: 'o1', page_epoch: 1, deadline: Date.now() + 1000,
    command_digest: '0'.repeat(64),
    command: {command_id: 'a1', action: 'fill', args: {ref: 'e0', value: 'hunter2'},
      expect_navigation: false},
    started_url_display: 'https://app.test/form',
    ...overrides,
  }
}

function observation(epoch = 2): ObservationEnvelope {
  return {
    schema_version: 1, session_id: 's1', observation_id: 'o2', page_epoch: epoch,
    observation: {schema_version: 1, epoch, url: 'https://app.test/form', title: 't',
      landmarks: [], elements: [], content: '', messages: [], truncated: false},
    events: [], caused_by_action_id: 'a1',
  }
}

describe('safeErrorDetail redacts and bounds an error message', () => {
  it('removes credentials and the value the command typed, then bounds the length', () => {
    const out = safeErrorDetail('failed to fill hunter2: password=hunter2',
      {command_id: 'a1', action: 'fill', args: {value: 'hunter2'}, expect_navigation: false},
      redactor)
    expect(out).not.toContain('hunter2')
    expect(out.length).toBeLessThanOrEqual(800)
  })

  it('handles a command with no args at all', () => {
    expect(safeErrorDetail('plain failure', null, redactor)).toBe('plain failure')
  })

  it('also removes a nested form handoff value from an unexpected error', () => {
    const command = {command_id: 'a2', action: 'handoff_form',
      args: {fields: [{field: 'phone', value: 'private-value-123'}]},
      expect_navigation: false}
    expect(safeErrorDetail('private-value-123 was not accepted', command, redactor))
      .toBe('[redacted] was not accepted')
  })
})

describe('result builders share one envelope header', () => {
  it('rejected carries no receipt and the given code/detail', () => {
    const result = rejected(action(), observation(1), 'stale_observation', 'the page changed')
    expect(result).toMatchObject({
      status: 'rejected', action_id: 'a1', session_id: 's1', turn_id: 't1',
      error_code: 'stale_observation', error_detail: 'the page changed', receipt: null,
    })
    expect(result.observation_after).toEqual(observation(1))
  })

  it('indeterminate always uses execution_state_unknown', () => {
    const result = indeterminate(action(), observation(), 'takeover mid-flight')
    expect(result.status).toBe('indeterminate')
    expect(result.error_code).toBe('execution_state_unknown')
  })

  it('completed carries the receipt and no error', () => {
    const built = receipt(action(), {
      ok: true, code: '', detail: '', observation: observation(),
      navigated: false, elapsedMs: 42, urlBefore: '',
    }, redactor)
    const result = completed(action(), built, observation())
    expect(result.status).toBe('completed')
    expect(result.error_code).toBe('')
    expect(result.receipt).toBe(built)
  })
})

describe('receipt redacts the typed value and reports elapsed time and navigation', () => {
  it('never echoes the typed value, even through args_echo', () => {
    const built = receipt(action(), {
      ok: false, code: 'element_not_actionable', detail: 'value hunter2 rejected',
      observation: observation(), navigated: false, elapsedMs: 10, urlBefore: '',
    }, redactor)
    expect(JSON.stringify(built)).not.toContain('hunter2')
    expect(built.args_echo.value).toBe('[redacted]')
  })

  it('clamps a negative or missing elapsed time to zero', () => {
    const built = receipt(action(), {
      ok: true, code: '', detail: '', observation: observation(),
      navigated: false, elapsedMs: -50, urlBefore: '',
    }, redactor)
    expect(built.elapsed_ms).toBe(0)
  })

  it('falls back to the supplied urlBefore only when the action has no started_url_display', () => {
    const built = receipt(action({started_url_display: undefined as unknown as string}), {
      ok: true, code: '', detail: '', observation: observation(),
      navigated: true, elapsedMs: 5, urlBefore: 'https://app.test/fallback',
    }, redactor)
    expect(built.url_before).toBe('https://app.test/fallback')
  })
})
