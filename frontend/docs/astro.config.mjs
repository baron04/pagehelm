import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'astro/config'
import starlight from '@astrojs/starlight'

const here = path.dirname(fileURLToPath(import.meta.url))
const docsRoot = path.resolve(here, '../../docs')
const site = process.env.ASTRO_SITE || 'https://pagehelm.dev'
const rawBase = process.env.ASTRO_BASE || '/'
const base = rawBase === '/' ? '/' : `/${rawBase.replace(/^\/+|\/+$/g, '')}`
const prefix = base === '/' ? '' : base

function rewriteRelativeDocsLinks() {
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
      description: 'Embedded server-governed AI operations framework for first-party web applications.',
      logo: {
        src: './src/assets/logo.svg',
        alt: 'PageHelm',
      },
      head: [
        {
          tag: 'link',
          attrs: {
            rel: 'alternate',
            type: 'text/plain',
            href: `${prefix}/llms.txt`,
            title: 'LLM Documentation Index',
          },
        },
      ],
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        'zh-cn': { label: '简体中文', lang: 'zh-CN' },
      },
      components: {
        Header: './src/components/Header.astro',
        Hero: './src/components/Hero.astro',
        Footer: './src/components/Footer.astro',
      },
      customCss: ['./src/styles/custom.css'],
      sidebar: [
        {
          label: 'Getting Started & Integration',
          translations: { 'zh-cn': '起步与接入' },
          items: [
            { label: 'Local Demo Quickstart', translations: { 'zh-cn': '本地示例上手' }, slug: 'integration/quickstart' },
            { label: 'Application Onboarding', translations: { 'zh-cn': '应用声明式接入' }, slug: 'integration/onboarding' },
            { label: 'Skills & Typed Writes', translations: { 'zh-cn': 'Skill 与类型化写入' }, slug: 'integration/skills-and-writes' },
          ],
        },
        {
          label: 'Architecture',
          translations: { 'zh-cn': '架构与执行' },
          items: [
            { label: 'Architecture Overview', translations: { 'zh-cn': '架构总览' }, slug: 'architecture/overview' },
            { label: 'Execution & Browser Runtime', translations: { 'zh-cn': '执行引擎与浏览器运行时' }, slug: 'architecture/execution-and-runtime' },
          ],
        },
        {
          label: 'Security & Operations',
          translations: { 'zh-cn': '安全与运维' },
          items: [
            { label: 'Write Safety & Privacy', translations: { 'zh-cn': '写入安全与隐私边界' }, slug: 'security/verified-writes' },
            { label: 'Deployment & Operations', translations: { 'zh-cn': '部署、运维与排障' }, slug: 'security/operations-and-observability' },
          ],
        },
        {
          label: 'Reference',
          translations: { 'zh-cn': '参考手册' },
          items: [
            { label: 'Configuration & Protocol', translations: { 'zh-cn': '配置与通信协议' }, slug: 'reference/protocol-and-configuration' },
            { label: 'Tools, Capabilities & Checks', translations: { 'zh-cn': '内置工具、能力与自检' }, slug: 'reference/tools-and-capabilities' },
          ],
        },
      ],
    }),
    rewriteRelativeDocsLinks(),
  ],
})
