import js from '@eslint/js'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import hooks from 'eslint-plugin-react-hooks'

export default [
  {ignores: ['dist', 'storybook-static', 'src/styles/_tokens.generated.scss', 'node_modules']},
  js.configs.recommended,
  {
    files: ['scripts/**/*.mjs'],
    languageOptions: {globals: {process: 'readonly'}},
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
    // The Runtime is what loads and speaks before any view exists, and the entry note it
    // renders is the whole interface until the server says otherwise. React arriving in
    // this directory would quietly merge the two modules the build keeps separate, so
    // the import itself is what gets refused here, not just the mistake it would cause.
    files: ['src/runtime/**/*.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        patterns: [{
          group: ['react', 'react/*', 'react-dom', 'react-dom/*'],
          message: 'the Runtime holds no view: panel.js imports React, not src/runtime',
        }],
      }],
    },
  },
]
