import type {StorybookConfig} from '@storybook/react-vite'

const config: StorybookConfig = {
  stories: ['../stories/**/*.stories.tsx'],
  // Storybook 10 folded the addon-essentials panels into the core, and the a11y panel is
  // gone by decision: this product's accessibility bar is the contrast gate in
  // tests/tokens.unit.test.ts, not an interactive audit tool.
  framework: {name: '@storybook/react-vite', options: {}},
  core: {disableTelemetry: true},
  // The same pin as the panel's own build (vite.config.ts): lightningcss, Vite 8's default,
  // shortens `#ffffff` to `#fff`, so the palette a reviewer sees would no longer be the one
  // design_tokens.json declares -- which is exactly what e2e/storybook.spec.ts checks.
  viteFinal: config => ({...config, build: {...config.build, cssMinify: 'esbuild'}}),
}
export default config
