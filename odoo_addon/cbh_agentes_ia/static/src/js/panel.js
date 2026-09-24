/** @odoo-module **/
import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";

/** Acción de cliente que embebe la plataforma de Agentes de IA dentro de Odoo (modo «embebido»). */
export class CbhAgentesPanel extends Component {
    static template = "cbh_agentes_ia.Panel";
    static props = ["*"];
    setup() {
        const params = (this.props.action && this.props.action.params) || {};
        this.url = params.url || "/cbh_agentes_ia/abrir?embed=1";
    }
}
registry.category("actions").add("cbh_agentes_ia.panel", CbhAgentesPanel);
