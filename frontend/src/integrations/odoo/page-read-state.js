// Plain same-origin observer exports consumed by the generic browser Runtime.
export function readPageRoute() {
    return window.__woaViewBridge?.current()?.path;
}
export function readPageRead() {
    const value = window.__woaViewBridge?.current();
    if (!value || !Number.isSafeInteger(value.page)) return undefined;
    return {path: value.path, query_digest: value.query_digest, query_fields: value.query_fields || {},
        revision: value.revision, page: value.page, page_size: value.page_size,
        total: value.total, rows: value.rows};
}
export function readElementPageControl(element) {
    const state = window.__woaViewBridge?.current();
    if (!state || !Number.isSafeInteger(state.page) || element?.disabled) return undefined;
    const query = queryControl(element);
    if (query) return query;
    if (!element?.closest('.o_control_panel .o_pager') ||
        !window.__woaViewBridge.contains(element) || element.tagName !== 'BUTTON' ||
        element.type !== 'button') return undefined;
    if (element.matches('button.o_pager_next') && state.page * state.page_size < state.total) {
        return {kind: 'pagination', page: state.page + 1, pageSize: state.page_size};
    }
    if (element.matches('button.o_pager_previous') && state.page > 1) {
        return {kind: 'pagination', page: state.page - 1, pageSize: state.page_size};
    }
    return undefined;
}
function queryControl(element) {
    const current = window.__woaViewBridge?.search?.(), bar = current?.bar;
    if (!bar || !element?.isConnected || element.getClientRects().length === 0) return undefined;
    const own = bar.root.el.contains(element), model = bar.env.searchModel;
    if (own && element === bar.inputRef.el && element.matches('input.o_searchview_input') &&
        !element.readOnly) {
        const active = Object.keys(current.fields).filter(k => k.startsWith('search_'));
        const first = bar.searchItemsFields.find(i => ['char', 'text'].includes(i.fieldType));
        const field = active.length === 1 ? active[0] : first && `search_${first.fieldName}`;
        if (field) return {kind: 'query', field};
    }
    if (own && element.matches('button.o_searchview_dropdown_toggler'))
        return {kind: 'query', field: 'search_menu'};
    const option = own && element.closest('.o_searchview_autocomplete .o_menu_item');
    if (option && !element.closest('.o_expand')) {
        const choices = bar.items.filter(i => String(i.id) === option.id && !i.isParent &&
            !i.isChild && !i.isFieldProperty && !i.isAddCustomFilterButton && !i.loadMore);
        const choice = choices.length === 1 && choices[0], item = choice && model.searchItems[choice.searchItemId];
        if (item?.type === 'field' && ['char', 'text'].includes(item.fieldType) &&
            typeof choice.value === 'string' && choice.value === bar.state.query &&
            ['ilike', 'like', '='].includes(choice.operator))
            return {kind: 'query', field: `search_${item.fieldName}`};
    }
    // Dropdown overlays are outside the view element. Bind the sole visible stock
    // search menu to this mounted SearchBar's open dropdown, never to its text alone.
    const menus = [...document.querySelectorAll('.o_search_bar_menu')].filter(e => e.getClientRects().length);
    const filter = element.closest('.o_filter_menu .o_menu_item[role=menuitemcheckbox]');
    if (bar.searchBarDropdownState.isOpen && menus.length === 1 && filter && menus[0].contains(filter)) {
        const choices = model.getSearchItems(i => i.type === 'filter' && !i.options)
            .filter(i => i.description.trim() === filter.textContent.trim());
        if (choices.length === 1) return {kind: 'query', field: `filter_${choices[0].name}`};
    }
    return undefined;
}
