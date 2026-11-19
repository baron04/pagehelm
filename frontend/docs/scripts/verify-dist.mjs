import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const siteRoot = path.resolve(here, '..')
const distDir = path.join(siteRoot, 'dist')

const baseArgIndex = process.argv.indexOf('--base')
const rawBase = baseArgIndex >= 0 ? process.argv[baseArgIndex + 1] : '/'
const base = rawBase === '/' ? '/' : `/${rawBase.replace(/^\/+|\/+$/g, '')}`

const required = ['index.html', 'zh-cn/index.html', 'llms.txt', 'llms-full.txt']
for (const rel of required) {
  const target = path.join(distDir, rel)
  if (!fs.existsSync(target) || fs.statSync(target).size === 0) {
    throw new Error(`Missing or empty required build output: ${target}`)
  }
}

function walkHtml(dir) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walkHtml(full))
    else if (entry.name.endsWith('.html')) out.push(full)
  }
  return out
}

const forbiddenUserPrefix = '/' + 'Us' + 'ers/'
const forbiddenFileScheme = 'fi' + 'le://'
const htmlFiles = walkHtml(distDir)
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
for (const htmlFile of htmlFiles) {
  const content = fs.readFileSync(htmlFile, 'utf8')
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

if (broken.length > 0) {
  throw new Error(`Broken documentation links found for base=${base}:\n${broken.join('\n')}`)
}
console.log(`Verified ${htmlFiles.length} HTML files, llms.txt, and llms-full.txt for base=${base} (0 broken links, 0 leaks).`)
