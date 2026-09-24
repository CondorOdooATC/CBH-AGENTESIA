"""Capacidades nuevas del copiloto: análisis contable de facturación (resumen, por cliente/mes, líneas por producto),
reportes Excel de cualquier consulta (fuente odoo/facturación), correo por Odoo con aprobación (con Excel adjunto) y
avisos a personas concretas. Todo con datos reales de la instancia (simulada) y por el mismo camino de aprobación."""
from __future__ import annotations

from app import db
from app.agents import autonomia
from app.llm import tools


def _limpiar():
    for e in autonomia.ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, limite=5000):
            db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")


def test_analisis_contable_de_facturacion(sim):
    r = tools.ejecutar("consultar_facturacion", {"dias": 120}, usuario="dir", rol="consulta")
    res = r["resumen"]
    assert res["facturas"] > 0 and res["facturado"] > 0 and res["por_cobrar"] >= 0 and res["por_cliente"] and res["por_mes"]
    assert res["cobrado"] + res["por_cobrar"] == res["facturado"] or abs(res["cobrado"] + res["por_cobrar"] - res["facturado"]) < 0.05
    por_cliente = tools.ejecutar("consultar_facturacion", {"dias": 120, "nivel": "facturas", "agrupar_por": ["cliente"]}, usuario="dir", rol="consulta")
    assert por_cliente["filas"] and "total" in por_cliente["filas"][0] and "saldo" in por_cliente["filas"][0]
    lineas = tools.ejecutar("consultar_facturacion", {"dias": 120, "nivel": "lineas", "agrupar_por": ["producto"], "limite": 5}, usuario="dir", rol="consulta")
    assert lineas["filas"] and "importe" in lineas["filas"][0] and lineas["filas"][0]["producto"]
    detalle = tools.ejecutar("consultar_facturacion", {"dias": 120, "nivel": "facturas", "filtro_texto": "HGZ 17", "limite": 3}, usuario="dir", rol="consulta")
    assert detalle["filas"] and all("HGZ 17" in f["cliente"] for f in detalle["filas"])


def test_reporte_excel_de_cualquier_consulta(sim):
    r = tools.ejecutar("excel_desde_consulta", {"fuente": "facturacion", "titulo": "Cartera por cliente",
                                                "parametros": {"dias": 120, "nivel": "facturas", "agrupar_por": ["cliente"]}}, usuario="dir", rol="consulta")
    assert r.get("id") and r.get("url") and r.get("filas", 1) >= 1
    r2 = tools.ejecutar("excel_desde_consulta", {"fuente": "odoo", "titulo": "Compras por proveedor",
                                                 "parametros": {"modelo": "purchase.order.line", "dominio": [], "agrupar_por": ["partner_id"], "sumar": ["product_qty"]}},
                        usuario="dir", rol="consulta")
    assert r2.get("id") and r2.get("url")
    g = tools.ejecutar("consultar_odoo", {"modelo": "stock.move", "dominio": [["state", "!=", "cancel"]], "agrupar_por": ["product_id"], "sumar": ["product_uom_qty"]},
                       usuario="dir", rol="consulta")
    assert g["filas"] and "product_uom_qty" in g["filas"][0] and g["filas"][0]["registros"]
    for m in ("res.users", "hr.payslip", "ir.mail_server"):
        try:
            tools.ejecutar("consultar_odoo", {"modelo": m}, usuario="dir", rol="admin"); raise AssertionError(m)
        except ValueError:
            pass


def test_correo_con_excel_adjunto_pasa_por_aprobacion(sim):
    _limpiar()
    rep = tools.ejecutar("excel_desde_consulta", {"fuente": "facturacion", "titulo": "Cartera", "parametros": {"dias": 60, "nivel": "facturas"}},
                         usuario="jefa", rol="operacion")
    r = tools.ejecutar("enviar_correo", {"para": ["Laura Contreras", "rsalas@cbh.mx", "externo@proveedor.mx", "Nadie Existe"],
                                         "asunto": "Cartera al día", "cuerpo": "Adjunto la cartera.\nSaludos.", "reporte_id": rep["id"]},
                       usuario="jefa", rol="operacion")
    assert r.get("id") and r["estado"] == "propuesta" and set(r["destinatarios"]) == {"lcontreras@cbh.mx", "rsalas@cbh.mx", "externo@proveedor.mx"}
    assert r["no_resueltos"] and "Nadie Existe" in r["no_resueltos"][0]
    assert "3 destinatario" in r["efecto"] and "adjuntando" in r["efecto"]
    n_mail = len(sim.tablas["mail.mail"])
    out = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí, envíalo"}, usuario="jefa", rol="operacion")
    assert out["estado"] == "ejecutada" and out["odoo_modelo"] == "mail.mail"
    correo = sim.tablas["mail.mail"][-1]
    assert len(sim.tablas["mail.mail"]) == n_mail + 1 and correo["state"] == "sent" and "externo@proveedor.mx" in correo["email_to"]
    assert correo.get("attachment_ids") and sim.tablas["ir.attachment"][-1]["name"].endswith(".xlsx")
    # volver a aprobar no reenvía
    again = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí"}, usuario="jefa", rol="operacion")
    assert again["estado"] == "ejecutada" and len(sim.tablas["mail.mail"]) == n_mail + 1
    # sin destinatarios válidos no se propone; un usuario de consulta no puede enviar
    assert tools.ejecutar("enviar_correo", {"para": ["Nadie Existe"], "asunto": "x", "cuerpo": "y"}, usuario="jefa", rol="operacion")["estado"] == "no_propuesta"
    try:
        tools.ejecutar("enviar_correo", {"para": ["rsalas@cbh.mx"], "asunto": "x", "cuerpo": "y"}, usuario="lector", rol="consulta"); raise AssertionError("permiso")
    except PermissionError:
        pass


def test_aviso_a_personas_concretas(sim):
    _limpiar()
    db.set_ajuste("avisos", {"incluir_admin_agentes": False})
    try:
        r = tools.ejecutar("proponer_accion", {"tipo": "aviso_equipo", "titulo": "Avisar a Laura", "payload": {"equipo": "direccion", "asunto": "Revisar cartera",
                                               "cuerpo": "Hay facturas vencidas", "personas": ["Laura Contreras"]}}, usuario="jefa", rol="operacion")
        a = db.accion(r["id"])
        assert a["payload"]["destinatarios"] == ["Laura Contreras"] and a["payload"]["logins_extra"] == ["lcontreras@cbh.mx"]
        n0 = len(sim.tablas["mail.activity"])
        assert tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí"}, usuario="jefa", rol="operacion")["estado"] == "ejecutada"
        assert len(sim.tablas["mail.activity"]) == n0 + 1 and sim.tablas["mail.activity"][-1]["user_id"][0] == 11
        amb = tools.ejecutar("proponer_accion", {"tipo": "aviso_equipo", "titulo": "x", "payload": {"equipo": "direccion", "asunto": "a", "cuerpo": "b", "personas": ["Nadie"]}},
                             usuario="jefa", rol="operacion")
        assert amb["estado"] == "no_propuesta"
    finally:
        db.set_ajuste("avisos", {})
