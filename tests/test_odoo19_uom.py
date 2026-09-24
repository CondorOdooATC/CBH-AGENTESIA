"""Odoo 19 cambió las unidades de medida: product.product ya no tiene ``uom_po_id`` (la unidad de compra vive en el
proveedor, ``product.supplierinfo.product_uom_id``), uom.uom es relativa (``relative_factor``/``relative_uom_id``) y
purchase.order.line usa ``product_uom_id``. La plataforma debe crear la RFQ en frascos igual que en Odoo ≤ 18."""
from __future__ import annotations

from app import db
from app.agents import autonomia
from app.odoo import acciones as OA, queries
from app.odoo.simulado import OdooSimulado


class Odoo19(OdooSimulado):
    """Simulador con el esquema de unidades de Odoo 19."""

    def __init__(self):
        super().__init__()
        for p in self.tablas["product.product"]:
            p.pop("uom_po_id", None)
        for u in self.tablas["uom.uom"]:
            f = u.pop("factor")
            if f == 1:
                u["relative_factor"], u["relative_uom_id"] = 1.0, False
            else:
                # 1 frasco = 250 mL  ↔  factor antiguo 1/250
                ref = next(x for x in self.tablas["uom.uom"] if x["id"] in (1, 11, 12, 13) and self._misma_familia(x["id"], u["id"]))
                u["relative_factor"], u["relative_uom_id"] = round(1.0 / f, 6), [ref["id"], ref["name"]]
        # unidad de compra en el proveedor principal (Odoo 19)
        from app.odoo.simulado import UOM_COMPRA, UOMS
        for si in self.tablas["product.supplierinfo"]:
            pid = si["product_id"][0]
            si["product_uom_id"] = [UOM_COMPRA.get(pid, 1), UOMS[UOM_COMPRA.get(pid, 1)]]
        for l in self.tablas["purchase.order.line"]:
            l["product_uom_id"] = l.pop("product_uom")

    @staticmethod
    def _misma_familia(ref_id, uid):
        return {14: 11, 15: 11, 16: 11, 17: 1, 18: 13}.get(uid) == ref_id

    def fields_get(self, modelo, atributos=None):
        f = super().fields_get(modelo)
        if modelo == "product.product":
            f.pop("uom_po_id", None)
        return f


def test_rfq_en_frascos_con_unidades_de_odoo19():
    from app.odoo import client, schema
    s19 = Odoo19()
    anterior = client._cliente
    client.set_client(s19)
    queries._UOM_CACHE.clear(); queries._CAMPO_UOM_CACHE.clear()
    try:
        db.init_db(); schema.descubrir(s19)
        assert "uom_po_id" not in s19.fields_get("product.product")
        cat = queries.catalogo_uom(s19)
        assert abs(cat[14]["factor"] - 1 / 250) < 1e-9 and cat[11]["factor"] == 1.0      # frasco 250 mL reconstruido desde relative_factor
        uc = queries.unidad_compra_de(1001, s19)
        assert uc["uom_id"] == 14 and uc["ratio"] == 250.0
        todas = queries.unidades_compra(s19)
        assert todas[1001]["ratio"] == 250.0 and todas[1001]["unidad_compra"] == "Frasco 250 mL" and 1004 not in todas   # 1004 se compra en su unidad base
        assert queries.campo_uom("purchase.order.line", s19) == "product_uom_id"
        n0 = len(s19.tablas["purchase.order"])
        r = OA.crear_solicitud_compra(1001, 2400.0, referencia="Agente IA · prueba odoo19", cli=s19)
        assert r["modelo"] == "purchase.order" and len(s19.tablas["purchase.order"]) == n0 + 1
        linea = next(l for l in s19.tablas["purchase.order.line"] if l["order_id"][0] == r["id"])
        assert linea["product_qty"] == 10 and linea["product_uom_id"][0] == 14 and "product_uom" not in linea   # 2,400 mL → 10 frascos (techo)
        # y desde el chat, por el mismo camino
        from app.llm import tools
        for e in autonomia.ESTADOS_PENDIENTES:
            for a in db.acciones(estado=e, limite=5000):
                db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")
        p = tools.ejecutar("proponer_accion", {"tipo": "solicitud_compra", "titulo": "Compra", "payload": {"producto": "Guantes", "cantidad_compra": 3}},
                           usuario="jefa", rol="operacion")
        assert p["cantidad"] == 150.0 and p["unidad"] == "par"                        # 3 cajas de 50 pares
        out = tools.ejecutar("aprobar_accion", {"id": p["id"], "confirmacion_usuario": "sí"}, usuario="jefa", rol="operacion")
        assert out["estado"] == "ejecutada" and out["odoo_modelo"] == "purchase.order"
    finally:
        client.set_client(anterior)
        queries._UOM_CACHE.clear(); queries._CAMPO_UOM_CACHE.clear()
        if anterior is not None:
            schema.descubrir(anterior)
