"""Lo que se le pide al copiloto se hace de verdad: «crea una orden de compra de 10 frascos de sevoflurano» y «manda 20
piezas de propofol del CEDIS a HGZ 17» terminan como documentos reales en Odoo tras la confirmación explícita del usuario,
por el mismo camino (roles, candado, revalidación, idempotencia) que el botón Aprobar. Sin confirmación no se ejecuta;
un usuario de consulta no puede aprobar; una cantidad en frascos se convierte con la conversión real de Odoo."""
from __future__ import annotations

import pytest

from app import db
from app.agents import autonomia
from app.llm import tools


def _limpiar():
    for e in autonomia.ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, limite=5000):
            db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")


def test_orden_de_compra_pedida_en_frascos_se_crea_en_odoo(sim):
    _limpiar()
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": True})     # aquí se prueba el camino de dos aprobaciones
    n0 = len(sim.tablas["purchase.order"])
    r = tools.ejecutar("proponer_accion", {"tipo": "solicitud_compra", "titulo": "Orden de compra de sevoflurano",
                                           "payload": {"producto": "Sevoflurano", "cantidad_compra": 10}, "motivo": "lo pidió el usuario"},
                       usuario="jefa", rol="operacion")
    assert r.get("id") and r["estado"] == "propuesta"
    assert r["cantidad"] == 2500.0 and r["unidad"] == "mL"                     # 10 frascos × 250 mL, con la conversión de Odoo
    assert "10" in r["titulo"] and "frasco" in r["titulo"].lower()
    assert "RFQ" in r["efecto"] and "aprob" in r["siguiente_paso"]
    assert len(sim.tablas["purchase.order"]) == n0                            # proponer NO escribe en Odoo
    # sin confirmación explícita, no se aprueba
    s = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "¿cuánto cuesta?"}, usuario="jefa", rol="operacion")
    assert s["estado"] == "sin_confirmar" and len(sim.tablas["purchase.order"]) == n0
    # un usuario de consulta no puede aprobar aunque confirme
    with pytest.raises(PermissionError):
        tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí, apruébala"}, usuario="lector", rol="consulta")
    # con confirmación: 2,500 mL es riesgo alto (≥ 50 % del tope) → primera aprobación de operación queda parcial…
    out = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "Sí, apruébala y créala"}, usuario="jefa", rol="operacion")
    assert out["estado"] == "aprobada_parcial" and len(sim.tablas["purchase.order"]) == n0
    # …y la segunda, de un administrador distinto, la ejecuta: se crea la RFQ en Odoo en la unidad de compra
    out = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí, adelante"}, usuario="director", rol="admin")
    assert out["estado"] == "ejecutada" and out["odoo_modelo"] == "purchase.order" and out["odoo_ref"]
    assert len(sim.tablas["purchase.order"]) == n0 + 1
    po = sim.tablas["purchase.order"][-1]
    linea = next(l for l in sim.tablas["purchase.order.line"] if l["order_id"][0] == po["id"])
    assert linea["product_qty"] == 10 and linea["product_uom"] == 14                 # 10 frascos, unidad de compra
    # volver a aprobar no duplica ni falla
    again = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí"}, usuario="director", rol="admin")
    assert again["estado"] == "ejecutada" and len(sim.tablas["purchase.order"]) == n0 + 1
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": False})
    # política por defecto (petición del cliente): una sola aprobación de administrador basta para riesgo alto
    r2 = tools.ejecutar("proponer_accion", {"tipo": "solicitud_compra", "titulo": "Compra grande", "payload": {"producto": "Desflurano", "cantidad_compra": 12}},
                        usuario="jefa", rol="operacion")
    assert r2["riesgo"] == "alto"
    assert tools.ejecutar("aprobar_accion", {"id": r2["id"], "confirmacion_usuario": "sí"}, usuario="jefa", rol="operacion")["estado"] == "propuesta"   # operación no basta
    assert tools.ejecutar("aprobar_accion", {"id": r2["id"], "confirmacion_usuario": "sí"}, usuario="director", rol="admin")["estado"] == "ejecutada"


def test_movimiento_de_almacen_pedido_por_chat_se_crea_en_odoo(sim):
    _limpiar()
    n0 = len(sim.tablas["stock.picking"])
    r = tools.ejecutar("proponer_accion", {"tipo": "transferencia_interna", "titulo": "Mandar propofol a HGZ 17",
                                           "payload": {"producto": "Midazolam", "cantidad": 20, "origen": "CEDIS-LR/Stock", "destino": "HGZ17/Stock"},
                                           "motivo": "lo pidió el usuario"}, usuario="jefa", rol="operacion")
    assert r.get("id") and r["estado"] == "propuesta" and r["unidad"] == "pz" and "20" in r["titulo"]
    out = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "hazlo"}, usuario="jefa", rol="operacion")
    assert out["estado"] == "ejecutada" and out["odoo_modelo"] == "stock.picking"
    assert len(sim.tablas["stock.picking"]) == n0 + 1
    move = next(m for m in sim.tablas["stock.move"] if m.get("picking_id") and m["picking_id"][0] == sim.tablas["stock.picking"][-1]["id"])
    assert move["product_uom_qty"] == 20.0 and move["product_id"][0] == 1007
    # pedir más de lo que el origen puede ceder no se propone (y se explica)
    r2 = tools.ejecutar("proponer_accion", {"tipo": "transferencia_interna", "titulo": "x",
                                            "payload": {"producto": "Midazolam", "cantidad": 999999, "origen": "CEDIS-LR/Stock", "destino": "HGZ17/Stock"}},
                        usuario="jefa", rol="operacion")
    assert r2["estado"] == "no_propuesta" and "puede ceder" in r2["motivo"]


def test_rechazo_desde_chat_y_falta_de_cantidad(sim):
    _limpiar()
    with pytest.raises(ValueError, match="cantidad"):
        tools.ejecutar("proponer_accion", {"tipo": "solicitud_compra", "titulo": "x", "payload": {"producto": "Sevoflurano"}}, usuario="jefa", rol="operacion")
    r = tools.ejecutar("proponer_accion", {"tipo": "solicitud_compra", "titulo": "x", "payload": {"producto": "Desflurano", "cantidad_compra": 2}},
                       usuario="jefa", rol="operacion")
    out = tools.ejecutar("rechazar_accion", {"id": r["id"], "motivo": "ya se compró ayer"}, usuario="jefa", rol="operacion")
    assert db.accion(r["id"])["estado"] == "rechazada"
