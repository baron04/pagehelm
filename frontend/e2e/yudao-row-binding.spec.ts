import {test, expect} from '@playwright/test'
import {readFileSync} from 'node:fs'
import {execFileSync} from 'node:child_process'
import {join} from 'node:path'

const modules = process.env.WOA_YUDAO_UI_MODULES
const facts = process.env.WOA_YUDAO_FACTS_MODULE ?? new URL('../dist/integrations/yudao/page-facts.js', import.meta.url)
interface SortReceipt {
  vue: string; element: string; props_order: number[]; rendered_order: number[]
  binding: {id: string; values: string[][]}; invoked: boolean; opened: number[]
}

test('an internally sorted Element Plus table binds the rendered row and native callback', async ({page}, info) => {
  test.skip(!modules, 'WOA_YUDAO_UI_MODULES must name the qualified application dependencies')
  const files = new Map<string, string | URL>([
    ['/vue.js', join(modules!, 'vue/dist/vue.global.prod.js')],
    ['/element.js', join(modules!, 'element-plus/dist/index.full.min.js')],
    ['/element.css', join(modules!, 'element-plus/dist/index.css')],
    ['/woa/page-facts.js', facts],
  ])
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== 'http://synthetic.test') return route.abort()
    const file = files.get(url.pathname)
    if (file) return route.fulfill({body:readFileSync(file),contentType:url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript'})
    return route.fulfill({contentType:'text/html',body:`<!doctype html>
      <link rel="stylesheet" href="/element.css"><script src="/vue.js"></script><script src="/element.js"></script>
      <div id="app"></div><script>
      const {h,reactive,ref,nextTick,getCurrentInstance,onMounted,KeepAlive,createApp} = Vue;
      const rows = reactive([{id:7,name:'Alpha'},{id:8,name:'Beta'}]), clicked = [], table = ref();
      const active = {path:'/system/role',fullPath:'/system/role',matched:[]};
      const Page = {name:'SyntheticRoles',setup() {
        const instance = getCurrentInstance();
        onMounted(() => {active.matched[0].instances.default = {$:instance}});
        return () => h(ElementPlus.ElTable,{data:rows,ref:table}, {default:() => [
          h(ElementPlus.ElTableColumn,{label:'Name',prop:'name',sortable:true}),
          h(ElementPlus.ElTableColumn,{label:'Action'}, {default:scope =>
            h(ElementPlus.ElButton,{onClick:() => clicked.push(scope.row.id)},()=> '详情')})]});
      }};
      const RouterView = {name:'RouterView',render() {return h(KeepAlive,null,{default:()=>h(Page,{key:active.fullPath})})}};
      const AppView = {name:'AppView',render() {return h('section',[h(RouterView)])}};
      active.matched = [{path:active.path,components:{default:Page},instances:{}}];
      const app = createApp(AppView);app.config.globalProperties.$router = {currentRoute:{value:active}};
      app.mount('#app');window.fixture = {rows,clicked,table,nextTick};
      </script>`})
  })
  await page.goto('http://synthetic.test/system/role')
  await expect(page.locator('.el-table__body-wrapper tbody tr')).toHaveCount(2)
  const result = await page.evaluate<SortReceipt>(`(async () => {
    const {boundRecord,boundClick} = await import('/woa/page-facts.js');
    const s = window.fixture;
    s.table.value.sort('name','descending');
    await s.nextTick();
    await new Promise(requestAnimationFrame);
    const button = document.querySelector('.el-table__body-wrapper tbody tr button');
    const before = boundRecord(button);
    const invoked = await boundClick(button);
    return {vue:Vue.version,element:ElementPlus.version,props_order:s.rows.map(row=>row.id),
      rendered_order:s.table.value.store.states.data.value.map(row=>row.id),
      binding:before,invoked,opened:s.clicked};
  })()`)
  await info.attach('internal-sort-receipt', {body:JSON.stringify(result,null,2),contentType:'application/json'})
  expect(result.props_order).toEqual([7,8])
  expect(result.rendered_order).toEqual([8,7])
  expect(result.binding.id).toBe('8')
  expect(result.binding.values).toContainEqual(['Name','Beta'])
  expect(result.invoked).toBe(true)
  expect(result.opened).toEqual([8])
})

test('whole-form restoration uses ChannelServer structure and rolls back failed native validation', async ({page}, info) => {
  test.skip(!modules, 'WOA_YUDAO_UI_MODULES must name the qualified application dependencies')
  const files = new Map<string, string | URL>([
    ['/vue.js', join(modules!, 'vue/dist/vue.global.prod.js')],
    ['/element.js', join(modules!, 'element-plus/dist/index.full.min.js')],
    ['/element.css', join(modules!, 'element-plus/dist/index.css')],
    ['/woa/page-facts.js', facts],
  ])
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== 'http://synthetic.test') return route.abort()
    const file = files.get(url.pathname)
    if (file) return route.fulfill({body:readFileSync(file), contentType:url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript'})
    return route.fulfill({contentType:'text/html', body:`<!doctype html>
      <link rel="stylesheet" href="/element.css"><script src="/vue.js"></script><script src="/element.js"></script>
      <div id="app"></div><script>
      const {h,reactive,ref,nextTick,getCurrentInstance,onMounted,KeepAlive,createApp} = Vue;
      const active = {path:'/synthetic/entries',fullPath:'/synthetic/entries',matched:[]};
      const model = reactive({z:{two:2,one:1},id:42,accountId:8,lines:[2,1]});
      const query = reactive({pageNo:1,pageSize:10,reference:'SYN-17'});
      const mode = ref('query'), behavior = ref('valid');
      const rules = {accountId:[{validator:(_rule,_value,done)=>{
        if(behavior.value==='reject'){model.z.one=99;done(new Error('Synthetic invalid account'))}
        else {if(behavior.value==='drift')model.accountId=12;done()}
      }}]};
      const Page = {name:'SyntheticEntries',setup(){
        const instance=getCurrentInstance();onMounted(()=>{active.matched[0].instances.default={$:instance}});
        return ()=>h('div',[
          mode.value==='query' ? h(ElementPlus.ElForm,{model:query},{default:()=>[
            h(ElementPlus.ElFormItem,{prop:'reference',label:'Reference'},{default:()=>h('input',{value:query.reference})})]})
          : h(ElementPlus.ElForm,{model,rules},{default:()=>[
            h(ElementPlus.ElFormItem,{prop:'accountId',label:'Account'},{default:()=>h('input',{value:model.accountId})})]}),
          h('aside',Array.from({length:24000},(_,i)=>h('span',{key:i},'Option '+i)))
        ]);
      }};
      const RouterView={name:'RouterView',render(){return h(KeepAlive,null,{default:()=>h(Page,{key:active.fullPath})})}};
      const AppView={name:'AppView',render(){return h('section',[h(RouterView)])}};
      active.matched=[{path:active.path,components:{default:Page},instances:{}}];
      const app=createApp(AppView);app.config.globalProperties.$router={currentRoute:{value:active}};
      app.mount('#app');window.fixture={model,mode,behavior,nextTick};
      </script>`})
  })
  await page.goto('http://synthetic.test/synthetic/entries')
  await expect(page.locator('aside span')).toHaveCount(24000)
  const query = await page.evaluate(`(async()=>{
    const {readElementPageControl}=await import('/woa/page-facts.js');
    return readElementPageControl(document.querySelector('form input'));
  })()`)
  expect(query).toEqual({kind:'query',field:'reference'})
  await page.evaluate(`(async()=>{window.fixture.mode.value='editor';await window.fixture.nextTick()})()`)
  const model = await page.evaluate<Record<string, unknown>>(`(async()=>{
    const {readElementFormModel}=await import('/woa/page-facts.js');
    return readElementFormModel(document.querySelector('form input')).model;
  })()`)
  const channelRequest = (expected: unknown, values: unknown) => JSON.parse(execFileSync(
    join(process.cwd(), '../.venv/bin/python'), ['-m', 'web_operation_agent.tests.transport.submit_wire'],
    {cwd:join(process.cwd(), '..'), input:JSON.stringify({kind:'restore',ref:'e1',expected,values}),
      env:{...process.env,PYTHONPATH:'src:examples/yudao/src'}, encoding:'utf8'}))
  const restore = (request: unknown) => page.evaluate(async request => {
    const modulePath = '/woa/page-facts.js'
    const {restoreElementFormModel} = await import(/* @vite-ignore */ modulePath)
    return restoreElementFormModel(document.querySelector('form input'), request)
  }, request)
  expect(await restore(channelRequest(model, {accountId:3}))).toBe(true)
  const baseline = {...model, accountId:3}
  for (const expected of [{...baseline,accountId:'3'}, {...baseline,lines:[1,2]}, {...baseline,extra:null}]) {
    expect(await restore(channelRequest(expected,{accountId:7}))).toBe(false)
  }
  for (const behavior of ['reject','drift']) {
    await page.evaluate(behavior => { (window as unknown as {fixture:{behavior:{value:string}}}).fixture.behavior.value=behavior }, behavior)
    expect(await restore(channelRequest(baseline,{accountId:7}))).toBe(false)
    expect(await page.evaluate('JSON.parse(JSON.stringify(window.fixture.model))')).toEqual(baseline)
  }
  await info.attach('whole-form-restoration', {body:JSON.stringify({query, baseline, rejected:['scalar type','array order','extra field','invalid','drift']}),contentType:'application/json'})
})
