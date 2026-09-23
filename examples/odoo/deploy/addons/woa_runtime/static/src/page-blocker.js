// An Odoo action can update its URL and then fail to render, leaving an error dialog over
// the previous controller. This is an application-owned readiness fact, not a navigation
// permission or a textual guess made by the generic Runtime.
export function readPageBlocker() {
    // Odoo uses the same technical modal wrapper for ordinary action wizards. Only an
    // error dialog blocks settlement; a Pay wizard is a ready page state, not an error.
    return [...document.querySelectorAll('.o_technical_modal .o_error_dialog')].some((dialog) =>
        dialog.isConnected && dialog.getClientRects().length > 0);
}
