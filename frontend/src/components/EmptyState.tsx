import {ChevronRight, Lightbulb, Search, Settings, Sparkles} from 'lucide-react'
import hero from '../assets/assistant-hero.webp'
import heroOnDark from '../assets/assistant-hero-dark.webp'
import type {ExampleKind, PanelExample} from '../bridge/host'
import type {Labels} from '../contract/panel'
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

/** The kinds of request a new conversation offers, in order, with their titles and glyphs. */
const KINDS: readonly {kind: ExampleKind; title: keyof Labels; Icon: typeof Search}[] = [
  {kind: 'query', title: 'exampleQuery', Icon: Search},
  {kind: 'find', title: 'exampleFind', Icon: Settings},
  {kind: 'operate', title: 'exampleOperate', Icon: Sparkles},
]

/**
 * A conversation with nothing in it yet: who is here, the question, one sentence of what it
 * can be asked, and a way in for each kind of request -- the application's own example of
 * it, and a last one that asks the assistant itself what it can do. An example fills the
 * composer rather than sending: it is the application's words, and the person may want
 * their own record or date in it. A kind the page offers no example for is left out; the
 * page's examples are the application's to declare (`runtime/examples.ts`).
 */
export function EmptyState({labels, examples, onPick}: {
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
    <div className="woa-empty">
      <div className="woa-empty__hero">
        {/* The picture comes on a pale disc of its own. The dark theme has the figure alone,
            on a glow the stylesheet draws; the stylesheet shows the one the theme wants. */}
        <img className="woa-empty__art" data-for="light" src={hero} alt="" aria-hidden="true" />
        <img className="woa-empty__art" data-for="dark" src={heroOnDark} alt="" aria-hidden="true" />
        <div className="woa-empty__copy">
          <p className="woa-empty__intro">{labels.emptyIntro}</p>
          <h3>{labels.emptyGreeting}</h3>
          <p className="woa-empty__detail">{labels.emptyDetail}</p>
        </div>
      </div>
      <ul className="woa-examples">
        {ways.map(({kind, title, text, Icon}) => (
          <li key={kind}>
            <button type="button" className="woa-example" data-kind={kind}
                    onClick={() => onPick(text)}>
              <span className="woa-example__tile" aria-hidden="true">
                <Icon {...iconProps(16)} />
              </span>
              <strong className="woa-example__title">{title}</strong>
              <ChevronRight {...iconProps(14)} className="woa-example__chevron" />
              <span className="woa-example__text"><Quoted text={text} /></span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
