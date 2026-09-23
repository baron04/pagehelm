/** @odoo-module **/

// Application-owned access to the current search model. The generic assistant Runtime
// never imports Odoo modules or invents search-item IDs; it sees only bounded choices.
import { ControlPanel } from "@web/search/control_panel/control_panel";
import { patch } from "@web/core/utils/patch";
import { onMounted, onWillUnmount } from "@odoo/owl";

const mounted = new Set();

patch(ControlPanel.prototype, {
    setup() {
        super.setup(...arguments);
        onMounted(() => mounted.add(this));
        onWillUnmount(() => mounted.delete(this));
    },
});

function currentModel() {
    const visible = [...mounted].filter(
        (panel) => panel.root?.el?.isConnected && panel.env?.searchModel
    );
    return visible.length === 1 ? visible[0].env.searchModel : null;
}

function choices(model) {
    return model.getSearchItems((item) => item.type === "filter" || item.type === "groupBy")
        .filter((item) => Number.isSafeInteger(item.id) && item.id >= 0 &&
            typeof item.description === "string" && item.description.trim())
        .map((item) => ({
            ref: `fc_${item.id}`,
            kind: item.type === "groupBy" ? "group" : "filter",
            name: item.description.trim(),
            active: Boolean(item.isActive),
            id: item.id,
        }));
}

function boundedChoices(model) {
    const all = choices(model);
    // Search views may define dozens of filters before the first group. Reserve room
    // for both kinds; otherwise a valid "group by" target is invisible to the Agent.
    const selected = [
        ...all.filter((item) => item.kind === "group").slice(0, 16),
        ...all.filter((item) => item.kind === "filter").slice(0, 16),
    ];
    for (const item of all) {
        if (selected.length === 32) break;
        if (!selected.some((included) => included.ref === item.ref)) selected.push(item);
    }
    return selected;
}

window.__woaFacetBridge = {
    list() {
        const model = currentModel();
        return model ? boundedChoices(model).map(({ id, ...candidate }) => candidate) : null;
    },
    apply(ref, active) {
        const model = currentModel();
        if (!model || typeof active !== "boolean") {
            throw new Error("facet operation unavailable on the current page");
        }
        const matches = boundedChoices(model).filter((item) => item.ref === ref);
        if (matches.length !== 1 || matches[0].active === active) {
            throw new Error("facet choice is stale, ambiguous, or already selected");
        }
        model.toggleSearchItem(matches[0].id);
    },
};
