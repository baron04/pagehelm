/** @odoo-module **/

// Read the live form controller, not the address bar: Odoo may change the URL before
// replacing the rendered record. A model/id pair is exposed only for one mounted form.
import {FormController} from "@web/views/form/form_controller";
import {patch} from "@web/core/utils/patch";
import {onMounted, onWillUnmount} from "@odoo/owl";

const mounted = new Set();

patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        onMounted(() => mounted.add(this));
        onWillUnmount(() => mounted.delete(this));
    },
});

window.__woaRecordBridge = {
    current() {
        const visible = [...mounted].filter((form) => form.rootRef?.el?.isConnected);
        if (visible.length !== 1) return null;
        const record = visible[0].model?.root;
        if (!record || record.isNew || !Number.isSafeInteger(record.resId)
                || record.resId <= 0 || typeof record.resModel !== "string"
                || !/^[A-Za-z_][A-Za-z0-9_.]{0,127}$/.test(record.resModel)) return null;
        return `/odoo/${record.resModel}/${record.resId}`;
    },
};
