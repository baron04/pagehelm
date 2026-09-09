import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildBaseline } from './build.mjs'

const firstRoot = await mkdtemp(join(tmpdir(), 'woa-pageagent-first-'))
const secondRoot = await mkdtemp(join(tmpdir(), 'woa-pageagent-second-'))
const first = await buildBaseline(firstRoot)
const second = await buildBaseline(secondRoot)
if (JSON.stringify(first) !== JSON.stringify(second)) throw new Error('PageAgent baseline build is not deterministic')
const bundle = await readFile(join(firstRoot, 'pageagent-baseline.js'), 'utf8')
for (const marker of ['__WOA_PAGEAGENT_BASELINE__', '__WOA_PAGEAGENT_PROVIDER_FETCH__', '__WOA_PAGEAGENT_BUSINESS_TOOL__', '__WOA_PAGEAGENT_OPEN_ROUTE__', 'PageAgentCore']) {
  if (!bundle.includes(marker)) throw new Error(`PageAgent baseline bundle is missing ${marker}`)
}
for (const forbidden of ['AGENT_OPENAI_API_KEY', 'WOA_TWENTY_OAUTH', 'sourceMappingURL=', 'execute_javascript: tool']) {
  if (bundle.includes(forbidden)) throw new Error(`PageAgent baseline bundle contains forbidden marker ${forbidden}`)
}
process.stdout.write(`${JSON.stringify({ status: 'READY', ...first })}\n`)
