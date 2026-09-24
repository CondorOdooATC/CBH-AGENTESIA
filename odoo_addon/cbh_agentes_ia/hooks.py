# -*- coding: utf-8 -*-
"""Instalación: genera un secreto compartido nuevo (si no existe) y crea el usuario técnico que usa la plataforma
para leer/escribir en Odoo (sin contraseña: sólo entra por clave API)."""
import logging
import secrets

_logger = logging.getLogger(__name__)

TECNICO_LOGIN = "agente.ia@i-condor.com"


def post_init_hook(env):
    icp = env["ir.config_parameter"].sudo()
    if not icp.get_param("cbh_agentes_ia.secreto"):
        icp.set_param("cbh_agentes_ia.secreto", secrets.token_urlsafe(48))
    if not icp.get_param("cbh_agentes_ia.modo"):
        icp.set_param("cbh_agentes_ia.modo", "pestana")
    env["cbh.agentes.ia"].sudo().asegurar_usuario_tecnico()
    _logger.info("cbh_agentes_ia: instalado; copiar los datos de Ajustes ▸ Agentes de IA a Render")
