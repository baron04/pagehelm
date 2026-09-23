// Application-owned page facts. The generic Runtime imports these exports from the same
// origin as the pinned PageController; Odoo selectors never enter the product core.
export {PageController, getElementByIndex} from './page-controller.js';
export {readPageFacets, readFacetCandidates, applyPageFacet} from './page-facets.js';
