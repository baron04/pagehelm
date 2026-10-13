import {afterEach, describe, expect, it, vi} from 'vitest'
import {applyPageFacet, readFacetCandidates, readPageFacets}
  from '../../examples/odoo/deploy/addons/woa_runtime/static/src/page-facets.js'

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals() })

describe('Odoo-owned active facet extraction', () => {
  it('delegates candidate listing and application to the Odoo search-model bridge', () => {
    const apply = vi.fn()
    vi.stubGlobal('__woaFacetBridge', {
      list: () => [{ref: 'fc_7', kind: 'group', name: 'Salesperson', active: false}],
      apply,
    })
    expect(readFacetCandidates()).toEqual([
      {ref: 'fc_7', kind: 'group', name: 'Salesperson', active: false},
    ])
    applyPageFacet('fc_7', true)
    expect(apply).toHaveBeenCalledWith('fc_7', true)
  })
  it('separates active filters and groups from same-named menu choices and columns', () => {
    document.body.innerHTML = `
      <div class="o_searchview_facet o_facet_with_domain"><div class="o_searchview_facet_label btn-primary"><i class="fa-filter"></i></div><div class="o_facet_values"><small class="o_facet_value">My Quotations</small></div></div>
      <div class="o_searchview_facet"><div class="o_searchview_facet_label text-bg-action"><i class="oi-group"></i></div><div class="o_facet_values"><small class="o_facet_value">Customer</small></div></div>
      <button role="menuitemcheckbox" aria-checked="false">Customer</button>
      <th>Customer</th>`
    expect(readPageFacets()).toEqual([
      {kind: 'filter', name: 'My Quotations'}, {kind: 'group', name: 'Customer'},
    ])
  })

  it('reports known empty state when no active chips exist', () => {
    document.body.innerHTML = '<button role="menuitemcheckbox">Customer</button>'
    expect(readPageFacets()).toEqual([])
  })

  it('recognizes ordered groups and does not confuse comparisons with filters', () => {
    document.body.innerHTML = `
      <div class="o_searchview_facet"><div class="o_searchview_facet_label text-bg-action"><i class="fa-sort-numeric-asc"></i></div><div class="o_facet_values"><small class="o_facet_value">Customer</small><small class="o_facet_value">Country</small></div></div>
      <div class="o_searchview_facet o_facet_with_domain"><div class="o_searchview_facet_label text-bg-action"><i class="fa-adjust"></i></div><div class="o_facet_values"><small class="o_facet_value">Previous Period</small></div></div>`
    expect(readPageFacets()).toEqual([
      {kind: 'group', name: 'Customer'}, {kind: 'group', name: 'Country'},
    ])
  })
})
