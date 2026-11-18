import {useEffect, useState} from 'react'
import {Check, ClipboardCheck, LocateFixed, Undo2} from 'lucide-react'
import type {
  HandoffCardFacts, HandoffFieldSummary, HandoffRestrictedControl, Labels,
} from '../contract/panel'
import {iconProps} from './icon'

export interface HandoffCardProps {
  handoff: HandoffCardFacts
  turnId: string
  labels: Labels
  onUndoFormFill?: ((turnId: string) => Promise<boolean> | boolean) | undefined
  onFocusHandoffControl?: ((control: HandoffRestrictedControl) => Promise<boolean> | boolean) | undefined
}

function HandoffFieldSection({title, fields, testId}: {
  title: string
  fields: readonly HandoffFieldSummary[]
  testId: string
}): React.JSX.Element | null {
  if (fields.length === 0) return null
  return (
    <div className="woa-handoff__section" data-testid={testId}>
      <h4 className="woa-handoff__section-title">{title}</h4>
      <dl className="woa-handoff__fields">
        {fields.map((field, index) => (
          <div className="woa-handoff__field" key={`${index}-${field.name}`}>
            <dt>{field.name}</dt>
            {field.value !== undefined && field.value !== '' && <dd>{field.value}</dd>}
          </div>
        ))}
      </dl>
    </div>
  )
}

/** Structured handoff summary card showing completed/pending fields, restricted control pointers, and form undo. */
export function HandoffCard({
  handoff, turnId, labels, onUndoFormFill, onFocusHandoffControl,
}: HandoffCardProps): React.JSX.Element {
  const [undoing, setUndoing] = useState(false)
  const [localUndone, setLocalUndone] = useState(false)

  useEffect(() => {
    setLocalUndone(false)
    setUndoing(false)
  }, [turnId, handoff.undone])

  const undone = localUndone || Boolean(handoff.undone)
  const showUndo = (Boolean(handoff.can_undo) && Boolean(onUndoFormFill)) || undone

  const handleUndo = async () => {
    if (!onUndoFormFill || undoing || undone) return
    setUndoing(true)
    try {
      const ok = await onUndoFormFill(turnId)
      if (ok === true) {
        setLocalUndone(true)
      }
    } catch {
      // Keep the undo action retryable when host restoration fails.
    } finally {
      setUndoing(false)
    }
  }

  const handleLocate = (control: HandoffRestrictedControl) => {
    if (!onFocusHandoffControl) return
    try {
      void Promise.resolve(onFocusHandoffControl(control)).catch(() => undefined)
    } catch {
      // Host focus errors should not escape into the host page realm.
    }
  }

  return (
    <section className="woa-handoff" data-testid="woa-handoff" aria-label={labels.handoffTitle}>
      <h3 className="woa-handoff__title">
        <ClipboardCheck {...iconProps(14)} />
        <span>{labels.handoffTitle}</span>
      </h3>

      <HandoffFieldSection
        title={labels.handoffCompletedFields}
        fields={handoff.completed_fields}
        testId="woa-handoff-completed"
      />

      <HandoffFieldSection
        title={labels.handoffPendingFields}
        fields={handoff.pending_fields}
        testId="woa-handoff-pending"
      />

      {handoff.restricted_controls && handoff.restricted_controls.length > 0 && (
        <ul className="woa-handoff__controls" data-testid="woa-handoff-controls">
          {handoff.restricted_controls.map((control, index) => (
            <li className="woa-handoff__control" key={`${index}-${control.ref ?? control.label}`}>
              <span className="woa-handoff__control-label">{control.label}</span>
              {onFocusHandoffControl && (
                <button
                  type="button"
                  className="woa-btn woa-btn--ghost woa-handoff__locate"
                  onClick={() => handleLocate(control)}
                >
                  <LocateFixed {...iconProps(14)} />
                  <span>{labels.handoffLocateControl}</span>
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {showUndo && (
        <div className="woa-handoff__actions">
          <button
            type="button"
            className="woa-btn woa-btn--ghost woa-handoff__undo"
            data-undone={undone ? 'true' : undefined}
            disabled={undone || undoing}
            onClick={() => { void handleUndo() }}
          >
            {undone ? <Check {...iconProps(14)} /> : <Undo2 {...iconProps(14)} />}
            <span>{undone ? labels.handoffUndoDone : labels.handoffUndoFill}</span>
          </button>
        </div>
      )}
    </section>
  )
}
