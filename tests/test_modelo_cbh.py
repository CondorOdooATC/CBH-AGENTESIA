"""Lectura del módulo real de CBH (cbh_operaciones_medicas, Odoo 19): la línea «Consumo de CB Ticket» no tiene fecha,
médico ni técnico; se toman de la cabecera (fecha de cirugía + hora de inicio de anestesia, anestesiólogo, técnico,
quirófano, sub-almacén de surtido), el lote sale del movimiento de consumo real y el importe se estima a costo
estándar. El paciente nunca se lee. Todo con los nombres técnicos reales del módulo."""
from __future__ import annotations

import pandas as pd

from app.odoo import queries, schema

REQ, LINE = "cbh.medical.service.request", "cbh.medical.service.request.line"


class OdooCBH:
    """Cliente mínimo que responde como la base real de CBH+ (nombres de campo del módulo)."""
    uid = 311

    def __init__(self):
        self.leidos: list[tuple] = []
        self.requests = [
            {"id": 900, "name": "HRT0650", "folio_asignado": "650", "state": "closed", "request_date": "2026-09-01", "surgery_date": "2026-09-02",
             "medical_unit_id": [41, "HGZ 33 Monterrey"], "unit_config_id": [3, "HGZ 33 Monterrey"], "operating_room_id": [7, "QX-2"],
             "anesthesia_time_start": 8.5, "anesthesia_time_end": 10.25, "procedure_datetime_start": "2026-09-02 14:40:00",
             "procedure_datetime_end": "2026-09-02 16:00:00", "surgical_procedure": "Colecistectomía", "surgical_specialty": "Cirugía general",
             "event_type": "scheduled", "shift": "morning", "surgeon_id": [201, "Dr. Cirujano"], "anesthesiologist_id": [202, "Dra. Anestesióloga"],
             "technician_employee_id": [55, "Técnico Pérez"], "inventory_source_location_id": [120, "HGZ33/Stock/QX-2"],
             "picking_consumption_id": [5001, "HGZ33/OUT/00012"], "package_price": 18500.0, "billing_package_name": "PAQ-COLE",
             "patient_name": "NO DEBE LEERSE", "company_id": [2, "CBH+"]},
            {"id": 901, "name": "HRT0651", "folio_asignado": "651", "state": "cancelled", "request_date": "2026-09-03", "surgery_date": "2026-09-03",
             "medical_unit_id": [41, "HGZ 33 Monterrey"], "anesthesia_time_start": 0.0, "anesthesia_time_end": 0.0, "company_id": [2, "CBH+"]},
        ]
        self.lines = [
            {"id": 1, "request_id": [900, "HRT0650"], "line_type": "medicine", "product_id": [1001, "SEVOFLURANO 250ML"], "effective_product_id": [1001, "SEVOFLURANO 250ML"],
             "qty_used": 21.5, "quantity_standard": 20.0, "qty_max": 30.0, "uom_id": [11, "mL"], "cbh_weighable": True,
             "initial_weight_g": 300.0, "final_weight_g": 278.0, "consumed_weight_g": 22.0, "consumed_qty_ml": 22.0,
             "final_weighing_date": "2026-09-02 16:05:00", "medical_unit_id": [41, "HGZ 33 Monterrey"], "company_id": [2, "CBH+"], "active": True,
             "create_date": "2026-09-01 09:00:00"},
            {"id": 2, "request_id": [900, "HRT0650"], "line_type": "goods", "product_id": [1004, "PROPOFOL 200MG/20ML"], "effective_product_id": [1044, "PROPOFOL 200MG/20ML (variante B)"],
             "qty_used": 2.0, "quantity_standard": 2.0, "qty_max": 3.0, "uom_id": [1, "Unidades"], "cbh_weighable": False,
             "initial_weight_g": 0.0, "final_weight_g": 0.0, "consumed_weight_g": 0.0, "consumed_qty_ml": 0.0,
             "medical_unit_id": [41, "HGZ 33 Monterrey"], "company_id": [2, "CBH+"], "active": True, "create_date": "2026-09-01 09:00:00"},
            {"id": 3, "request_id": [901, "HRT0651"], "line_type": "goods", "product_id": [1004, "PROPOFOL 200MG/20ML"], "qty_used": 1.0,
             "uom_id": [1, "Unidades"], "cbh_weighable": False, "initial_weight_g": 0.0, "final_weight_g": 0.0, "medical_unit_id": [41, "HGZ 33 Monterrey"],
             "company_id": [2, "CBH+"], "active": True, "create_date": "2026-09-03 09:00:00"},
        ]
        self.move_lines = [
            {"id": 77, "picking_id": [5001, "HGZ33/OUT/00012"], "product_id": [1001, "SEVOFLURANO 250ML"], "lot_id": [31, "LSEV-2026-07"],
             "expiration_date": "2027-01-31 00:00:00", "quantity": 21.5},
            {"id": 78, "picking_id": [5001, "HGZ33/OUT/00012"], "product_id": [1044, "PROPOFOL"], "lot_id": [32, "LPRO-2026-02"],
             "expiration_date": "2026-08-01 00:00:00", "quantity": 2.0},
        ]
        self.productos = [{"id": 1001, "standard_price": 12.4, "uom_id": [11, "mL"]}, {"id": 1004, "standard_price": 48.0, "uom_id": [1, "Unidades"]},
                          {"id": 1044, "standard_price": 50.0, "uom_id": [1, "Unidades"]}]

    # ── introspección ──
    def fields_get(self, modelo, atributos=None):
        def f(nombre, tipo, string="", relation=None):
            m = {"string": string or nombre, "type": tipo}
            if relation:
                m["relation"] = relation
            return nombre, m
        if modelo == REQ:
            return dict([f("name", "char", "Folio Interno"), f("folio_asignado", "char", "Folio Asignado"), f("state", "selection", "Estado"),
                         f("request_date", "date", "Fecha de registro"), f("surgery_date", "date", "Fecha"),
                         f("medical_unit_id", "many2one", "Hospital / Unidad Médica", "res.partner"), f("unit_config_id", "many2one", "Unidad Médica", "cbh.medical.unit.config"),
                         f("operating_room_id", "many2one", "Número de Quirófano", "cbh.medical.operating.room"),
                         f("anesthesia_time_start", "float", "Inicio anestesia"), f("anesthesia_time_end", "float", "Fin anestesia"),
                         f("procedure_datetime_start", "datetime"), f("procedure_datetime_end", "datetime"),
                         f("surgical_procedure", "char", "Procedimiento Quirúrgico"), f("surgical_specialty", "char", "Especialidad Quirúrgica"),
                         f("event_type", "selection", "Evento"), f("shift", "selection", "Turno"),
                         f("surgeon_id", "many2one", "Nombre del Cirujano", "res.partner"), f("anesthesiologist_id", "many2one", "Nombre del Anestesiólogo", "res.partner"),
                         f("technician_employee_id", "many2one", "Técnico", "hr.employee"), f("inventory_source_location_id", "many2one", "Ubicación origen del ticket", "stock.location"),
                         f("picking_consumption_id", "many2one", "Consumo real", "stock.picking"), f("package_price", "monetary", "Precio del paquete"),
                         f("billing_package_name", "char", "Nombre del Paquete"), f("patient_name", "char", "Nombre completo"), f("patient_nss", "char", "NSS"),
                         f("line_ids", "one2many", "Consumos", LINE), f("company_id", "many2one", "Empresa", "res.company"), f("create_date", "datetime")])
        if modelo == LINE:
            return dict([f("request_id", "many2one", "CB Ticket", REQ), f("line_type", "selection", "Tipo"), f("product_id", "many2one", "Producto", "product.product"),
                         f("effective_product_id", "many2one", "Producto efectivo (Inventario)", "product.product"), f("qty_used", "float", "Cantidad utilizada"),
                         f("quantity_standard", "float", "Cantidad del paquete"), f("qty_max", "float", "Máximo"), f("uom_id", "many2one", "Unidad de medida", "uom.uom"),
                         f("cbh_weighable", "boolean", "Control por pesaje"), f("initial_weight_g", "float", "Peso inicial (g)"), f("final_weight_g", "float", "Peso final (g)"),
                         f("consumed_weight_g", "float", "Consumo calculado (g)"), f("consumed_qty_ml", "float", "Consumo calculado (mL)"),
                         f("final_weighing_date", "datetime", "Fecha pesaje final"), f("medical_unit_id", "many2one", "Hospital / Unidad Médica", "res.partner"),
                         f("company_id", "many2one", "Empresa", "res.company"), f("active", "boolean"), f("create_date", "datetime")])
        if modelo == "stock.move.line":
            return dict([f("picking_id", "many2one", "", "stock.picking"), f("product_id", "many2one", "", "product.product"), f("lot_id", "many2one", "", "stock.lot"),
                         f("expiration_date", "datetime"), f("quantity", "float"), f("date", "datetime"), f("state", "selection"), f("location_id", "many2one", "", "stock.location"),
                         f("location_dest_id", "many2one", "", "stock.location"), f("product_uom_id", "many2one", "", "uom.uom")])
        if modelo in ("product.product", "stock.quant", "stock.move", "stock.warehouse", "stock.location", "res.partner", "hr.employee",
                      "stock.warehouse.orderpoint", "purchase.order.line", "uom.uom", "stock.picking"):
            return dict([f("id", "integer"), f("name", "char"), f("display_name", "char"), f("product_id", "many2one", "", "product.product"),
                         f("uom_id", "many2one", "", "uom.uom"), f("standard_price", "float"), f("active", "boolean"), f("type", "selection"),
                         f("quantity", "float"), f("reserved_quantity", "float"), f("location_id", "many2one", "", "stock.location"), f("lot_id", "many2one", "", "stock.lot"),
                         f("date", "datetime"), f("state", "selection"), f("complete_name", "char"), f("usage", "selection"), f("product_uom_id", "many2one", "", "uom.uom"),
                         f("product_min_qty", "float"), f("product_max_qty", "float"), f("factor", "float"), f("rounding", "float")])
        return {}

    def search_read(self, modelo, dominio, campos=None, limite=0, orden=None, offset=0):
        self.leidos.append((modelo, dominio, tuple(campos or [])))
        if modelo == "ir.model":
            return [{"id": i, "model": m, "name": m, "modules": "cbh_operaciones_medicas" if m.startswith("cbh.") else "base", "transient": False, "state": "base"}
                    for i, m in enumerate([REQ, LINE, "product.product", "stock.quant", "stock.move", "stock.move.line", "stock.warehouse", "stock.location",
                                           "res.partner", "hr.employee", "stock.warehouse.orderpoint", "uom.uom", "stock.picking"], 1)]
        if modelo == LINE:
            assert all("patient" not in c for c in (campos or [])), "nunca se leen campos de paciente"
            fecha_dom = [c for c in dominio if isinstance(c, list) and c[0] == "request_id.surgery_date"]
            assert fecha_dom, "la fecha se filtra por la cabecera (request_id.surgery_date)"
            assert any(isinstance(c, list) and c[0] == "request_id.state" and c[1] == "not in" for c in dominio), "excluye cancelados en el dominio"
            return [dict(l) for l in self.lines if l["request_id"][0] != 901]     # el 901 está cancelado: Odoo no lo devolvería
        if modelo == REQ:
            assert all("patient" not in c for c in (campos or [])), "nunca se leen campos de paciente"
            ids = next(c[2] for c in dominio if isinstance(c, list) and c[0] == "id")
            return [{k: v for k, v in r.items() if k in (campos or []) or k == "id"} for r in self.requests if r["id"] in ids]
        if modelo == "stock.move.line":
            return [dict(m) for m in self.move_lines]
        if modelo == "product.product":
            ids = next((c[2] for c in dominio if isinstance(c, list) and c[0] == "id"), None)
            return [dict(p) for p in self.productos if ids is None or p["id"] in ids]
        return []

    def search_read_all(self, modelo, dominio, campos, pagina=5000, tope=200_000, orden="id"):
        return self.search_read(modelo, dominio, campos, limite=0)

    def search_read_por_ids(self, modelo, ids, campos, dominio_extra=None, campo="id", bloque=1000):
        ids = sorted({int(x) for x in ids})
        out = []
        for i in range(0, len(ids), bloque):
            out.extend(self.search_read(modelo, [[campo, "in", ids[i:i + bloque]]] + list(dominio_extra or []), campos, limite=0))
        return out

    def search_count(self, modelo, dominio):
        return 1

    def existe_modelo(self, modelo):
        return bool(self.fields_get(modelo))


def test_mapeo_y_lectura_del_cb_ticket_real(sim):
    cli = OdooCBH()
    m = schema.descubrir(cli, guardar_resultado=True)
    try:
        f, c = m["entidades"]["folio"], m["entidades"]["consumo"]
        assert f["modelo"] == REQ and c["modelo"] == LINE and schema.en_respaldo(m) is False
        assert f["campos"]["fecha"] == "surgery_date" and f["campos"]["hospital"] == "medical_unit_id" and f["campos"]["medico"] == "anesthesiologist_id"
        assert f["campos"]["empleado"] == "technician_employee_id" and f["campos"]["ubicacion"] == "inventory_source_location_id"
        assert f["campos"]["hora_inicio"] == "anesthesia_time_start" and f["campos"]["quirofano"] == "operating_room_id" and f["campos"]["paciente"] == "patient_name"
        assert c["campos"]["folio_id"] == "request_id" and c["campos"]["cantidad"] == "qty_used" and c["campos"]["peso_inicial"] == "initial_weight_g"
        assert c["campos"]["consumo_ml"] == "consumed_qty_ml" and c["campos"]["pesable"] == "cbh_weighable" and c["campos"]["hospital"] == "medical_unit_id"
        assert c["campos"]["producto_efectivo"] == "effective_product_id" and c["campos"]["unidad"] == "uom_id"

        df = queries.consumo(dias=30, cli=cli)
        assert len(df) == 2 and set(df["folio"]) == {"HRT0650"}                       # el folio cancelado no entra
        sev = df[df["producto_id"] == 1001].iloc[0]
        assert sev["fecha"] == pd.Timestamp("2026-09-02 08:30:00")                    # fecha de cirugía + inicio de anestesia (8.5 h)
        assert abs(sev["duracion_min"] - 105.0) < 0.01                                 # 8:30 → 10:15
        assert sev["hospital"] == "HGZ 33 Monterrey" and sev["medico"] == "Dra. Anestesióloga" and sev["auxiliar"] == "Técnico Pérez"
        assert sev["subalmacen"] == "HGZ33/Stock/QX-2" and sev["ubicacion"] == "HGZ33/Stock/QX-2" and sev["almacen"] == "HGZ33"
        assert sev["quirofano"] == "QX-2" and sev["tipo_evento"] == "scheduled" and sev["estado_folio"] == "closed"
        assert sev["lote"] == "LSEV-2026-07" and str(sev["caducidad"])[:10] == "2027-01-31"   # del movimiento de consumo real
        assert sev["peso_inicial"] == 300.0 and sev["peso_final"] == 278.0 and sev["consumo_ml"] == 22.0 and bool(sev["pesable"]) is True
        assert sev["cantidad"] == 21.5 and sev["unidad"] == "mL" and sev["importe"] == round(21.5 * 12.4, 2) and bool(sev["importe_estimado"])
        pro = df[df["producto"].str.contains("variante B")].iloc[0]
        assert pro["producto_id"] == 1044 and pro["lote"] == "LPRO-2026-02"          # manda la variante que se movió en inventario
        assert "patient_name" not in df.columns and not any("NO DEBE LEERSE" in str(v) for v in df.values.ravel())
        # el motor de anomalías entiende la línea real: pesaje 0/0 no es pesaje; el consumo de báscula usa el mL calculado por Odoo
        from app.ml import anomalias as A
        res = A.detectar(df, A.ConfigAnomalias(min_muestras=1))
        assert res["basculas"]["lineas_con_bascula"] == 1 and res["basculas"]["imposibles"] == 0
    finally:
        schema.descubrir(sim)      # restaurar el mapeo del simulador para las demás pruebas
