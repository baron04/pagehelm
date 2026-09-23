// Odoo-only interpretation of applied search facets. Candidate menu items and list
// column labels are deliberately ignored: they do not show an active page condition.
export function readPageFacets() {
  const facts = [];
  for (const facet of document.querySelectorAll('.o_searchview_facet')) {
    if (!facet.isConnected || facet.closest('[hidden], [aria-hidden="true"]')) continue;
    const label = facet.querySelector('.o_searchview_facet_label');
    const kind = label?.querySelector('.oi-group, .fa-sort-numeric-asc, .fa-sort-numeric-desc')
      ? 'group' : label?.classList.contains('btn-primary') &&
        facet.classList.contains('o_facet_with_domain') ? 'filter' : null;
    if (!kind) continue;
    for (const value of facet.querySelectorAll('.o_facet_value')) {
      const name = value.textContent?.trim();
      if (name) facts.push({kind, name});
      if (facts.length === 16) return facts;
    }
  }
  return facts;
}
