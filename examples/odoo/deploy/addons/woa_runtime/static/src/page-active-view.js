// Read the rendered Odoo controller, not its URL or a possibly absent switch button.
// More than one visible controller is ambiguous (for example during a transition).
export function readActiveView() {
    const controllers = [...document.querySelectorAll('.o_action_manager .o_view_controller')]
        .filter(element => element.getClientRects().length > 0);
    if (controllers.length !== 1) return undefined;
    const match = controllers[0].className.match(/\bo_(list|kanban|form|pivot|graph|calendar|activity|map)_view\b/);
    return match ? match[1][0].toUpperCase() + match[1].slice(1) : undefined;
}
