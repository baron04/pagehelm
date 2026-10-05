/** @odoo-module **/
// Read application-owned mounted state. No source index, authority or Agent plans.
import {FormController} from '@web/views/form/form_controller';
import {ListController} from '@web/views/list/list_controller';
import {KanbanController} from '@web/views/kanban/kanban_controller';
import {RelationalModel} from '@web/model/relational_model/relational_model';
import {DynamicList} from '@web/model/relational_model/dynamic_list';
import {patch} from '@web/core/utils/patch';
import {onMounted, onWillUnmount, toRaw} from '@odoo/owl';

const mounted = new Set(), reads = new WeakMap();
for (const Controller of [FormController, ListController, KanbanController]) {
    patch(Controller.prototype, {
        setup() {
            super.setup(...arguments);
            onMounted(() => mounted.add(this));
            onWillUnmount(() => mounted.delete(this));
        },
    });
}
function begin(model) {
    model = toRaw(model);
    const prior = reads.get(model) || {serial: 0};
    const state = {serial: prior.serial + 1, pending: true, failed: false};
    reads.set(model, state);
    return state;
}
for (const [Type, modelOf] of [[RelationalModel, value => value], [DynamicList, value => value.model]]) {
    patch(Type.prototype, {
        async load() {
            const model = toRaw(modelOf(this)), state = begin(model);
            try {
                const result = await super.load(...arguments);
                const root = model.root;
                const input = JSON.stringify([model.config?.resModel, root?.domain, root?.orderBy]);
                const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input));
                state.query_digest = [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
                if (reads.get(model) === state) state.pending = false;
                return result;
            } catch (error) {
                if (reads.get(model) === state) { state.pending = false; state.failed = true; }
                throw error;
            }
        },
    });
}
function current() {
    const controllers = [...mounted].filter(c => c.rootRef?.el?.isConnected &&
        c.rootRef.el.getClientRects().length && !c.rootRef.el.closest('[hidden],[aria-hidden="true"]'));
    if (controllers.length !== 1) return null;
    const c = controllers[0], root = c.model?.root, state = reads.get(toRaw(c.model));
    const action = c.actionService?.currentController?.action;
    if (!root || !state || state.pending || state.failed || c.model.useSampleModel ||
        root.dirty || root.editedRecord || c.editedRecord ||
        !action || action.type !== 'ir.actions.act_window' ||
        action.id !== c.env.config.actionId || action.res_model !== c.props.resModel) return null;
    const route = action.path || `action-${action.id}`;
    if (typeof route !== 'string' || !/^[a-z0-9][a-z0-9/_-]{0,200}$/.test(route)) return null;
    let path = `/odoo/${route}`;
    if (c instanceof FormController) {
        if (root.isNew || !Number.isSafeInteger(root.resId) || root.resId < 1) return null;
        path += `/${root.resId}`;
    }
    if (path !== location.pathname) return null;
    return {controller: c, root, state, path};
}
window.__woaViewBridge = {
    contains(element) {
        const state = current();
        return Boolean(state && element?.isConnected && state.controller.rootRef.el.contains(element));
    },
    current() {
        const value = current();
        if (!value) return null;
        const {controller, root, state, path} = value;
        const base = {path, revision: state.serial, query_digest: state.query_digest};
        if (controller instanceof FormController) return base;
        if (root.isGrouped || root.hasLimitedCount || !Array.isArray(root.records) ||
            ![root.offset, root.limit, root.count].every(Number.isSafeInteger) ||
            root.offset < 0 || root.limit < 1 || root.count < root.records.length ||
            root.records.length > root.limit || root.offset % root.limit) return null;
        return {...base, page: root.offset / root.limit + 1, page_size: root.limit,
            total: root.count, rows: root.records.length};
    },
};
