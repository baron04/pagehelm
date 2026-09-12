/**
 * Dev entry: mount the panel over a page that stands in for a host application.
 *
 * The panel is mounted exactly the way the Runtime mounts it -- same `mountPanel`, same
 * shadow root, same adopted stylesheet -- so what you see here is what a host page gets.
 */
import {mountPanel} from '../src/mount'
import {createDevHost} from './host'

const host = createDevHost()
mountPanel({host, allowedOrigins: [window.location.origin]})

for (const button of document.querySelectorAll<HTMLButtonElement>('[data-scenario]')) {
  button.addEventListener('click', () => {
    const name = button.dataset.scenario
    if (name) host.scenario(name as Parameters<typeof host.scenario>[0])
  })
}

const theme = document.querySelector<HTMLButtonElement>('[data-theme-toggle]')
theme?.addEventListener('click', () => {
  const root = document.documentElement
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark'
  root.dataset.theme = next
  theme.textContent = next === 'dark' ? '☾ 暗色' : '☀ 亮色'
})
