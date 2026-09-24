/**
 * Storybook is where a person reviews the panel, so it has to show the shipped panel.
 *
 * The palette, the type and the shadows are all declared on `:host`. A story rendered
 * straight into the document has no host, every one of them silently resolves to nothing,
 * and the reviewer is looking at a serif, borderless, transparent panel no user ever sees.
 */
import {existsSync, readFileSync, statSync} from 'node:fs'
import {createServer, type Server} from 'node:http'
import type {AddressInfo} from 'node:net'
import {extname, join, normalize, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {test as base, expect, type Page} from '@playwright/test'
import tokens from '../src/styles/design_tokens.json' with {type: 'json'}

const STATIC = resolve(fileURLToPath(new URL('.', import.meta.url)), '../storybook-static')
const TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
  '.png': 'image/png', '.ico': 'image/x-icon',
}

function serve(): Promise<Server> {
  const server = createServer((request, response) => {
    const path = normalize(decodeURIComponent(new URL(request.url ?? '/', 'http://x').pathname))
    const file = join(STATIC, path === '/' ? 'index.html' : path)
    if (!file.startsWith(STATIC) || !existsSync(file) || !statSync(file).isFile()) {
      response.writeHead(404)
      response.end()
      return
    }
    response.writeHead(200, {'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream'})
    response.end(readFileSync(file))
  })
  return new Promise(done => server.listen(0, '127.0.0.1', () => done(server)))
}

const test = base.extend<{story: (id: string, theme: 'light' | 'dark') => Promise<Page>}>({
  story: async ({browser}, use) => {
    const server = await serve()
    const {port} = server.address() as AddressInfo
    const context = await browser.newContext({viewport: {width: 1024, height: 720}})
    await use(async (id, theme) => {
      const page = await context.newPage()
      await page.goto(`http://127.0.0.1:${port}/iframe.html?id=${id}&viewMode=story` +
                      `&globals=theme:${theme}`, {waitUntil: 'load'})
      return page
    })
    await context.close()
    await new Promise(done => server.close(done))
  },
})

test.skip(!existsSync(join(STATIC, 'iframe.html')),
          'Storybook is not built; run npm run build-storybook')

async function expectShipped(page: Page, selector: string, theme: 'light' | 'dark',
                             {edged = true} = {}): Promise<void> {
  const node = page.locator(selector).first()
  await node.waitFor({timeout: 15_000})
  const facts = await node.evaluate(node => {
    const style = getComputedStyle(node)
    return {in_shadow_root: node.getRootNode() instanceof ShadowRoot,
            bg: style.getPropertyValue('--woa-bg').trim(),
            font: style.fontFamily, shadow: style.boxShadow}
  })
  const where = `${selector} in ${theme}: ${JSON.stringify(facts)}`
  expect(facts.in_shadow_root, where).toBe(true)
  expect(facts.bg, where).toBe(tokens.themes[theme].bg)
  expect(facts.font.startsWith('Inter'), where).toBe(true)
  if (edged) {
    // The window, the card and the field draw their edges with box-shadow tokens; with no
    // palette those resolve to nothing and the edge simply disappears.
    expect(facts.shadow, where).not.toBe('none')
  }
}

for (const theme of ['light', 'dark'] as const) {
  test(`a window story renders with the palette, type and edges (${theme})`, async ({story}) => {
    const page = await story('panel-window--first-open', theme)
    const launcher = page.locator('.woa-launcher')
    await launcher.waitFor({timeout: 15_000})
    await launcher.click()
    await expectShipped(page, '.woa-window', theme)
    await expectShipped(page, '.woa-field', theme)
  })
}

test('a confirmation story renders the card as shipped', async ({story}) => {
  const page = await story('panel-window--awaiting-confirmation', 'dark')
  await expectShipped(page, '.woa-approval', 'dark')
})

test('a single piece renders as shipped and stays pressable', async ({story}) => {
  const page = await story('panel-launcher--launcher-idle', 'light')
  // The launcher's disc carries its own gradient; the button itself draws no edge.
  await expectShipped(page, '.woa-launcher', 'light', {edged: false})
  expect(await page.locator('.woa-launcher').evaluate(
    node => getComputedStyle(node).pointerEvents)).toBe('auto')
})
