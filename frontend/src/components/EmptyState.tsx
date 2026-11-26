import {Lightbulb, Search, Sparkles, SquarePen} from 'lucide-react'
import type {ExampleKind, PanelExample} from '../bridge/host'
import type {Labels} from '../contract/panel'
import avatar from '../assets/assistant-avatar.webp'
import {iconProps} from './icon'

/** Full-width punctuation that can end an example. */
const CLOSING = /[？！。，；：…]$/u

/**
 * An example in quotation marks. A full-width mark carries its own blank half, so one that
 * ends the sentence stood a gap away from the closing quote; that last mark alone is set
 * half-width, and the ones inside the sentence keep their room.
 */
function Quoted({text}: {text: string}): React.JSX.Element {
  if (!CLOSING.test(text)) return <>“{text}”</>
  const last = [...text].at(-1)!
  return <>“{text.slice(0, -last.length)}<span className="woa-example__halt">{last}</span>”</>
}

/** The kinds of request a new conversation offers, in order, with their titles and glyphs.
 *  The order is the design's: querying, operating pages, editing data, then asking the assistant itself. */
const KINDS: readonly {kind: ExampleKind; title: keyof Labels; Icon: typeof Search}[] = [
  {kind: 'query', title: 'exampleQuery', Icon: Search},
  {kind: 'operate', title: 'exampleOperate', Icon: SquarePen},
  {kind: 'edit', title: 'exampleEdit', Icon: Sparkles},
]

/**
 * Who is here, said as the assistant introducing itself: its face on a soft halo, and one
 * line beside it. The picture is the same portrait the launcher and the title bar wear, so
 * a new conversation opens on the one face the panel has.
 */
export function WelcomeHeading({labels}: {labels: Labels}): React.JSX.Element {
  return (
    <div className="woa-empty__hero">
      <div className="woa-empty__hello">
        <span className="woa-empty__face">
          <img src={avatar} alt="" aria-hidden="true" />
        </span>
        <p className="woa-empty__intro">{labels.emptyIntro}</p>
      </div>
      <h3>{labels.emptyGreeting}</h3>
      <p className="woa-empty__detail">{labels.emptyDetail}</p>
    </div>
  )
}

/** Application-owned prompts fill the draft for editing; missing kinds are not offered. */
export function ExamplePrompts({labels, examples, onPick}: {
  labels: Labels
  examples: readonly PanelExample[]
  onPick: (text: string) => void
}): React.JSX.Element {
  const offered = KINDS.flatMap(({kind, title, Icon}) => {
    const example = examples.find(item => item.kind === kind)
    return example ? [{kind, title: labels[title], text: example.text, Icon}] : []
  })
  const ways = [...offered,
    {kind: 'more', title: labels.exampleMore, text: labels.exampleMorePrompt, Icon: Lightbulb}]
  return (
    <ul className="woa-examples">
      {ways.map(({kind, title, text, Icon}) => (
        <li key={kind}>
          <button type="button" className="woa-example" data-kind={kind}
                  onClick={() => onPick(text)}>
            {/* The kind is a small tinted pill: the glyph and its name sit on one colour that
                is the request's own, and hover raises that same colour into the words. */}
            <span className="woa-example__badge">
              <Icon {...iconProps(12, 2)} aria-hidden="true" />
              <span className="woa-example__title">{title}</span>
            </span>
            <span className="woa-example__text"><Quoted text={text} /></span>
          </button>
        </li>
      ))}
    </ul>
  )
}
