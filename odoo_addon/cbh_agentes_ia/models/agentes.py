# -*- coding: utf-8 -*-
"""Lógica del puente: rol según grupos de Odoo, token firmado, acción del menú y usuario técnico."""
import inspect
import logging
from urllib.parse import quote

from odoo import _, api, models
from odoo.exceptions import UserError

from ..sso_token import token_para

_logger = logging.getLogger(__name__)

TECNICO_LOGIN = "agente.ia@i-condor.com"
GRUPOS_TECNICO = ("base.group_user", "stock.group_stock_manager", "purchase.group_purchase_user", "hr.group_hr_user",
                  "base.group_partner_manager")


class CbhAgentesIa(models.AbstractModel):
    _name = "cbh.agentes.ia"
    _description = "Agentes de IA · puente con la plataforma"

    # ── parámetros ──────────────────────────────────────────────────────────
    @api.model
    def _param(self, clave, defecto=""):
        return self.env["ir.config_parameter"].sudo().get_param("cbh_agentes_ia." + clave, defecto) or defecto

    @api.model
    def url_plataforma(self):
        return (self._param("url") or "").strip().rstrip("/")

    # ── rol: el más alto de los grupos de Agentes de IA del usuario; NUNCA «condor» (sólo Cóndor lo da en la plataforma) ──
    @api.model
    def rol_de(self, user=None):
        user = user or self.env.user
        if user.has_group("cbh_agentes_ia.group_admin"):
            return "admin"
        if user.has_group("cbh_agentes_ia.group_operacion"):
            return "operacion"
        if user.has_group("cbh_agentes_ia.group_consulta"):
            return "consulta"
        return None

    @api.model
    def token_usuario(self, fin="sso", embed=False):
        secreto = self._param("secreto")
        if not secreto:
            raise UserError(_("Falta el secreto compartido en Ajustes ▸ Agentes de IA."))
        rol = self.rol_de()
        if not rol:
            raise UserError(_("Tu usuario no tiene acceso a Agentes de IA: pide a un administrador el grupo Consulta, Operación o Administrador."))
        u = self.env.user
        return token_para(secreto, self.env.cr.dbname, u.login, u.name, u.email or u.login, rol, u.id, fin=fin, embed=embed)

    @api.model
    def url_acceso(self, embed=False):
        base = self.url_plataforma()
        if not base:
            raise UserError(_("Configura la URL de la plataforma en Ajustes ▸ Agentes de IA."))
        return "%s/sso?token=%s%s" % (base, quote(self.token_usuario(embed=embed)), "&embed=1" if embed else "")

    # ── acción del menú: pestaña nueva (por omisión) o embebido en Odoo ──
    @api.model
    def accion_abrir(self):
        if not self.url_plataforma():
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": _("Agentes de IA"), "type": "warning", "sticky": True,
                               "message": _("La plataforma aún no está configurada: Ajustes ▸ Agentes de IA ▸ URL de la plataforma.")}}
        if not self.rol_de():
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": _("Agentes de IA"), "type": "danger", "sticky": True,
                               "message": _("No tienes acceso: pide a un administrador el grupo «Agentes de IA / Consulta, Operación o Administrador».")}}
        if self._param("modo", "pestana") == "embebido":
            return {"type": "ir.actions.client", "tag": "cbh_agentes_ia.panel", "name": _("Agentes de IA"),
                    "params": {"url": "/cbh_agentes_ia/abrir?embed=1"}}
        return {"type": "ir.actions.act_url", "url": "/cbh_agentes_ia/abrir", "target": "new"}

    # ── usuario técnico (la plataforma lee/escribe con él vía clave API; sin contraseña interactiva) ──
    @api.model
    def asegurar_usuario_tecnico(self):
        Users = self.env["res.users"].sudo()
        tecnico = Users.with_context(active_test=False).search([("login", "=", TECNICO_LOGIN)], limit=1)
        campo_grupos = "group_ids" if "group_ids" in Users._fields else "groups_id"
        grupos = []
        for xmlid in GRUPOS_TECNICO:
            g = self.env.ref(xmlid, raise_if_not_found=False)
            if g:
                grupos.append(g.id)
        vals = {"name": "Agentes de IA (Ingeniería Cóndor)", "login": TECNICO_LOGIN, "email": TECNICO_LOGIN, "active": True,
                campo_grupos: [(6, 0, grupos)]}
        companias = self.env["res.company"].sudo().search([])
        if companias:
            vals["company_ids"] = [(6, 0, companias.ids)]
        if tecnico:
            tecnico.write(vals)
        else:
            tecnico = Users.create(vals)
        return tecnico

    @api.model
    def generar_clave_api_tecnico(self):
        """Genera (y guarda para mostrarla en Ajustes) una clave API del usuario técnico; revoca las anteriores del módulo."""
        tecnico = self.asegurar_usuario_tecnico()
        Keys = self.env["res.users.apikeys"].sudo()
        anteriores = Keys.search([("user_id", "=", tecnico.id), ("name", "=", "Agentes de IA · CBH")])
        if anteriores:
            anteriores.unlink()
        gen = Keys.with_user(tecnico).sudo()
        firma = inspect.signature(gen._generate)
        nombre = "Agentes de IA · CBH"
        if "expiration_date" in firma.parameters:
            try:
                clave = gen._generate("rpc", nombre, False)          # sin caducidad (la revoca Cóndor al rotarla)
            except Exception:  # noqa: BLE001 — algunas versiones exigen fecha: un año
                from datetime import datetime, timedelta
                clave = gen._generate("rpc", nombre, datetime.now() + timedelta(days=365))
        else:
            clave = gen._generate("rpc", nombre)
        self.env["ir.config_parameter"].sudo().set_param("cbh_agentes_ia.odoo_api_key", clave)
        _logger.info("cbh_agentes_ia: clave API del usuario técnico regenerada por %s", self.env.user.login)
        return clave
