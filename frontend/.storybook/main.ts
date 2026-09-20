import type {StorybookConfig} from '@storybook/react-vite'

const config: StorybookConfig = {
  stories: ['../stories/**/*.stories.tsx'],
  // Storybook 10 folded the addon-essentials panels into the core, and the a11y panel is
  // gone by decision: this product's accessibility bar is the contrast gate in
  // tests/test_panel_contract.py, not an interactive audit tool.
  framework: {name: '@storybook/react-vite', options: {}},
  core: {disableTelemetry: true},
}
export default config
