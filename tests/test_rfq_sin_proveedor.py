"""Producto sin proveedor configurado: la solicitud de cotización se crea de todos modos. Si la base permite dejar el
proveedor en blanco, queda en blanco; si Odoo lo exige (comportamiento estándar), se usa el contacto «PROVEEDOR POR
DEFINIR» (creado una sola vez) y la tarjeta/chatter avisan que Compras debe asignarlo antes de confirmar."""
from __future__ import annotations

from app import db
from app.agents import autonomia
from app.llm import tools
from app.odoo import acciones as OA


def _sin_proveedor(sim, pid):
    quitados = [si for si in sim.tablas["product.supplierinfo"] if si["product_id"][0] == pid]
    sim.tablas["product.supplierinfo"] = [si for si in sim.tablas["product.supplierinfo"] if si["product_id"][0] != pid]
    return quitados


def test_rfq_con_proveedor_por_definir_cuando_odoo_lo_exige(sim):
    quitados = _sin_proveedor(sim, 1016)
    try:
        for e in autonomia.ESTADOS_PENDIENTES:
            for a in db.acciones(estado=e, limite=5000):
                db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")
        assert OA.proveedor_de(1016, sim) is None
        n_partners = len(sim.tablas["res.partner"])
        r = tools.ejecutar("proponer_accion", {"tipo": "solicitud_compra", "titulo": "Compra bloqueador", "payload": {"producto": "Bloqueador", "cantidad": 5}},
                           usuario="jefa", rol="operacion")
        a = db.accion(r["id"])
        assert a["payload"]["sin_proveedor"] is True and "sin proveedor" in a["motivo"].lower()        # la tarjeta lo dice desde la propuesta
        out = tools.ejecutar("aprobar_accion", {"id": r["id"], "confirmacion_usuario": "sí"}, usuario="jefa", rol="operacion")
        assert out["estado"] == "ejecutada" and out["odoo_modelo"] == "purchase.order"
        po = next(p for p in sim.tablas["purchase.order"] if p["id"] == out["odoo"]["id"])
        marcador = next(p for p in sim.tablas["res.partner"] if p["name"] == "PROVEEDOR POR DEFINIR")
        assert po["partner_id"][0] == marcador["id"] and marcador.get("supplier_rank") == 1
        assert len(sim.tablas["res.partner"]) == n_partners + 1
        assert "por definir" in (out.get("advertencia") or "").lower() and db.accion(r["id"])["revalidacion"]
        assert any("PROVEEDOR POR DEFINIR" in (m.get("cuerpo") or "") for m in sim.mensajes if m["modelo"] == "purchase.order")
        # segunda RFQ sin proveedor: reutiliza el contacto, no crea otro
        r2 = OA.crear_solicitud_compra(1016, 3.0, referencia="Agente IA · prueba spd 2", cli=sim)
        assert r2["sin_proveedor"] and len(sim.tablas["res.partner"]) == n_partners + 1
    finally:
        sim.tablas["product.supplierinfo"].extend(quitados)


def test_rfq_en_blanco_si_la_base_lo_permite(sim, monkeypatch):
    quitados = _sin_proveedor(sim, 1015)
    monkeypatch.setattr(sim, "proveedor_obligatorio", False, raising=False)     # una base donde el proveedor no es obligatorio
    try:
        r = OA.crear_solicitud_compra(1015, 4.0, referencia="Agente IA · prueba spd blanco", cli=sim)
        po = next(p for p in sim.tablas["purchase.order"] if p["id"] == r["id"])
        assert not po.get("partner_id") and "en blanco" in r["advertencia"]
    finally:
        sim.tablas["product.supplierinfo"].extend(quitados)
