import type {Plugin} from 'vite'

/**
 * ai-motion 0.4.8's aaStep can pass equal edges to smoothstep when fwidth is zero
 * on a corner diagonal. That undefined division produces opaque coloured speckles
 * on Chromium/Metal. Bound only the AA width; keep the upstream rendering intact.
 */
export function aiMotionAntialiasFix(): Plugin {
  return {
    name: 'ai-motion-antialias-fix',
    enforce: 'pre',
    transform(code, id) {
      if (!id.split('?')[0]!.replaceAll('\\', '/').endsWith('/ai-motion/build/Motion.js')) return
      const original = 'float width = fwidth(d);'
      if (!code.includes(original)) this.error('ai-motion AA guard no longer matches; review the upstream update.')
      return {code: code.replace(original, 'float width = max(fwidth(d), 0.0001);'), map: null}
    },
  }
}
