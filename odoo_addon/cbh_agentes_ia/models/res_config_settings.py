# -*- coding: utf-8 -*-
import json
import logging
import secrets
import time

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..sso_token import token_para

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    cbh_agentes_url = fields.Char("URL de la plataforma", config_parameter="cbh_agentes_ia.url",
                                  help="Ej.: https://agentes.i-condor.mx (staging: https://cbh-agentes-staging.onrender.com)")
    cbh_agentes_secreto = fields.Char("Secreto compartido (SSO)", config_parameter="cbh_agentes_ia.secreto",
                                      help="Debe ser idéntico a la variable SSO_SECRET del servicio en Render. Se generó al instalar.")
    cbh_agentes_modo = fields.Selection([("pestana", "Abrir en pestaña nueva (recomendado)"), ("embebido", "Embebido dentro de Odoo")],
                                        string="Cómo se abre", config_parameter="cbh_agentes_ia.modo", default="pestana")
    cbh_agentes_api_key = fields.Char("Clave API del usuario técnico", config_parameter="cbh_agentes_ia.odoo_api_key", readonly=True)
    cbh_agentes_datos_render = fields.Text("Datos para Render", compute="_compute_datos_render")

    @api.depends("cbh_agentes_url", "cbh_agentes_secreto", "cbh_agentes_api_key")
    def _compute_datos_render(self):
        icp = self.env["ir.config_parameter"].sudo()
        base_url = icp.get_param("web.base.url") or ""
        for rec in self:
            rec.cbh_agentes_datos_render = "\n".join([
                "ODOO_URL=%s" % base_url,
                "ODOO_DB=%s" % self.env.cr.dbname,
                "ODOO_USER=agente.ia@i-condor.com",
                "ODOO_API_KEY=%s" % (rec.cbh_agentes_api_key or "(pulsa «Generar clave API»)"),
                "ODOO_COMPANY_ID=%s" % self.env.company.id,
                "SSO_SECRET=%s" % (rec.cbh_agentes_secreto or ""),
                "EMBED_ORIGINS=%s" % (base_url if rec.cbh_agentes_modo == "embebido" else ""),
            ])

    def action_regenerar_secreto(self):
        self.ensure_one()
        nuevo = secrets.token_urlsafe(48)
        self.env["ir.config_parameter"].sudo().set_param("cbh_agentes_ia.secreto", nuevo)
        return self._notificar(_("Secreto regenerado. Copia el nuevo valor a SSO_SECRET en Render (hasta entonces el acceso desde Odoo fallará)."), "warning")

    def action_generar_clave_api(self):
        self.ensure_one()
        clave = self.env["cbh.agentes.ia"].generar_clave_api_tecnico()
        return self._notificar(_("Clave API generada para agente.ia@i-condor.com. Cópiala a ODOO_API_KEY en Render: %s") % clave, "success", sticky=True)

    def action_probar_conexion(self):
        self.ensure_one()
        url = (self.cbh_agentes_url or self.env["cbh.agentes.ia"].url_plataforma() or "").strip().rstrip("/")
        secreto = self.cbh_agentes_secreto or self.env["cbh.agentes.ia"]._param("secreto")
        if not url or not secreto:
            raise UserError(_("Captura la URL y el secreto antes de probar."))
        u = self.env.user
        token = token_para(secreto, self.env.cr.dbname, u.login, u.name, u.email or u.login,
                           self.env["cbh.agentes.ia"].rol_de() or "consulta", u.id, fin="verificar")
        try:
            r = requests.get("%s/sso/verificar" % url, params={"token": token}, timeout=15)
            datos = r.json()
        except Exception as e:  # noqa: BLE001
            raise UserError(_("No se pudo contactar la plataforma en %s: %s") % (url, e))
        if not datos.get("ok"):
            raise UserError(_("La plataforma respondió con error: %s") % datos.get("error"))
        msg = _("Conexión correcta con %s v%s (%s). Usuario %s con rol %s. Odoo configurado en la plataforma: %s.") % (
            datos.get("plataforma"), datos.get("version"), datos.get("env"), datos.get("usuario"), datos.get("rol"),
            _("sí") if datos.get("odoo_configurado") else _("todavía no"))
        if self.cbh_agentes_modo == "embebido" and not datos.get("embebido_permitido"):
            msg += " " + _("AVISO: el modo embebido requiere EMBED_ORIGINS en Render con la URL de Odoo.")
        return self._notificar(msg, "success", sticky=True)

    def _notificar(self, mensaje, tipo="info", sticky=False):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": _("Agentes de IA"), "message": mensaje, "type": tipo, "sticky": sticky}}
