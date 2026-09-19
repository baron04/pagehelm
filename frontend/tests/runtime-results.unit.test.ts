import {describe, expect, it} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {completed, indeterminate, receipt, rejected, safeErrorDetail} from '../src/runtime/results'
import type {ActionEnvelope, ObservationEnvelope} from '../src/runtime/types'

const redactor = createRedactor()

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
