import type {Preview} from '@storybook/react'
import '../src/styles/panel.scss'

/**
 * Stories render the panel's own components directly, so the stylesheet is imported rather
 * than adopted into a shadow root. Everything else -- the contract, the labels, the bounds
 * -- comes from the same files the server sends.
 */
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
  decorators: [(Story, context) => {
    document.documentElement.dataset.theme = String(context.globals.theme ?? 'light')
    return Story()
  }],
}
export default preview
