"""The evaluator's own reading of the final page: what the browser shows, not what was said."""

FINAL_STATE_PROBE = """() => {
  const text = e => (e.innerText || '').replace(/\\s+/g, ' ').trim();
  const crumbs = [...document.querySelectorAll('.o_breadcrumb .breadcrumb-item, .o_breadcrumb .active')]
    .map(text).filter(Boolean);
  const tabs = [...document.querySelectorAll('.o_notebook .nav-link.active')].map(text);
  const params = new URLSearchParams(location.search);
  let view = params.get('view_type') || '';
  const controller = document.querySelector('.o_action_manager .o_view_controller');
  if (controller) {
    const m = controller.className.match(/\\bo_(list|kanban|form|pivot|graph|calendar|activity|map)_view\\b/);
    if (m) view = m[1];
  }
  const facets = [...document.querySelectorAll('.o_searchview_facet')].map(text);
  const dialog = [...document.querySelectorAll('.modal-body')].map(text).join(' | ');
  return {path: location.pathname, query: location.search,
          breadcrumb: crumbs.join(' / '), breadcrumb_last: crumbs.length ? crumbs[crumbs.length - 1] : '',
          active_tabs: tabs, view_type: view, facets: facets, dialog: dialog};
}"""
