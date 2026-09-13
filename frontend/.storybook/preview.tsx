import {useLayoutEffect, useRef, useState, type ReactNode} from 'react'
import {createPortal} from 'react-dom'
import type {Decorator, Preview} from '@storybook/react'
import css from '../src/styles/panel.scss?inline'

/**
 * Stories render the panel's own components, mounted the way `mountPanel` mounts them: inside
 * a shadow root with the stylesheet adopted. The palette, the type and the shadows are all
 * declared on `:host`, so a story rendered into the document itself would show none of them.
 * Everything else -- the contract, the labels, the bounds -- comes from the same files the
 * server sends.
 */
function ShadowHost(props: {theme: string; overlay: boolean; children: ReactNode}) {
  const host = useRef<HTMLDivElement>(null)
  const [mountPoint, setMountPoint] = useState<HTMLElement | null>(null)

  useLayoutEffect(() => {
    const element = host.current
    if (!element) return
    const shadow = element.shadowRoot ?? element.attachShadow({mode: 'open'})
    const sheet = new CSSStyleSheet()
    sheet.replaceSync(css)
    shadow.adoptedStyleSheets = [sheet]
    let point = shadow.querySelector<HTMLElement>('.woa-root')
    if (!point) {
      point = document.createElement('div')
      point.className = 'woa-root'
      shadow.appendChild(point)
    }
    setMountPoint(point)
  }, [])

  // A whole window keeps the shipped host, a transparent overlay across the viewport. A single
  // piece sits where Storybook centres it, so its host joins the flow and stays pressable.
  const style = props.overlay ? undefined : {
    position: 'relative', inset: 'auto', zIndex: 'auto', pointerEvents: 'auto',
    minWidth: 92, minHeight: 92,
  } as const
  return (
    <div ref={host} id="woa-assistant" data-theme={props.theme} style={style}>
      {mountPoint && createPortal(props.children, mountPoint)}
    </div>
  )
}

const inShadowRoot: Decorator = (Story, context) => (
  <ShadowHost theme={String(context.globals.theme ?? 'light')}
              overlay={context.parameters.layout === 'fullscreen'}>
    <Story />
  </ShadowHost>
)

const preview: Preview = {
  parameters: {
    layout: 'centered',
    backgrounds: {
      default: 'application',
      values: [
        {name: 'application', value: '#eceef2'},
        {name: 'dark', value: '#0b0d10'},
      ],
    },
    a11y: {test: 'error'},
  },
  globalTypes: {
    theme: {
      description: 'Panel theme',
      defaultValue: 'light',
      toolbar: {items: ['light', 'dark'], dynamicTitle: true},
    },
  },
  decorators: [inShadowRoot],
}
export default preview
