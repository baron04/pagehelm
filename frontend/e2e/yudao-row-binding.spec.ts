import {test, expect} from '@playwright/test'
import {readFileSync} from 'node:fs'
import {join} from 'node:path'

const modules = process.env.WOA_YUDAO_UI_MODULES
const facts = process.env.WOA_YUDAO_FACTS_MODULE ?? new URL('../src/integrations/yudao/page-facts.js', import.meta.url)
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
