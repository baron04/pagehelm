import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineCollection } from 'astro:content'
import { glob } from 'astro/loaders'
import { docsSchema } from '@astrojs/starlight/schema'

const docsBase = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../docs')

export const collections = {
  docs: defineCollection({
    loader: glob({ base: docsBase, pattern: '**/[^_]*.{md,mdx}' }),
    schema: docsSchema(),
  }),
}
