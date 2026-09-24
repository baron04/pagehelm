import {defineConfig, devices} from '@playwright/test'

/**
 * The panel's Chromium checks: the built panel (`npm run build`) over a scripted host, in
 * `e2e/`. They are the gate for what the panel does in a real browser -- its states, the
 * confirmation card's keyboard and narrow layouts, and that it constructs under a strict CSP.
 * Storybook is for looking; its build is checked too when present (`npm run build-storybook`).
 *
 * `@playwright/test` is pinned apart from the Python `playwright` pin on purpose: 1.52 hangs
 * while loading a test file under Node 24 in an ESM package such as this one, before any
 * test runs. Each side installs the Chromium its own version names.
 */
export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  forbidOnly: true,
  reporter: [['list']],
  use: {
    ...devices['Desktop Chrome'],
    headless: true,
    // Headless Chromium hides scrollbars by default, which is no one's real browser: on
    // Windows and Linux a scrollbar takes room from the layout, and what that room does to
    // the panel is exactly what a test has to be able to see.
    launchOptions: {ignoreDefaultArgs: ['--hide-scrollbars']},
  },
})
