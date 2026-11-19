import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'astro/config'
import starlight from '@astrojs/starlight'

const here = path.dirname(fileURLToPath(import.meta.url))
const docsRoot = path.resolve(here, '../../docs')
const site = process.env.ASTRO_SITE || 'https://pagehelm.dev'
const rawBase = process.env.ASTRO_BASE || '/'
const base = rawBase === '/' ? '/' : `/${rawBase.replace(/^\/+|\/+$/g, '')}`

function rewriteRelativeDocsLinks() {
  const prefix = base === '/' ? '' : base
  return {
    name: 'pagehelm-docs-link-rewriter',
    hooks: {
      'astro:config:setup': ({ config }) => {
        const processor = config.markdown?.processor
        if (!processor?.options?.mdastPlugins) return
        processor.options.mdastPlugins.push(({ fileURL }) => {
          if (!fileURL) return
          const filePath = fileURLToPath(fileURL)
          const fileDir = path.dirname(filePath)
          return {
            name: 'rewrite-relative-docs-links',
            link(node) {
              const url = node?.url
              if (
                typeof url !== 'string' ||
                !url ||
                url.startsWith('#') ||
                /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(url) ||
                url.startsWith('/')
              ) {
                return
              }
              const hashIndex = url.indexOf('#')
              const hash = hashIndex >= 0 ? url.slice(hashIndex) : ''
              const cleanPath = hashIndex >= 0 ? url.slice(0, hashIndex) : url
              if (!cleanPath.endsWith('.md') && !cleanPath.endsWith('.mdx')) return
              const resolved = path.resolve(fileDir, cleanPath)
              const rel = path.relative(docsRoot, resolved).split(path.sep).join('/')
              if (rel.startsWith('..')) {
                return { type: 'emphasis', children: node.children }
              }
              let route = rel.replace(/\.(md|mdx)$/i, '')
              if (route === 'index') route = ''
              else if (route.endsWith('/index')) route = route.slice(0, -'/index'.length)
              const joined = route ? `${prefix}/${route}/` : `${prefix}/`
              return { ...node, url: `${joined.replace(/\/{2,}/g, '/')}${hash}` }
            },
          }
        })
      },
    },
  }
}

export default defineConfig({
  site,
  base,
  trailingSlash: 'always',
  image: {
    service: { entrypoint: 'astro/assets/services/noop' },
  },
  integrations: [
    starlight({
      title: 'PageHelm',
      description: 'Embedded AI operations helm for first-party web applications.',
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        'zh-cn': { label: '简体中文', lang: 'zh-CN' },
      },
      customCss: ['./src/styles/custom.css'],
      sidebar: [
        {
          label: 'Architecture',
          translations: { 'zh-cn': '简介与架构' },
          items: [
            { label: 'Overview', translations: { 'zh-cn': '系统总览' }, slug: 'index' },
            { label: 'Architecture Overview', translations: { 'zh-cn': '架构概览' }, slug: 'architecture/overview' },
          ],
        },
        {
          label: 'Quickstart & Integration',
          translations: { 'zh-cn': '快速上手与接入' },
          items: [
            { label: '5-Min Quickstart & Ops', translations: { 'zh-cn': '5 分钟快速上手与运维' }, slug: 'integration/quickstart' },
            { label: 'L0–L3 Progressive Integration', translations: { 'zh-cn': 'L0–L3 分级接入指南' }, slug: 'integration/levels' },
          ],
        },
        {
          label: 'Security & Verified Writes',
          translations: { 'zh-cn': '安全与核验写入' },
          items: [
            { label: 'Security & Verified Writes', translations: { 'zh-cn': '安全边界与核验写入' }, slug: 'security/verified-writes' },
          ],
        },
        {
          label: 'Reference',
          translations: { 'zh-cn': '协议与参考' },
          items: [
            { label: 'Protocol & Configuration', translations: { 'zh-cn': 'API 与协议配置参考' }, slug: 'reference/protocol-and-configuration' },
          ],
        },
      ],
    }),
    rewriteRelativeDocsLinks(),
  ],
})
