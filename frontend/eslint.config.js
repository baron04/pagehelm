import js from '@eslint/js'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import hooks from 'eslint-plugin-react-hooks'

export default [
  {ignores: ['dist', 'storybook-static', 'src/styles/_tokens.generated.scss', 'node_modules',
    'test-results', 'playwright-report', 'docs/.astro', 'docs/dist', 'docs/node_modules',
    'docs/public/llms*.txt']},
  js.configs.recommended,
  {
    files: ['scripts/**/*.mjs'],
    languageOptions: {globals: {process: 'readonly'}},
  },
  {
    files: ['docs/**/*.mjs'],
    languageOptions: {globals: {console: 'readonly', process: 'readonly', URL: 'readonly'}},
  },
  {
    files: ['src/integrations/**/*.js', 'tests/**/*.js'],
    languageOptions: {globals: {window: 'readonly', document: 'readonly', URL: 'readonly',
      location: 'readonly', crypto: 'readonly', TextEncoder: 'readonly',
      Element: 'readonly', HTMLElement: 'readonly', HTMLButtonElement: 'readonly',
      HTMLInputElement: 'readonly', HTMLLabelElement: 'readonly',
      HTMLTableCellElement: 'readonly', setTimeout: 'readonly', Event: 'readonly',
      MouseEvent: 'readonly', PointerEvent: 'readonly'}},
  },
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsparser,
      parserOptions: {ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: {jsx: true}},
      globals: {window: 'readonly', document: 'readonly', console: 'readonly',
        fetch: 'readonly', setTimeout: 'readonly', clearTimeout: 'readonly',
        AbortController: 'readonly', IntersectionObserver: 'readonly',
        MessageEvent: 'readonly', HTMLElement: 'readonly', HTMLTextAreaElement: 'readonly',
        HTMLButtonElement: 'readonly', Element: 'readonly', trustedTypes: 'readonly',
        performance: 'readonly', URL: 'readonly', URLSearchParams: 'readonly',
        queueMicrotask: 'readonly', process: 'readonly', structuredClone: 'readonly'},
    },
    plugins: {'@typescript-eslint': tseslint, 'react-hooks': hooks},
    rules: {
      ...tseslint.configs.recommended.rules,
      ...hooks.configs.recommended.rules,
      'no-undef': 'off',
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      'eqeqeq': ['error', 'always'],
      'no-console': ['error', {allow: ['warn', 'error']}],
    },
  },
  {
    // The Runtime shares its JavaScript realm with the host page (no iframe): an
    // uncaught error in a raw timer or listener reaches the host's own error handler --
    // Odoo answers with its "Oops!" dialog. `contain.ts` is the one place a failure is
    // caught before it can escape; this rule keeps every other module in this directory
    // from reaching around it.
    files: ['src/runtime/**/*.ts'],
    ignores: ['src/runtime/contain.ts'],
    rules: {
      'no-restricted-globals': ['error',
        {name: 'setTimeout', message: "use later() from './contain'"},
        {name: 'setInterval', message: "use every() from './contain'"},
        {name: 'queueMicrotask', message: "use soon() from './contain'"},
      ],
      'no-restricted-syntax': ['error',
        {selector: "CallExpression[callee.property.name='addEventListener']",
          message: "use listen() from './contain'"},
      ],
    },
  },
  {
    // The Runtime and the panel ship in one file, but the Runtime still holds no view: it
    // hands the panel a host and a container, and the panel renders. `panelBridge.ts` is the
    // one place that mounts it; nothing else in this directory reaches for React or for the
    // panel's components.
    files: ['src/runtime/**/*.ts'],
    ignores: ['src/runtime/panelBridge.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        patterns: [{
          group: ['react', 'react/*', 'react-dom', 'react-dom/*',
                  '../mount', '../components/*', '../state/*'],
          message: 'the Runtime holds no view: mount the panel through panelBridge.ts',
        }],
      }],
    },
  },
  {
    files: ['src/runtime/panelBridge.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        patterns: [{
          group: ['react', 'react/*', 'react-dom', 'react-dom/*', '../components/*'],
          message: 'panelBridge.ts mounts the panel through ../mount and nothing else',
        }],
      }],
    },
  },
  {
    // Playwright's fixtures hand the test a function named `use`; it is not React's hook.
    files: ['e2e/**/*.ts'],
    rules: {'react-hooks/rules-of-hooks': 'off'},
  },
]
