import {afterEach, expect, it} from 'vitest'
import {readPageRoute, readPageRead, readElementPageControl} from '../src/integrations/odoo/page-read-state.js'
afterEach(() => {delete window.__woaViewBridge;document.body.innerHTML=''})
it('needs current mounted application facts, never infers the route from the URL', () => {
    expect(readPageRoute()).toBeUndefined()
    window.__woaViewBridge={current:()=>null}
    expect(readPageRead()).toBeUndefined()
    window.__woaViewBridge={current:()=>({path:'/odoo/products',revision:2,
        query_digest:'a'.repeat(64),page:1,page_size:20,total:40,rows:20})}
    expect(readPageRead()).toMatchObject({path:'/odoo/products',page:1,revision:2,query_digest:'a'.repeat(64)})
})
it('only projects enabled standard pagers belonging to the current controller', () => {
    document.body.innerHTML='<div class="o_control_panel"><div class="o_pager"><button class="o_pager_next" type="button">Next</button></div></div><button>Next</button>'
    const buttons=document.querySelectorAll('button')
    window.__woaViewBridge={current:()=>({page:1,page_size:20,total:40}),contains:e=>e===buttons[0]}
    expect(readElementPageControl(buttons[0])).toEqual({kind:'pagination',page:2,pageSize:20})
    expect(readElementPageControl(buttons[1])).toBeUndefined()
    buttons[0].type='submit'
    expect(readElementPageControl(buttons[0])).toBeUndefined()
    buttons[0].type='button';buttons[0].disabled=true
    expect(readElementPageControl(buttons[0])).toBeUndefined()
})
