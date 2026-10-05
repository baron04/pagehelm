// Application-owned page facts. The generic Runtime imports these exports from the same
// origin as the pinned PageController; Odoo selectors never enter the product core.
import {PageController as BasePageController, getElementByIndex} from './page-controller.js';

// The pinned controller sets a native input value and emits `input`, but not `change`.
// Odoo's existing-customer form does not add that value to its pending record without
// `change`: the DOM appears filled, yet leaving the form silently saves the old value.
// Do not do this on create forms: Odoo's date widget reformats on `change` and would break
// the qualified quotation draft. Only the existing-customer edit draft uses this behavior.
export class PageController extends BasePageController {
    async inputText(index, text) {
        const outcome = await super.inputText(index, text);
        if (outcome?.success !== true || !/^\/odoo\/customers\/[1-9][0-9]*$/.test(location.pathname)) {
            return outcome;
        }
        const element = getElementByIndex(this.selectorMap, index);
        if (!element?.isConnected || !('value' in element) || element.value !== text) {
            return {success: false, message: 'Odoo input changed before it could be committed'};
        }
        element.dispatchEvent(new Event('change', {bubbles: true}));
        return outcome;
    }
}

export {getElementByIndex};
export {readPageFacets, readFacetCandidates, applyPageFacet} from './page-facets.js';
export {readPageRecordRoute} from './page-record-route.js';
export {readPageBlocker} from './page-blocker.js';
export {readActiveView} from './page-active-view.js';
export {readPageRoute, readPageRead, readElementPageControl} from './page-read-state.js';
