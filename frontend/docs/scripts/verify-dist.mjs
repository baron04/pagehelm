import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const siteRoot = path.resolve(here, '..')
const docsRoot = path.resolve(siteRoot, '../../docs')
const distDir = path.join(siteRoot, 'dist')

const baseArgIndex = process.argv.indexOf('--base')
const rawBase = baseArgIndex >= 0 ? process.argv[baseArgIndex + 1] : (process.env.ASTRO_BASE || '/')
const base = rawBase === '/' ? '/' : `/${rawBase.replace(/^\/+|\/+$/g, '')}`

const required = ['index.html', 'zh-cn/index.html', 'llms.txt', 'llms-full.txt']
for (const rel of required) {
  const target = path.join(distDir, rel)
  if (!fs.existsSync(target) || fs.statSync(target).size === 0) {
    throw new Error(`Missing or empty required build output: ${target}`)
  }
}

function walkFiles(dir, ext) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walkFiles(full, ext))
    else if (entry.name.endsWith(ext)) out.push(full)
  }
  return out
}

// Check 1:1 EN/ZH Markdown parity and basic editorial hygiene in docs/
const allMd = walkFiles(docsRoot, '.md').map((p) => path.relative(docsRoot, p).split(path.sep).join('/'))
const enMd = allMd.filter((rel) => !rel.startsWith('zh-cn/')).sort()
const zhMd = allMd
  .filter((rel) => rel.startsWith('zh-cn/'))
  .map((rel) => rel.slice('zh-cn/'.length))
  .sort()
if (JSON.stringify(enMd) !== JSON.stringify(zhMd)) {
  throw new Error(`EN/ZH docs file mismatch.\nEN: ${enMd.join(', ')}\nZH: ${zhMd.join(', ')}`)
}
for (const rel of allMd) {
  const text = fs.readFileSync(path.join(docsRoot, rel), 'utf8')
  if (/\bTL;DR\b/i.test(text)) {
    throw new Error(`Disallowed boilerplate ("TL;DR") found in docs/${rel}`)
  }
}

const forbiddenUserPrefix = '/' + 'Us' + 'ers/'
const forbiddenFileScheme = 'fi' + 'le://'
const htmlFiles = walkFiles(distDir, '.html')
const broken = []

for (const file of [...htmlFiles, path.join(distDir, 'llms.txt'), path.join(distDir, 'llms-full.txt')]) {
  const text = fs.readFileSync(file, 'utf8')
  if (text.includes(forbiddenUserPrefix) || text.includes(forbiddenFileScheme)) {
    throw new Error(`Forbidden local path or file URI leaked into ${file}`)
  }
}

function targetExists(distRelativePath) {
  const clean = distRelativePath.replace(/^\/+|\/+$/g, '')
  if (!clean) return fs.existsSync(path.join(distDir, 'index.html'))
  const direct = path.join(distDir, clean)
  if (fs.existsSync(direct) && fs.statSync(direct).isFile()) return true
  if (fs.existsSync(`${direct}.html`)) return true
  if (fs.existsSync(path.join(direct, 'index.html'))) return true
  return false
}

const hrefPattern = /\bhref="([^"]+)"/g
const topNavPattern = /<nav[^>]*class="[^"]*ph-topnav[^"]*"[^>]*>([\s\S]*?)<\/nav>/i
for (const htmlFile of htmlFiles) {
  const content = fs.readFileSync(htmlFile, 'utf8')
  const topNavMatch = content.match(topNavPattern)
  if (topNavMatch && /llms\.txt/i.test(topNavMatch[1])) {
    throw new Error(`Primary navigation must not contain llms.txt (${path.relative(distDir, htmlFile)})`)
  }
  const pageDir = path.dirname(path.relative(distDir, htmlFile))
  for (const match of content.matchAll(hrefPattern)) {
    const rawHref = match[1]
    if (!rawHref || rawHref.startsWith('#') || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(rawHref) || rawHref.startsWith('//')) {
      continue
    }
    const clean = rawHref.split('#')[0].split('?')[0]
    if (!clean) continue
    let distRelative = ''
    if (clean.startsWith('/')) {
      if (base !== '/') {
        if (!clean.startsWith(base)) {
          broken.push(`${path.relative(distDir, htmlFile)} -> ${rawHref} (missing base ${base})`)
          continue
        }
        distRelative = clean.slice(base.length) || '/'
      } else {
        distRelative = clean
      }
    } else {
      distRelative = path.posix.normalize(path.posix.join(pageDir === '.' ? '' : pageDir, clean))
    }
    if (!targetExists(distRelative)) {
      broken.push(`${path.relative(distDir, htmlFile)} -> ${rawHref} (resolved: ${distRelative})`)
    }
  }
}

const llmsPath = path.join(distDir, 'llms.txt')
const llmsContent = fs.readFileSync(llmsPath, 'utf8')
const mdLinkPattern = /\[[^\]]+\]\(([^)]+)\)/g
let llmsLinksChecked = 0
for (const match of llmsContent.matchAll(mdLinkPattern)) {
  const rawHref = match[1].trim()
  if (!rawHref || rawHref.startsWith('#') || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(rawHref) || rawHref.startsWith('//')) {
    continue
  }
  const clean = rawHref.split('#')[0].split('?')[0]
  if (!clean) continue
  llmsLinksChecked += 1
  if (!clean.startsWith('/')) {
    broken.push(`llms.txt -> ${rawHref} (expected root-relative link)`)
    continue
  }
  let distRelative = clean
  if (base !== '/') {
    if (clean !== base && !clean.startsWith(`${base}/`)) {
      broken.push(`llms.txt -> ${rawHref} (missing base ${base})`)
      continue
    }
    distRelative = clean.slice(base.length) || '/'
  }
  if (!targetExists(distRelative)) {
    broken.push(`llms.txt -> ${rawHref} (resolved: ${distRelative})`)
  }
}
if (llmsLinksChecked < 20) {
  throw new Error(`Expected >= 20 internal links in llms.txt, found ${llmsLinksChecked}`)
}

if (broken.length > 0) {
  throw new Error(`Broken documentation links found for base=${base}:\n${broken.join('\n')}`)
}
console.log(`Verified ${htmlFiles.length} HTML files, ${enMd.length} EN/ZH doc pairs, llms.txt (${llmsLinksChecked} links), and llms-full.txt for base=${base} (0 broken links, 0 leaks).`)
