# -*- coding: utf-8 -*-
"""Redirección con token firmado: el usuario de Odoo entra a la plataforma con su sesión de Odoo."""
from odoo import http
from odoo.exceptions import UserError
from odoo.http import request


class CbhAgentesIaController(http.Controller):

    @http.route("/cbh_agentes_ia/abrir", type="http", auth="user", methods=["GET"], website=False)
    def abrir(self, embed=None, **kw):
        try:
            url = request.env["cbh.agentes.ia"].url_acceso(embed=bool(embed))
        except UserError as e:
            return request.make_response(
                "<html><body style='font-family:sans-serif;padding:32px'><h2>Agentes de IA</h2><p>%s</p></body></html>" % e.args[0],
                headers=[("Content-Type", "text/html; charset=utf-8")], status=403)
        return request.redirect(url, code=302, local=False)
