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

function searchFixture() {
    document.body.innerHTML='<div id="search"><input class="o_searchview_input"><button class="o_searchview_dropdown_toggler" aria-expanded="false"></button><ul class="o_searchview_autocomplete"><li class="o_menu_item" id="7"><a>Search Name for Blue</a></li><li class="o_menu_item" id="8"><a>Add Custom Filter</a></li></ul></div><div class="o_search_bar_menu"><div class="o_filter_menu"><span class="o_menu_item" role="menuitemcheckbox">Active</span></div></div><button id="outside">Active</button>'
    for(const e of document.querySelectorAll('*'))e.getClientRects=()=>[{}]
    const root=document.querySelector('#search'),input=root.querySelector('input')
    const field={id:1,type:'field',fieldName:'name',fieldType:'char'}
    const filter={type:'filter',name:'active',description:'Active'}
    const bar={root:{el:root},inputRef:{el:input},state:{query:'Blue'},searchBarDropdownState:{isOpen:true},
        searchItemsFields:[field],items:[{id:7,searchItemId:1,value:'Blue',operator:'ilike'},
        {id:8,isAddCustomFilterButton:true}],env:{searchModel:{searchItems:{1:field},
        getSearchItems:predicate=>[field,filter].filter(predicate)}}}
    window.__woaViewBridge={current:()=>({page:1,page_size:20,total:40}),
        search:()=>({bar,fields:{}}),contains:e=>root.contains(e)}
    return {bar,input}
}

it('binds actual text search choices and rejects custom or unrelated controls', () => {
    const {bar,input}=searchFixture()
    expect(readElementPageControl(input)).toEqual({kind:'query',field:'search_name'})
    expect(readElementPageControl(document.querySelector('[id="7"] a'))).toEqual({kind:'query',field:'search_name'})
    expect(readElementPageControl(document.querySelector('[id="8"] a'))).toBeUndefined()
    expect(readElementPageControl(document.querySelector('#outside'))).toBeUndefined()
    bar.state.query='Red'
    expect(readElementPageControl(document.querySelector('[id="7"] a'))).toBeUndefined()
})

it('binds only the sole open stock filter menu to its mounted SearchBar', () => {
    const {bar}=searchFixture(),filter=document.querySelector('[role=menuitemcheckbox]')
    expect(readElementPageControl(filter)).toEqual({kind:'query',field:'filter_active'})
    bar.searchBarDropdownState.isOpen=false
    expect(readElementPageControl(filter)).toBeUndefined()
    bar.searchBarDropdownState.isOpen=true
    const other=document.querySelector('.o_search_bar_menu').cloneNode(true)
    other.getClientRects=()=>[{}]
    document.body.append(other)
    expect(readElementPageControl(filter)).toBeUndefined()
})
