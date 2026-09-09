import { PageAgentCore, tool } from '@page-agent/core'
import { PageController } from '@page-agent/page-controller'
import { z } from 'zod/v4'

const VERSION = '1.12.4'
const MAX_PROVIDER_REQUEST_BYTES = 2 * 1024 * 1024
const MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
const MAX_PAGE_CONTENT_BYTES = 512 * 1024
const MAX_TOOL_RESULT_BYTES = 256 * 1024
const NAME = /^[a-z][a-z0-9_]{0,63}$/
const BUILTIN_TOOLS = new Set([
  'ask_user',
  'click_element_by_index',
  'done',
  'execute_javascript',
  'input_text',
  'open_allowed_route',
  'scroll',
  'scroll_horizontally',
  'select_dropdown_option',
  'wait',
])

function exactObject(value, fields, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`)
  const keys = Object.keys(value).sort()
  if (keys.length !== fields.length || keys.some((key, index) => key !== [...fields].sort()[index])) throw new Error(`${name} has unexpected fields`)
  return value
}

function boundedString(value, name, maximum) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) throw new Error(`${name} must be a bounded string`)
  return value
}

function boundedInteger(value, name, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error(`${name} is outside its allowed range`)
  return value
}

function primitiveSchema(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`)
  const allowed = new Set(['type', 'description', 'enum', 'minLength', 'maxLength', 'minimum', 'maximum'])
  if (Object.keys(value).some((key) => !allowed.has(key))) throw new Error(`${name} uses an unsupported JSON Schema keyword`)
  let schema
  if (value.type === 'string') {
    schema = z.string()
    if (value.minLength !== undefined) schema = schema.min(boundedInteger(value.minLength, `${name}.minLength`, 0, 10000))
    if (value.maxLength !== undefined) schema = schema.max(boundedInteger(value.maxLength, `${name}.maxLength`, 1, 10000))
  } else if (value.type === 'integer') {
    schema = z.number().int()
    if (value.minimum !== undefined) schema = schema.min(boundedInteger(value.minimum, `${name}.minimum`, -1000000, 1000000))
    if (value.maximum !== undefined) schema = schema.max(boundedInteger(value.maximum, `${name}.maximum`, -1000000, 1000000))
  } else if (value.type === 'number') {
    schema = z.number()
  } else if (value.type === 'boolean') {
    schema = z.boolean()
  } else {
    throw new Error(`${name}.type is unsupported`)
  }
  if (value.enum !== undefined) {
    if (!Array.isArray(value.enum) || !value.enum.length || value.enum.some((item) => typeof item !== value.type && !(value.type === 'integer' && typeof item === 'number'))) throw new Error(`${name}.enum is invalid`)
    schema = schema.refine((candidate) => value.enum.includes(candidate), { message: 'value is outside the declared enum' })
  }
  if (value.description !== undefined) schema = schema.describe(boundedString(value.description, `${name}.description`, 500))
  return schema
}

function compileInputSchema(value, name) {
  exactObject(value, ['type', 'properties', 'required', 'additionalProperties'], name)
  if (value.type !== 'object' || value.additionalProperties !== false || !value.properties || typeof value.properties !== 'object' || Array.isArray(value.properties)) throw new Error(`${name} must be a closed object schema`)
  const propertyNames = Object.keys(value.properties).sort()
  const required = value.required
  if (!Array.isArray(required) || required.some((item) => typeof item !== 'string') || required.length !== new Set(required).size || required.some((item) => !propertyNames.includes(item))) throw new Error(`${name}.required is invalid`)
  const requiredSet = new Set(required)
  const shape = {}
  for (const propertyName of propertyNames) {
    if (!NAME.test(propertyName)) throw new Error(`${name} property name is invalid`)
    const schema = primitiveSchema(value.properties[propertyName], `${name}.properties.${propertyName}`)
    shape[propertyName] = requiredSet.has(propertyName) ? schema : schema.optional()
  }
  return z.strictObject(shape)
}

function safeRelativeRoute(value) {
  const route = boundedString(value, 'route', 1000)
  if (!route.startsWith('/') || route.startsWith('//') || /[\\\u0000-\u0020\u007f]/u.test(route)) throw new Error('route must be a safe relative path')
  const parsed = new URL(route, location.href)
  if (parsed.origin !== location.origin || parsed.hash || `${parsed.pathname}${parsed.search}` !== route || parsed.pathname.split('/').includes('..') || parsed.pathname.includes('%')) throw new Error('route must be a safe same-origin path')
  return route
}

function compileTools(specifications, capabilityProfile, runToken, navigationMode, metrics, maxPageActions) {
  if (!Array.isArray(specifications) || specifications.length > 32) throw new Error('tools must be a bounded array')
  if (capabilityProfile === 'browser_only' && specifications.length) throw new Error('browser-only baseline cannot receive custom tools')
  if (capabilityProfile === 'skill_api' && !specifications.length) throw new Error('Skill/API baseline requires custom tools')
  const result = { ask_user: null, execute_javascript: null }
  for (const specification of specifications) {
    exactObject(specification, ['name', 'description', 'input_schema'], 'tool specification')
    const name = boundedString(specification.name, 'tool.name', 64)
    if (!NAME.test(name) || BUILTIN_TOOLS.has(name) || Object.hasOwn(result, name)) throw new Error('tool name is invalid or collides with PageAgent')
    const inputSchema = compileInputSchema(specification.input_schema, `tool.${name}.input_schema`)
    result[name] = tool({
      description: boundedString(specification.description, `tool.${name}.description`, 1000),
      inputSchema,
      execute: async (args, { signal }) => {
        signal.throwIfAborted()
        const invoke = globalThis.__WOA_PAGEAGENT_BUSINESS_TOOL__
        if (typeof invoke !== 'function') throw new Error('business-tool bridge is unavailable')
        const value = await invoke({ run_token: runToken, name, args })
        signal.throwIfAborted()
        const rendered = typeof value === 'string' ? value : JSON.stringify(value)
        if (typeof rendered !== 'string' || new TextEncoder().encode(rendered).length > MAX_TOOL_RESULT_BYTES) throw new Error('business-tool result is invalid or oversized')
        return rendered
      },
    })
  }
  if (capabilityProfile === 'skill_api') {
    result.open_allowed_route = tool({
      description: 'Open only an exact ui_path returned by a business tool in this run. Never invent or alter a route.',
      inputSchema: z.strictObject({ route: z.string().min(1).max(1000) }),
      execute: async ({ route }, { signal }) => {
        signal.throwIfAborted()
        if (metrics.page_actions >= maxPageActions) throw new Error('page-action budget is exhausted')
        const selected = safeRelativeRoute(route)
        const invoke = globalThis.__WOA_PAGEAGENT_OPEN_ROUTE__
        if (typeof invoke !== 'function') throw new Error('route bridge is unavailable')
        const permit = await invoke({ run_token: runToken, route: selected })
        exactObject(permit, ['allowed', 'route'], 'route permit')
        if (permit.allowed !== true || permit.route !== selected) throw new Error('route was not authorized')
        signal.throwIfAborted()
        if (navigationMode !== 'spa') throw new Error('route bridge currently requires SPA navigation')
        history.pushState({}, '', selected)
        window.dispatchEvent(new PopStateEvent('popstate'))
        return JSON.stringify({ opened: true, route: selected })
      },
    })
  }
  return result
}

function providerFetch(runToken, metrics) {
  return async (input, init = {}) => {
    const invoke = globalThis.__WOA_PAGEAGENT_PROVIDER_FETCH__
    if (typeof invoke !== 'function') throw new Error('provider bridge is unavailable')
    const url = typeof input === 'string' ? input : input?.url
    const method = String(init.method || 'POST').toUpperCase()
    const body = typeof init.body === 'string' ? init.body : ''
    if (url !== 'https://provider.invalid/v1/chat/completions' || method !== 'POST') throw new Error('PageAgent attempted an unexpected provider request')
    if (!body || new TextEncoder().encode(body).length > MAX_PROVIDER_REQUEST_BYTES) throw new Error('provider request body is empty or oversized')
    JSON.parse(body)
    metrics.provider_calls += 1
    const response = await invoke({ run_token: runToken, url, method, body })
    exactObject(response, ['status', 'body'], 'provider response')
    const status = boundedInteger(response.status, 'provider response status', 100, 599)
    if (typeof response.body !== 'string' || new TextEncoder().encode(response.body).length > MAX_PROVIDER_RESPONSE_BYTES) throw new Error('provider response body is invalid or oversized')
    JSON.parse(response.body)
    return new Response(response.body, { status, headers: { 'content-type': 'application/json' } })
  }
}

async function execute(config) {
  exactObject(config, ['prompt', 'model', 'language', 'max_steps', 'max_page_actions', 'wall_ms', 'navigation_mode', 'capability_profile', 'instructions', 'tools', 'run_token', 'request_options'], 'baseline config')
  const prompt = boundedString(config.prompt, 'prompt', 20000)
  const model = boundedString(config.model, 'model', 120)
  const language = config.language
  if (!['en-US', 'zh-CN'].includes(language)) throw new Error('language is unsupported')
  const maxSteps = boundedInteger(config.max_steps, 'max_steps', 1, 100)
  const maxPageActions = boundedInteger(config.max_page_actions, 'max_page_actions', 0, 200)
  const wallMs = boundedInteger(config.wall_ms, 'wall_ms', 1000, 3600000)
  const navigationMode = config.navigation_mode
  if (!['document', 'spa'].includes(navigationMode)) throw new Error('navigation_mode is unsupported')
  const capabilityProfile = config.capability_profile
  if (!['browser_only', 'skill_api'].includes(capabilityProfile)) throw new Error('capability_profile is unsupported')
  const instructions = config.instructions === '' ? '' : boundedString(config.instructions, 'instructions', 100000)
  const runToken = boundedString(config.run_token, 'run_token', 128)
  const requestOptions = exactObject(config.request_options, ['temperature', 'top_p', 'max_completion_tokens', 'parallel_tool_calls'], 'request_options')
  if (typeof requestOptions.temperature !== 'number' || !Number.isFinite(requestOptions.temperature) || requestOptions.temperature < 0 || requestOptions.temperature > 2) throw new Error('temperature is invalid')
  if (typeof requestOptions.top_p !== 'number' || !Number.isFinite(requestOptions.top_p) || requestOptions.top_p < 0 || requestOptions.top_p > 1) throw new Error('top_p is invalid')
  boundedInteger(requestOptions.max_completion_tokens, 'max_completion_tokens', 1, 131072)
  if (requestOptions.parallel_tool_calls !== false) throw new Error('parallel tool calls must be disabled')
  const metrics = { provider_calls: 0, page_actions: 0, business_calls: 0, retries: 0, steps: 0 }
  const customTools = compileTools(config.tools, capabilityProfile, runToken, navigationMode, metrics, maxPageActions)
  const businessNames = new Set(config.tools.map((item) => item.name))
  const pageController = new PageController({ enableMask: false })
  const agent = new PageAgentCore({
    pageController,
    baseURL: 'https://provider.invalid/v1',
    model,
    language,
    maxSteps,
    maxRetries: 0,
    stepDelay: 0,
    customTools,
    instructions: instructions ? { system: instructions } : undefined,
    experimentalScriptExecutionTool: false,
    experimentalLlmsTxt: false,
    customFetch: providerFetch(runToken, metrics),
    transformRequestBody: (requestBody) => ({ ...requestBody, ...requestOptions }),
    transformPageContent: (content) => {
      if (typeof content !== 'string' || new TextEncoder().encode(content).length > MAX_PAGE_CONTENT_BYTES) throw new Error('PageAgent page observation is invalid or oversized')
      return content
    },
    onAfterStep: () => { metrics.steps += 1 },
  })
  agent.addEventListener('activity', (event) => {
    const activity = event.detail
    if (activity?.type === 'retrying') metrics.retries += 1
    if (activity?.type === 'executed') {
      if (businessNames.has(activity.tool)) metrics.business_calls += 1
      else if (!['done', 'wait'].includes(activity.tool)) metrics.page_actions += 1
    }
  })
  const timer = setTimeout(() => { void agent.stop() }, wallMs)
  try {
    const result = await agent.execute(prompt)
    const pageBudgetExceeded = metrics.page_actions > maxPageActions
    return {
      bridge_version: 2,
      upstream_version: VERSION,
      status: pageBudgetExceeded ? 'stopped' : agent.status,
      success_claim: result.success === true && !pageBudgetExceeded,
      terminal_reason: pageBudgetExceeded ? 'page_action_budget' : null,
      answer: typeof result.data === 'string' ? result.data.slice(0, 20000) : '',
      metrics,
    }
  } finally {
    clearTimeout(timer)
    agent.dispose()
  }
}

if (Object.hasOwn(globalThis, '__WOA_PAGEAGENT_BASELINE__')) throw new Error('PageAgent baseline bridge is already installed')
Object.defineProperty(globalThis, '__WOA_PAGEAGENT_BASELINE__', {
  value: Object.freeze({ bridge_version: 2, upstream_version: VERSION, execute }),
  configurable: false,
  enumerable: false,
  writable: false,
})
