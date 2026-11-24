import {test, expect, type Page} from '@playwright/test'
import {readFileSync} from 'node:fs'
import {join} from 'node:path'

const modules = process.env.WOA_YUDAO_UI_MODULES
const generic = process.env.WOA_PAGE_CONTROLLER_MODULE

async function install(page: Page, times: number[][] | null = [[0, 0, 0], [23, 59, 59]], initial: string[] = [], type = 'daterange') {
  const requests: string[] = []
  const files = new Map<string, string | URL>([
    ['/vue.js', join(modules!, 'vue/dist/vue.global.prod.js')],
    ['/element.js', join(modules!, 'element-plus/dist/index.full.min.js')],
    ['/locale.js', join(modules!, 'element-plus/dist/locale/zh-cn.min.js')],
    ['/element.css', join(modules!, 'element-plus/dist/index.css')],
    ['/woa/page-facts.js', new URL('../dist/integrations/yudao/page-facts.js', import.meta.url)],
    ['/woa/yudao-page-controller.js', new URL('../dist/integrations/yudao/yudao-page-controller.js', import.meta.url)],
    ['/woa/form-surface.js', new URL('../dist/integrations/yudao/form-surface.js', import.meta.url)],
    ...(generic ? [['/woa/page-controller.js', generic] as [string, string]] : []),
  ])
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== 'https://synthetic.test') return route.abort()
    if (url.pathname === '/admin-api/inventory/items/page') {
      requests.push(url.search)
      return route.fulfill({json: {code: 0, data: {list: [], total: 0}}})
    }
    const file = files.get(url.pathname)
    if (file) return route.fulfill({body: readFileSync(file), contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript'})
    return route.fulfill({contentType: 'text/html', body: `<!doctype html><meta charset="utf-8">
      <link rel="stylesheet" href="/element.css"><script src="/vue.js"></script><script src="/element.js"></script><script src="/locale.js"></script>
      <div id="app"></div><script>
      const {h,reactive,ref,nextTick,getCurrentInstance,onMounted,KeepAlive,createApp} = Vue;
      const query=reactive({pageNo:1,pageSize:10,period:${JSON.stringify(initial)}}), callbacks=[], shown=ref(true), disabled=ref(false), blocked=ref(false);
      const times=${JSON.stringify(times)}?.map(parts=>new Date(2000,0,1,...parts));
      const active={path:'/inventory/list',fullPath:'/inventory/list',matched:[]};
      const search=()=>fetch('/admin-api/inventory/items/page?'+new URLSearchParams({start:query.period[0]||'',end:query.period[1]||''}));
      const Page={name:'SyntheticInventory',setup(){const instance=getCurrentInstance();
        onMounted(()=>{active.matched[0].instances.default={$:instance}});
        return ()=>h('main',[shown.value && h(ElementPlus.ElForm,{model:query},()=>[
          h(ElementPlus.ElFormItem,{label:'Period',prop:'period'},()=>h(ElementPlus.ElDatePicker,{
            type:${JSON.stringify(type)},modelValue:query.period,valueFormat:'YYYY-MM-DD HH:mm:ss',disabled:disabled.value,
            disabledDate:date=>blocked.value && date.getDate()===31,
            defaultValue:[new Date(2026,9,1),new Date(2026,10,1)],defaultTime:times,
            'onUpdate:modelValue':value=>{callbacks.push({kind:'update',value});query.period=value},
            onChange:value=>callbacks.push({kind:'change',value})})),
          h(ElementPlus.ElButton,{onClick:search},()=> 'Search')])])}};
      const RouterView={name:'RouterView',render(){return h(KeepAlive,null,{default:()=>h(Page,{key:active.fullPath})})}};
      const AppView={name:'AppView',render(){return h('section',[h(RouterView)])}};
      active.matched=[{path:active.path,components:{default:Page},instances:{}}];
      const app=createApp(AppView);app.config.globalProperties.$router={currentRoute:{value:active}};
      app.use(ElementPlus,{locale:ElementPlusLocaleZhCn});app.mount('#app');
      window.fixture={query,callbacks,shown,disabled,blocked,nextTick,active};
      </script>`})
  })
  await page.goto('https://synthetic.test/inventory/list')
  await expect(page.locator('.el-range-input')).toHaveCount(2)
  return requests
}

async function fillRange(page: Page, enabled: boolean, values = ['2026-10-01', '2026-10-31']) {
  return page.evaluate(`(async()=>{
    const api=await import('/woa/yudao-page-controller.js${enabled ? '?bindings=1' : ''}');
    const controller=new api.PageController({enableMask:false});
    const results=[];
    try {
      for(const [end,value] of ${JSON.stringify(values)}.entries()) {
        await controller.getBrowserState();
        const input=document.querySelectorAll('.el-range-input')[end];
        const index=[...controller.elementTextMap.keys()].find(i=>api.getElementByIndex(controller.selectorMap,i)===input);
        if(index===undefined)throw new Error('fresh range input is not indexed');
        results.push(await controller.inputText(index,value));await window.fixture.nextTick();
      }
      await controller.getBrowserState();
      const index=[...controller.elementTextMap.keys()].find(i=>api.getElementByIndex(controller.selectorMap,i)?.textContent.trim()==='Search');
      const search=await controller.clickElement(index);await window.fixture.nextTick();
      return {results,search,values:[...window.fixture.query.period],callbacks:[...window.fixture.callbacks],
        inputs:[...document.querySelectorAll('.el-range-input')].map(e=>e.value)};
    }finally{controller.dispose()}
  })()`)
}

test.beforeEach(() => test.skip(!modules || !generic, 'qualified Vue/Element Plus and pinned SDK modules are required'))

for (const enabled of [false, true]) {
  test(`date range uses actual ${enabled ? 'binding' : 'ordinary'} callbacks and preserves endpoint times`, async ({page}, info) => {
    const requests = await install(page)
    const result = await fillRange(page, enabled) as {
      results: {success: boolean; execution_trace: {trace_id: string; events: {operation: string; path: string}[]}}[]
      values: string[]; callbacks: {kind: string; value: string[]}[]
    }
    await info.attach('date-range-proof', {body: JSON.stringify({result, requests}), contentType: 'application/json'})
    expect(result.values).toEqual(['2026-10-01 00:00:00', '2026-10-31 23:59:59'])
    expect(result.callbacks).toContainEqual({kind: 'update', value: result.values})
    expect(result.callbacks).toContainEqual({kind: 'change', value: result.values})
    for (const item of result.results) {
      expect(item.success).toBe(true)
      expect(item.execution_trace.events.length).toBeGreaterThan(0)
      expect(item.execution_trace.events.every(e => e.operation === 'fill' && e.path === (enabled ? 'binding' : 'ordinary'))).toBe(true)
    }
    expect(result.results[0]!.execution_trace.trace_id).not.toBe(result.results[1]!.execution_trace.trace_id)
    await expect.poll(() => requests.some(q => new URLSearchParams(q).get('start') === result.values[0]
      && new URLSearchParams(q).get('end') === result.values[1])).toBe(true)
  })
}

test('date binding uses application endpoint defaults rather than hardcoded day limits', async ({page}, info) => {
  const requests = await install(page, [[8, 15, 30], [17, 45, 20]])
  const result = await fillRange(page, true) as {
    values: string[]; results: {execution_trace: {events: {path: string}[]}}[]
  }
  await info.attach('application-time-defaults', {body: JSON.stringify({result, requests}), contentType: 'application/json'})
  expect(result.values).toEqual(['2026-10-01 08:15:30', '2026-10-31 17:45:20'])
  expect(result.results.every(r => r.execution_trace.events.every(e => e.path === 'binding'))).toBe(true)
})

for (const values of [
  ['2026-10-01', '2026-10-31'],
  ['2026-10-01 08:15:30', '2026-10-31 17:45:20'],
]) {
  test(`query datetimerange without defaultTime preserves ${values[0]!.includes(' ') ? 'explicit times' : 'inclusive natural days'}`, async ({page}, info) => {
    const requests = await install(page, null, [], 'datetimerange')
    const result = await fillRange(page, true, values) as {
      values: string[]; callbacks: {kind: string; value: string[]}[]
      results: {success: boolean; execution_trace: {events: {path: string}[]}}[]
    }
    const expected = values[0]!.includes(' ') ? values : ['2026-10-01 00:00:00', '2026-10-31 23:59:59']
    await page.waitForTimeout(600)
    result.callbacks = await page.evaluate('JSON.parse(JSON.stringify(window.fixture.callbacks))')
    await info.attach('query-datetimerange-proof', {body: JSON.stringify({result, requests}), contentType: 'application/json'})
    expect(result.values).toEqual(expected)
    expect(result.callbacks).toContainEqual({kind: 'update', value: expected})
    expect(result.callbacks).toContainEqual({kind: 'change', value: expected})
    expect(result.results.every(r => r.success && r.execution_trace.events.length > 0
      && r.execution_trace.events.every(e => e.path === 'binding'))).toBe(true)
    await expect.poll(() => requests.some(q => new URLSearchParams(q).get('start') === expected[0]
      && new URLSearchParams(q).get('end') === expected[1])).toBe(true)
  })
}

for (const values of [
  ['2026-10-01', '2026-10-31'],
  ['2026-10-01 08:15:30', '2026-10-31 17:45:20'],
]) {
  test(`datetimerange ${values[0]!.includes(' ') ? 'explicit times' : 'natural days'} are retained when the application also has defaultTime`, async ({page}, info) => {
    const requests = await install(page, [[9, 0, 0], [18, 0, 0]], [], 'datetimerange')
    const result = await fillRange(page, true, values) as {values: string[]; callbacks: {kind: string; value: string[]}[]}
    const expected = values[0]!.includes(' ') ? values : ['2026-10-01 00:00:00', '2026-10-31 23:59:59']
    await page.waitForTimeout(600)
    result.callbacks = await page.evaluate('JSON.parse(JSON.stringify(window.fixture.callbacks))')
    await info.attach('query-time-with-defaults', {body: JSON.stringify({result, requests}), contentType: 'application/json'})
    expect(result.values).toEqual(expected)
    expect(result.callbacks.filter(c => c.kind === 'change').at(-1)).toEqual({kind: 'change', value: expected})
    await expect.poll(() => requests.some(q => new URLSearchParams(q).get('start') === expected[0]
      && new URLSearchParams(q).get('end') === expected[1])).toBe(true)
  })
}

test('invalid datetime queries refuse before any binding or ordinary input callback', async ({page}, info) => {
  await install(page, null, [], 'datetimerange')
  const result = await page.evaluate(`(async()=>{
    const api=await import('/woa/yudao-page-controller.js?bindings=1');
    const controller=new api.PageController({enableMask:false}), outcomes=[];
    try{
      for(const value of ['2026-02-30','2026-10-01 24:00:00','2026-10-01 12:60:00','2026-10-01 12:00:60','2026-10-01 08:00']){
        await controller.getBrowserState();const input=document.querySelector('.el-range-input');
        const index=[...controller.elementTextMap.keys()].find(i=>api.getElementByIndex(controller.selectorMap,i)===input);
        let error='';try{await controller.inputText(index,value)}catch(e){error=String(e)}
        outcomes.push({error,trace:controller.bindingTrace});
      }
      return {outcomes,values:window.fixture.query.period,callbacks:window.fixture.callbacks};
    }finally{controller.dispose()}
  })()`) as {outcomes: {error: string; trace: {events: unknown[]}}[]; values: string[]; callbacks: unknown[]}
  await info.attach('invalid-datetime-proof', {body: JSON.stringify(result), contentType: 'application/json'})
  expect(result.outcomes.every(o => o.error.includes('native date range') && o.trace.events.length === 0)).toBe(true)
  expect(result.values).toEqual([])
  expect(result.callbacks).toEqual([])
})

for (const type of ['daterange', 'datetimerange']) {
test(`${type} bindings reject invalid, readonly, disabled, business, inactive and detached inputs`, async ({page}) => {
  await install(page, type === 'datetimerange' ? null : undefined, [], type)
  const result = await page.evaluate(`(async()=>{
    const {boundInput}=await import('/woa/page-facts.js');const s=window.fixture;
    const input=document.querySelector('.el-range-input'), outcomes=[];let invoked=0;
    const attempt=async value=>{try{return await boundInput(input,value,()=>invoked++)}catch(e){return String(e)}};
    outcomes.push((await attempt('2026-02-30')).includes('refuses this date or time'));
    input.readOnly=true;outcomes.push(await attempt('2026-10-01'));input.readOnly=false;
    s.disabled.value=true;await s.nextTick();outcomes.push(await attempt('2026-10-01'));
    s.disabled.value=false;await s.nextTick();
    delete s.query.pageNo;outcomes.push(await attempt('2026-10-01'));s.query.pageNo=1;
    s.active.path='/other';outcomes.push(await attempt('2026-10-01'));s.active.path='/inventory/list';
    s.shown.value=false;await s.nextTick();outcomes.push(await attempt('2026-10-01'));
    return {outcomes,invoked,callbacks:s.callbacks,values:s.query.period};
  })()`)
  expect(result).toEqual({outcomes: [true, false, false, false, false, false], invoked: 0, callbacks: [], values: []})
})
}

test('datetime binding diagnostics cannot change the query or native update/change events', async ({page}, info) => {
  const results: unknown[] = []
  for (const broken of [false, true]) {
    await install(page, null, [], 'datetimerange')
    const result = await page.evaluate(`(async()=>{
      const {boundInput}=await import('/woa/page-facts.js');const s=window.fixture;
      const callback=()=>{if(${broken})throw new Error('synthetic diagnostic collector failure')};
      const outcomes=[];
      for(const [i,value] of ['2026-10-01','2026-10-31'].entries()){
        outcomes.push(await boundInput(document.querySelectorAll('.el-range-input')[i],value,callback));await s.nextTick();
      }
      return {outcomes,values:[...s.query.period],callbacks:[...s.callbacks]};
    })()`) as {outcomes: boolean[]; values: string[]; callbacks: unknown[]}
    await page.waitForTimeout(600)
    results.push({...result, callbacks: await page.evaluate('JSON.parse(JSON.stringify(window.fixture.callbacks))')})
  }
  await info.attach('datetime-diagnostic-independence', {body: JSON.stringify(results), contentType: 'application/json'})
  expect(results[1]).toEqual(results[0])
  expect(results[1]).toMatchObject({outcomes: [true, true], values: ['2026-10-01 00:00:00', '2026-10-31 23:59:59']})
})

test('editing a datetime range preserves the untouched endpoint time and real change events', async ({page}, info) => {
  const requests = await install(page, null, ['2026-12-01 09:30:00', '2026-12-31 18:45:00'], 'datetimerange')
  const values = ['2026-10-01 08:15:30', '2026-10-31 17:45:20']
  const result = await fillRange(page, true, values) as {values: string[]; callbacks: {kind: string; value: string[]}[]}
  await page.waitForTimeout(600)
  result.callbacks = await page.evaluate('JSON.parse(JSON.stringify(window.fixture.callbacks))')
  await info.attach('existing-datetime-range-proof', {body: JSON.stringify({result, requests}), contentType: 'application/json'})
  expect(result.values).toEqual(values)
  expect(result.callbacks.filter(c => c.kind === 'change')).toEqual([
    {kind: 'change', value: [values[0], '2026-12-31 18:45:00']}, {kind: 'change', value: values},
  ])
})

test('the component disabled-date contract prevents value updates without ordinary replay', async ({page}, info) => {
  await install(page)
  const result = await page.evaluate(`(async()=>{
    const api=await import('/woa/yudao-page-controller.js?bindings=1');const s=window.fixture;
    s.blocked.value=true;await s.nextTick();const controller=new api.PageController({enableMask:false});
    try {
      await controller.getBrowserState();const input=document.querySelector('.el-range-input');
      const index=[...controller.elementTextMap.keys()].find(i=>api.getElementByIndex(controller.selectorMap,i)===input);
      let error='';try{await controller.inputText(index,'2026-10-31')}catch(e){error=String(e)}
      return {error,trace:controller.bindingTrace,values:s.query.period,callbacks:s.callbacks};
    }finally{controller.dispose()}
  })()`) as {error: string; trace: {events: {path: string}[]}; values: string[]; callbacks: unknown[]}
  await info.attach('disabled-date-proof', {body: JSON.stringify(result), contentType: 'application/json'})
  expect(result.error).toContain('native date range refuses this date')
  expect(result.trace.events.map(e => e.path)).toEqual(['binding'])
  expect(result.values).toEqual([])
  expect(result.callbacks).toEqual([])
})

test('editing an existing range preserves the native change callbacks and endpoint defaults', async ({page}, info) => {
  const results: {enabled: boolean; result: {values: string[]; callbacks: {kind: string; value: string[]}[]}; requests: string[]}[] = []
  for (const enabled of [false, true]) {
    const requests = await install(page, undefined, ['2026-12-01 00:00:00', '2026-12-31 23:59:59'])
    const result = await fillRange(page, enabled) as {values: string[]; callbacks: {kind: string; value: string[]}[]}
    await page.waitForTimeout(600)
    result.callbacks = await page.evaluate(`JSON.parse(JSON.stringify(window.fixture.callbacks))`)
    results.push({enabled, result, requests})
  }
  await info.attach('existing-range-native-comparison', {body: JSON.stringify(results), contentType: 'application/json'})
  for (const entry of results) expect(entry.result.values).toEqual(['2026-10-01 00:00:00', '2026-10-31 23:59:59'])
  const changes = results.map(entry => entry.result.callbacks.filter(row => row.kind === 'change'))
  // Ordinary mode drives native Element Plus calendar selection without intermediate text blur,
  // emitting a single change event on completion; binding mode preserves the untouched endpoint mid-edit.
  expect(changes[0]).toEqual([{kind: 'change', value: ['2026-10-01 00:00:00', '2026-10-31 23:59:59']}])
  expect(changes[1]).toEqual([
    {kind: 'change', value: ['2026-10-01 00:00:00', '2026-12-31 23:59:59']},
    {kind: 'change', value: ['2026-10-01 00:00:00', '2026-10-31 23:59:59']},
  ])
  expect(changes[1]!.at(-1)).toEqual(changes[0]!.at(-1))
  for (const entry of results) {
    expect(entry.requests.some(q => new URLSearchParams(q).get('start') === '2026-10-01 00:00:00'
      && new URLSearchParams(q).get('end') === '2026-10-31 23:59:59')).toBe(true)
  }
})
