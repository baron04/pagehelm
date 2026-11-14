import {test, expect, type Page} from '@playwright/test'
import {readFileSync} from 'node:fs'
import {join} from 'node:path'

const modules = process.env.WOA_YUDAO_UI_MODULES
const generic = process.env.WOA_PAGE_CONTROLLER_MODULE
const facts = process.env.WOA_YUDAO_FACTS_MODULE ?? new URL('../src/integrations/yudao/page-facts.js', import.meta.url)

async function install(page: Page) {
  const requests: string[] = []
  const files = new Map<string, string | URL>([
    ['/vue.js', join(modules!, 'vue/dist/vue.global.prod.js')],
    ['/element.js', join(modules!, 'element-plus/dist/index.full.min.js')],
    ['/element.css', join(modules!, 'element-plus/dist/index.css')],
    ['/woa/page-facts.js', facts],
    ['/woa/yudao-page-controller.js', new URL('../src/integrations/yudao/yudao-page-controller.js', import.meta.url)],
    ...(generic ? [['/woa/page-controller.js', generic] as [string, string]] : []),
  ])
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== 'http://synthetic.test') return route.abort()
    if (url.pathname === '/admin-api/inventory/items/page') {
      requests.push(url.search)
      return route.fulfill({json:{code:0,data:{list:[],total:240}}})
    }
    const file = files.get(url.pathname)
    if (file) return route.fulfill({body:readFileSync(file),contentType:url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript'})
    return route.fulfill({contentType:'text/html',body:`<!doctype html><meta charset="utf-8">
      <link rel="stylesheet" href="/element.css"><script src="/vue.js"></script><script src="/element.js"></script>
      <div id="app"></div><script>
      const {h,ref,reactive,nextTick,getCurrentInstance,onMounted,KeepAlive,createApp} = Vue;
      const pageNo=ref(1), pageSize=ref(10), shown=ref(true), callbacks=[], business=[];
      const businessShown=ref(true);
      const query=reactive({pageNo:1,pageSize:10,name:'',status:null}), searched=[], choices=[];
      const active={path:'/inventory/list',fullPath:'/inventory/list',matched:[]};
      const read=()=>fetch('/admin-api/inventory/items/page?'+new URLSearchParams({pageNo:pageNo.value,pageSize:pageSize.value,status:query.status??''}));
      const Page={name:'SyntheticInventory',setup() {
        const instance=getCurrentInstance();onMounted(()=>{active.matched[0].instances.default={$:instance}});
        return ()=>h('main',[
          h(ElementPlus.ElForm,{model:query},()=>[
            h(ElementPlus.ElFormItem,{label:'Name',prop:'name'},()=>h(ElementPlus.ElInput,
              {modelValue:query.name,'onUpdate:modelValue':value=>{query.name=value}})),
            h(ElementPlus.ElFormItem,{label:'Status',prop:'status'},()=>h(ElementPlus.ElSelect,
              {modelValue:query.status,'onUpdate:modelValue':value=>{query.status=value},onChange:value=>choices.push(value)},
              ()=>[h(ElementPlus.ElOption,{label:'Available',value:1}),h(ElementPlus.ElOption,{label:'Paused',value:2})])),
            h(ElementPlus.ElButton,{onClick:()=>{searched.push(query.name);read()}},()=> 'Search')]),
          shown.value && h(ElementPlus.ElPagination,{currentPage:pageNo.value,pageSize:pageSize.value,
            pageSizes:[10,20,40],total:240,layout:'sizes,prev,pager,next',
            'onUpdate:pageSize':value=>{pageSize.value=value},
            'onUpdate:currentPage':value=>{pageNo.value=value},
            onSizeChange:value=>{callbacks.push({kind:'size',value});read()},
            onCurrentChange:value=>{callbacks.push({kind:'page',value});read()}}),
          businessShown.value && h(ElementPlus.ElForm,{model:{quantity:10}},()=>h(ElementPlus.ElFormItem,{label:'Business quantity'},()=>
            h(ElementPlus.ElSelect,{modelValue:10,'onUpdate:modelValue':value=>business.push(value)},()=>
              [10,20].map(value=>h(ElementPlus.ElOption,{label:String(value),value})))))]);
      }};
      const RouterView={name:'RouterView',render(){return h(KeepAlive,null,{default:()=>h(Page,{key:active.fullPath})})}};
      const AppView={name:'AppView',render(){return h('section',[h(RouterView)])}};
      active.matched=[{path:active.path,components:{default:Page},instances:{}}];
      const app=createApp(AppView);app.config.globalProperties.$router={currentRoute:{value:active}};
      app.use(ElementPlus,{locale:{name:'zh-cn',el:{pagination:{pagesize:'条/页',
        goto:'前往',pageClassifier:'页',total:'共 {total} 条',prev:'上一页',next:'下一页',
        currentPage:'第 {pager} 页',prevPages:'上 {pager} 页',nextPages:'下 {pager} 页'}}}});
      app.mount('#app');window.fixture={pageNo,pageSize,shown,businessShown,callbacks,business,query,searched,choices,nextTick};
      </script>`})
  })
  await page.goto('http://synthetic.test/inventory/list')
  await expect(page.locator('.el-pagination input[role=combobox]')).toBeVisible()
  return requests
}

test.beforeEach(() => test.skip(!modules, 'WOA_YUDAO_UI_MODULES must name the qualified application dependencies'))

test('native pagination opens its owned options, selects 20 and requests page 2 with size 20', async ({page}, info) => {
  const requests = await install(page)
  const opened = await page.evaluate<{bound: boolean; expanded: string; owner: string; control: string}>(`(async()=>{
    const {boundClick,readElementPageControl}=await import('/woa/page-facts.js');
    const input=document.querySelector('.el-pagination input[role=combobox]');
    const bound=await boundClick(input);await window.fixture.nextTick();
    return {bound,expanded:input.getAttribute('aria-expanded'),owner:input.getAttribute('aria-controls'),
      control:readElementPageControl(input)?.kind};
  })()`)
  await info.attach('pagination-open', {body:JSON.stringify(opened),contentType:'application/json'})
  expect(opened.control).toBe('page_size')
  expect(opened.bound).toBe(true)
  expect(opened.expanded).toBe('true')
  const options = page.locator(`[id="${opened.owner}"] [role=option]`)
  await expect(options.filter({hasText:/^20\s*条\/页$/})).toBeVisible()
  const result = await page.evaluate<{selected: boolean; paged: boolean; size: number; page: number; callbacks: unknown[]}>(`(async()=>{
    const {boundClick}=await import('/woa/page-facts.js');const s=window.fixture;
    const input=document.querySelector('.el-pagination input[role=combobox]');
    const list=document.getElementById(input.getAttribute('aria-controls'));
    const option=[...list.querySelectorAll('[role=option]')].find(e=>e.textContent.replace(/\\s/g,'')==='20条/页');
    const selected=await boundClick(option);await s.nextTick();await new Promise(requestAnimationFrame);
    const second=[...document.querySelectorAll('.el-pagination li.number')].find(e=>e.textContent.trim()==='2');
    const paged=await boundClick(second);await s.nextTick();
    return {selected,paged,size:s.pageSize.value,page:s.pageNo.value,callbacks:s.callbacks};
  })()`)
  await info.attach('pagination-receipt', {body:JSON.stringify({opened,result,requests}),contentType:'application/json'})
  expect(result.selected).toBe(true)
  expect(result.paged).toBe(true)
  expect(result.size).toBe(20)
  expect(result.page).toBe(2)
  expect(result.callbacks).toContainEqual({kind:'size',value:20})
  expect(result.callbacks).toContainEqual({kind:'page',value:2})
  await expect.poll(() => requests.some(query => new URLSearchParams(query).get('pageNo') === '2'
    && new URLSearchParams(query).get('pageSize') === '20')).toBe(true)
})

test('business selects and detached paging options do not receive native bindings', async ({page}) => {
  await install(page)
  const result = await page.evaluate<{business: boolean; detached: boolean; writes: unknown[]; callbacks: unknown[]}>(`(async()=>{
    const {boundClick}=await import('/woa/page-facts.js');const s=window.fixture;
    const business=await boundClick(document.querySelector('.el-form input[role=combobox]'));
    const input=document.querySelector('.el-pagination input[role=combobox]');
    await boundClick(input);await s.nextTick();
    const list=document.getElementById(input.getAttribute('aria-controls'));
    const option=list?.querySelector('[role=option]');s.shown.value=false;await s.nextTick();
    const detached=option ? await boundClick(option) : false;
    return {business,detached,writes:s.business,callbacks:s.callbacks};
  })()`)
  expect(result).toEqual({business:false,detached:false,writes:[],callbacks:[]})
})

test('query combobox and its exact owned option have independent binding click receipts', async ({page}, info) => {
  test.skip(!generic, 'WOA_PAGE_CONTROLLER_MODULE must name the pinned generic controller')
  const requests = await install(page)
  const result = await page.evaluate(`(async()=>{
    const api=await import('/woa/yudao-page-controller.js?bindings=1');const s=window.fixture;
    s.businessShown.value=false;await s.nextTick();const controller=new api.PageController({enableMask:false});
    const find=element=>[...controller.elementTextMap.keys()].find(index=>api.getElementByIndex(controller.selectorMap,index)===element);
    try {
      await controller.getBrowserState();
      const input=document.querySelector('.el-form .el-select input[role=combobox]');
      const opened=await controller.clickElement(find(input));await s.nextTick();
      // Match the procedure's bounded post-open settlement before a fresh choice.
      await new Promise(resolve=>setTimeout(resolve,300));
      await controller.getBrowserState();
      const list=document.getElementById(input.getAttribute('aria-controls'));
      const options=[...list.querySelectorAll('[role=option]')].filter(e=>e.textContent.trim()==='Available');
      if(options.length!==1)throw new Error('no unique owned option');
      const selected=await controller.clickElement(find(options[0]));await s.nextTick();
      await controller.getBrowserState();
      const button=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='Search');
      await controller.clickElement(find(button));await s.nextTick();
      return {opened,selected,status:s.query.status,choices:s.choices};
    }finally{controller.dispose()}
  })()`) as {opened: {execution_trace: {trace_id: string; events: {path: string}[]}};
    selected: {execution_trace: {trace_id: string; events: {path: string}[]}}; status: number; choices: number[]}
  await info.attach('query-select-clicks', {body: JSON.stringify({result, requests}), contentType: 'application/json'})
  expect(result.status).toBe(1)
  expect(result.choices).toEqual([1])
  for (const receipt of [result.opened, result.selected]) {
    expect(receipt.execution_trace.events.map(e => e.path)).toEqual(['binding'])
  }
  expect(result.opened.execution_trace.trace_id).not.toBe(result.selected.execution_trace.trace_id)
  await expect.poll(() => requests.some(q => new URLSearchParams(q).get('status') === '1')).toBe(true)
})

for (const enabled of [false,true]) {
  test(`controller traces actual ${enabled ? 'bound' : 'ordinary'} query branches and preserves trace identity on reads`, async ({page}, info) => {
    test.skip(!generic, 'WOA_PAGE_CONTROLLER_MODULE must name the pinned generic controller')
    await install(page)
    const result = await page.evaluate<{
      empty: {trace_id: null; events: unknown[]}; fill: {trace_id: string; events: {sequence: number; operation: string; path: string}[]}
      click: {trace_id: string; events: {sequence: number; operation: string; path: string}[]}; repeated: unknown
      name: string; searched: string[]
    }>(`(async()=>{
      const api=await import('/woa/yudao-page-controller.js${enabled ? '?bindings=1' : ''}');
      window.fixture.businessShown.value=false;await window.fixture.nextTick();
      const controller=new api.PageController({enableMask:false});
      const marker=async()=>{
        const state=await controller.getBrowserState();
        if (state.content.includes('组件动作回执: ')) throw new Error('diagnostic entered page content');
        return structuredClone(controller.bindingTrace);
      };
      const find=selector=>[...controller.elementTextMap.keys()].find(index=>
        api.getElementByIndex(controller.selectorMap,index)?.matches(selector));
      try {
        const empty=await marker();
        const filled=await controller.inputText(find('.el-form-item input:not([role=combobox])'),'Acme');
        const fill=filled.execution_trace;const repeated=await marker();
        const button=[...controller.elementTextMap.keys()].find(index=>
          api.getElementByIndex(controller.selectorMap,index)?.textContent.trim()==='Search');
        const clicked=await controller.clickElement(button);const click=clicked.execution_trace;await marker();
        return {empty,fill,click,repeated,name:window.fixture.query.name,searched:window.fixture.searched};
      } finally {controller.dispose()}
    })()`)
    await info.attach('controller-trace', {body:JSON.stringify(result),contentType:'application/json'})
    expect(result.empty).toEqual({trace_id:null,events:[]})
    expect(result.fill.trace_id).toMatch(/^[a-f0-9]{32}$/)
    expect(result.click.trace_id).not.toBe(result.fill.trace_id)
    expect(result.repeated).toEqual(result.fill)
    expect(result.fill.events.length).toBeGreaterThan(0)
    expect(result.click.events.length).toBeGreaterThan(0)
    for (const [trace, operation] of [[result.fill,'fill'],[result.click,'click']] as const) {
      expect(trace.events.map(e=>e.sequence)).toEqual(trace.events.map((_,i)=>i+1))
      expect(trace.events.every(e=>e.operation===operation && e.path===(enabled ? 'binding' : 'ordinary'))).toBe(true)
    }
    expect(result.name).toBe('Acme')
    expect(result.searched).toEqual(['Acme'])
  })
}
