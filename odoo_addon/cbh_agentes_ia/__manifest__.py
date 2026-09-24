# -*- coding: utf-8 -*-
{
    "name": "Agentes de IA · CBH (puente)",
    "summary": "Abre la plataforma de Agentes de IA (Ingeniería Cóndor) desde Odoo con la sesión de Odoo, sin contraseña adicional",
    "description": """
Módulo puente para la plataforma «CBH · Agentes de IA» (Control Inteligente de Consumo y Pronóstico de Demanda y Resurtido).

• Menú «Agentes de IA» en Odoo que abre la plataforma (pestaña nueva o embebida).
• Acceso sin segundo login: token firmado (HMAC) de 60 segundos con el rol que corresponde al grupo de Odoo del usuario.
• Tres grupos de seguridad: Consulta, Operación y Administrador de Agentes de IA.
• Ajustes ▸ Agentes de IA: URL de la plataforma, secreto compartido, modo de apertura, prueba de conexión, usuario técnico y
  los datos listos para copiar a Render.

La plataforma corre en su propio servicio (Render); este módulo NO instala dependencias de Python ni tareas programadas en Odoo.
""",
    "version": "19.0.1.3.0",
    "category": "Productivity",
    "author": "Ingeniería Cóndor",
    "website": "https://www.i-condor.com",
    "license": "OPL-1",
    "depends": ["base", "web", "base_setup"],
    "data": [
        "security/groups.xml",
        "data/params.xml",
        "views/res_config_settings_views.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "cbh_agentes_ia/static/src/js/panel.js",
            "cbh_agentes_ia/static/src/xml/panel.xml",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": True,
}
