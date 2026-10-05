// Plain same-origin observer exports consumed by the generic browser Runtime.
export function readPageRoute() {
    return window.__woaViewBridge?.current()?.path;
}
export function readPageRead() {
    const value = window.__woaViewBridge?.current();
    if (!value || !Number.isSafeInteger(value.page)) return undefined;
    // Standard unfiltered paging is independent of raw record values. Query proofs are
    // absent until a separate applied-query observation is qualified.
    return {path: value.path, query_digest: value.query_digest, query_fields: {},
        revision: value.revision, page: value.page, page_size: value.page_size,
        total: value.total, rows: value.rows};
}
export function readElementPageControl(element) {
    const state = window.__woaViewBridge?.current();
    if (!state || !Number.isSafeInteger(state.page) || element?.disabled ||
        !element?.closest('.o_control_panel .o_pager') ||
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
