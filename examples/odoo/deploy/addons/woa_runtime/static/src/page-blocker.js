// An Odoo action can update its URL and then fail to render, leaving an error dialog over
// the previous controller. This is an application-owned readiness fact, not a navigation
// permission or a textual guess made by the generic Runtime.
export function readPageBlocker() {
    return [...document.querySelectorAll('.o_technical_modal')].some((dialog) =>
        dialog.isConnected && dialog.getClientRects().length > 0);
}
