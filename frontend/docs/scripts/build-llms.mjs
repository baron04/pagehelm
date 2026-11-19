import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const siteRoot = path.resolve(here, '..')
const docsRoot = path.resolve(siteRoot, '../../docs')
const publicDir = path.resolve(siteRoot, 'public')

const SECTIONS = [
  { rel: 'index.md', route: '/', lang: 'en' },
  { rel: 'architecture/overview.md', route: '/architecture/overview/', lang: 'en' },
  { rel: 'architecture/execution-and-runtime.md', route: '/architecture/execution-and-runtime/', lang: 'en' },
  { rel: 'integration/quickstart.md', route: '/integration/quickstart/', lang: 'en' },
  { rel: 'integration/levels.md', route: '/integration/levels/', lang: 'en' },
  { rel: 'integration/skills-and-writes.md', route: '/integration/skills-and-writes/', lang: 'en' },
  { rel: 'security/verified-writes.md', route: '/security/verified-writes/', lang: 'en' },
  { rel: 'security/operations-and-observability.md', route: '/security/operations-and-observability/', lang: 'en' },
  { rel: 'reference/protocol-and-configuration.md', route: '/reference/protocol-and-configuration/', lang: 'en' },
  { rel: 'reference/tools-and-capabilities.md', route: '/reference/tools-and-capabilities/', lang: 'en' },
  { rel: 'zh-cn/index.md', route: '/zh-cn/', lang: 'zh-cn' },
  { rel: 'zh-cn/architecture/overview.md', route: '/zh-cn/architecture/overview/', lang: 'zh-cn' },
  { rel: 'zh-cn/architecture/execution-and-runtime.md', route: '/zh-cn/architecture/execution-and-runtime/', lang: 'zh-cn' },
  { rel: 'zh-cn/integration/quickstart.md', route: '/zh-cn/integration/quickstart/', lang: 'zh-cn' },
  { rel: 'zh-cn/integration/levels.md', route: '/zh-cn/integration/levels/', lang: 'zh-cn' },
  { rel: 'zh-cn/integration/skills-and-writes.md', route: '/zh-cn/integration/skills-and-writes/', lang: 'zh-cn' },
  { rel: 'zh-cn/security/verified-writes.md', route: '/zh-cn/security/verified-writes/', lang: 'zh-cn' },
  { rel: 'zh-cn/security/operations-and-observability.md', route: '/zh-cn/security/operations-and-observability/', lang: 'zh-cn' },
  { rel: 'zh-cn/reference/protocol-and-configuration.md', route: '/zh-cn/reference/protocol-and-configuration/', lang: 'zh-cn' },
  { rel: 'zh-cn/reference/tools-and-capabilities.md', route: '/zh-cn/reference/tools-and-capabilities/', lang: 'zh-cn' },
]

function parseFrontmatter(raw) {
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/)
  if (!match) return { meta: {}, body: raw.trim() }
  const meta = {}
  for (const line of match[1].split(/\r?\n/)) {
    const kv = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/)
    if (kv) meta[kv[1]] = kv[2].replace(/^['"]|['"]$/g, '').trim()
  }
  return { meta, body: match[2].trim() }
}

fs.mkdirSync(publicDir, { recursive: true })

const docs = SECTIONS.map((entry) => {
  const fullPath = path.join(docsRoot, entry.rel)
  if (!fs.existsSync(fullPath)) {
    throw new Error(`Missing docs source file: ${fullPath}`)
  }
  const { meta, body } = parseFrontmatter(fs.readFileSync(fullPath, 'utf8'))
  return { ...entry, title: meta.title || entry.rel, description: meta.description || '', body }
})

const indexLines = [
  '# PageHelm',
  '',
  '> Embedded AI operations helm (`web_operation_agent`) for first-party web applications: server-owned planning, hybrid same-actor API + semantic DOM execution, and fresh visible write approvals.',
  '',
  '## Key Invariants',
  '',
  '- **Default read-only:** Mutations default off (`read-only`); `assisted-draft` prepares handoff without dispatching; `verified-operate` enforces the 8-stage `WritePipeline`.',
  '- **Fresh single-use approvals:** Visible diff cards require `event.isTrusted`, visibility dwell, pre-dispatch re-read, same-actor readback, and never auto-retry `INDETERMINATE` writes.',
  '- **Neutral core:** Model keys, Skill manuals, and policies remain on the server; the Runtime never holds credentials or business plans.',
  '',
  '## English Documentation',
  '',
  ...docs.filter((d) => d.lang === 'en').map((d) => `- [${d.title}](${d.route}): ${d.description}`),
  '',
  '## 简体中文文档',
  '',
  ...docs.filter((d) => d.lang === 'zh-cn').map((d) => `- [${d.title}](${d.route}): ${d.description}`),
  '',
  '## Full Corpus',
  '',
  '- [Full bilingual documentation text](/llms-full.txt)',
  '',
]

const fullChunks = [
  '# PageHelm Full Bilingual Documentation Corpus',
  '',
  ...docs.flatMap((d) => [
    `================================================================================`,
    `SOURCE: docs/${d.rel} (${d.route})`,
    `TITLE: ${d.title}`,
    `================================================================================`,
    '',
    d.body,
    '',
  ]),
]

fs.writeFileSync(path.join(publicDir, 'llms.txt'), indexLines.join('\n'), 'utf8')
fs.writeFileSync(path.join(publicDir, 'llms-full.txt'), fullChunks.join('\n'), 'utf8')
console.log(`Generated public/llms.txt and public/llms-full.txt from ${docs.length} docs pages.`)
