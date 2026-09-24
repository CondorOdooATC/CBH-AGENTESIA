"""Odoo simulado: un "gemelo" en memoria de CBH+ con datos realistas.

Sirve para dos cosas:
  • MODO DEMO (``DEMO_MODE=true``): la plataforma corre completa sin conectarse
    a Odoo, ideal para presentarla a dirección antes de tener credenciales.
  • Pruebas automatizadas: los agentes se validan contra anomalías "sembradas"
    cuya respuesta correcta conocemos.

Incluye 18 meses de folios de anestesia con lecturas de báscula, 6 unidades
médicas (La Raza con 4 sub-almacenes), 15 auxiliares, 12 anestesiólogos,
existencias con lotes y caducidades, proveedores con tiempos de entrega y
un conjunto de anomalías sembradas (sobreconsumo sistemático de un auxiliar,
consumos nocturnos sin cirugía, discrepancias de báscula, lote viajero,
duplicados, lotes caducados, cambio de nivel en un sub-almacén, etc.).
"""
from __future__ import annotations

from .client import OdooError

import math
import random
from datetime import datetime, timedelta
from typing import Any

# ── Catálogos maestros ──────────────────────────────────────────────────────
HOSPITALES = [
    (101, "HGZ 4 Guadalupe", "MTY"),
    (102, "HGZ 17 Monterrey", "MTY"),
    (103, "HGZ 33 Monterrey", "MTY"),
    (104, "HGZ 67 Apodaca", "MTY"),
    (105, "HGZMF 6 San Nicolás", "MTY"),
    (106, "HGZ La Raza", "CDMX"),
]

# (id, nombre, código, uom, uom_id, contenido_por_envase, densidad g/mL, costo, categoría, es_volatil)
PRODUCTOS = [
    (1001, "Sevoflurano 250 mL frasco", "ANE-SEV-250", "mL", 11, 250, 1.52, 4.90, "Anestésicos volátiles", True),
    (1002, "Desflurano 240 mL frasco", "ANE-DES-240", "mL", 11, 240, 1.47, 7.80, "Anestésicos volátiles", True),
    (1003, "Isoflurano 100 mL frasco", "ANE-ISO-100", "mL", 11, 100, 1.50, 3.10, "Anestésicos volátiles", True),
    (1004, "Propofol 200 mg/20 mL ampolleta", "ANE-PRO-20", "pz", 1, 1, 0, 38.0, "Anestésicos IV", False),
    (1005, "Fentanilo 0.5 mg/10 mL ampolleta", "ANE-FEN-10", "pz", 1, 1, 0, 22.0, "Opioides", False),
    (1006, "Rocuronio 50 mg/5 mL", "ANE-ROC-5", "pz", 1, 1, 0, 95.0, "Relajantes", False),
    (1007, "Midazolam 15 mg/3 mL", "ANE-MID-3", "pz", 1, 1, 0, 14.5, "Benzodiacepinas", False),
    (1008, "Lidocaína 2% 50 mL", "ANE-LID-50", "pz", 1, 1, 0, 26.0, "Anestésicos locales", False),
    (1009, "Circuito de anestesia adulto", "INS-CIR-AD", "pz", 1, 1, 0, 185.0, "Insumos", False),
    (1010, "Cal sodada 5 kg", "INS-CAL-5", "kg", 12, 5, 0, 62.0, "Insumos", False),
    (1011, "Mascarilla laríngea #4", "INS-MLA-4", "pz", 1, 1, 0, 210.0, "Vía aérea", False),
    (1012, "Tubo endotraqueal 7.5", "INS-TET-75", "pz", 1, 1, 0, 32.0, "Vía aérea", False),
    (1013, "Guantes quirúrgicos 7.5", "INS-GUA-75", "par", 13, 1, 0, 9.5, "Insumos", False),
    (1014, "Jeringa 10 mL", "INS-JER-10", "pz", 1, 1, 0, 3.2, "Insumos", False),
    (1015, "Cánula de Guedel #4", "INS-GUE-4", "pz", 1, 1, 0, 11.0, "Vía aérea", False),
    (1016, "Bloqueador bronquial", "INS-BLQ-1", "pz", 1, 1, 0, 1450.0, "Vía aérea", False),
]
UOMS = {1: "Unidades", 11: "mL", 12: "kg", 13: "par", 14: "Frasco 250 mL", 15: "Frasco 240 mL", 16: "Frasco 100 mL",
        17: "Caja 100 pz", 18: "Caja 50 pares"}
# factor relativo a la unidad de referencia (como en uom.uom): 1 frasco 250 mL = 250 mL → factor 1/250
UOM_FACTOR = {1: 1.0, 11: 1.0, 12: 1.0, 13: 1.0, 14: 1 / 250, 15: 1 / 240, 16: 1 / 100, 17: 1 / 100, 18: 1 / 50}
UOM_COMPRA = {1001: 14, 1002: 15, 1003: 16, 1014: 17, 1013: 18}   # producto → unidad de compra
# precisión (rounding de uom.uom): mL y kg admiten decimales; piezas, pares, frascos y cajas son enteros
UOM_ROUNDING = {1: 1.0, 11: 0.1, 12: 0.01, 13: 1.0, 14: 1.0, 15: 1.0, 16: 1.0, 17: 1.0, 18: 1.0}

MEDICOS = [
    (201, "Dra. Alejandra Treviño"), (202, "Dr. Luis Garza"), (203, "Dra. Mariana Elizondo"),
    (204, "Dr. Rodrigo Cantú"), (205, "Dra. Fernanda Salinas"), (206, "Dr. Héctor Villarreal"),
    (207, "Dra. Paola Montemayor"), (208, "Dr. Andrés Quiroga"), (209, "Dra. Sofía Lozano"),
    (210, "Dr. Emilio Zambrano"), (211, "Dra. Regina Ochoa"), (212, "Dr. Iván Sepúlveda"),
]

AUXILIARES = [  # (id, nombre, hospital_id)
    (301, "Ana Karen Ledezma", 101), (302, "Jorge Peña", 101), (303, "Roberto Cadena", 102),
    (304, "Lucía Ramos", 102), (305, "Daniel Ortiz", 103), (306, "Brenda Solís", 103),
    (307, "Carlos Ibarra", 104), (308, "Valeria Nava", 104), (309, "Miguel Ángel Ruiz", 105),
    (310, "Diana Cavazos", 105), (311, "Sergio Alanís", 106), (312, "Paty Rangel", 106),
    (313, "Óscar Medina", 106), (314, "Gabriela Torres", 106), (315, "Ricardo Flores", 106),
]

CIRUGIAS = [
    ("Colecistectomía laparoscópica", 90, 150), ("Apendicectomía", 60, 110),
    ("Hernioplastia inguinal", 60, 120), ("Cesárea", 45, 90), ("Artroscopia de rodilla", 70, 130),
    ("Histerectomía", 120, 200), ("Prostatectomía", 150, 240), ("Fractura de cadera", 120, 210),
    ("Amigdalectomía", 30, 60), ("Cirugía de columna", 180, 320), ("Tiroidectomía", 90, 160),
    ("Safenectomía", 60, 100),
]

# Ubicaciones internas (id, nombre completo, almacén_id, almacén, hospital_id)
UBICACIONES = [
    (501, "CEDIS-MTY/Stock", 1, "CEDIS Monterrey", None),
    (502, "HGZ4/Stock", 2, "HGZ 4 Guadalupe", 101),
    (503, "HGZ17/Stock", 3, "HGZ 17 Monterrey", 102),
    (504, "HGZ33/Stock", 4, "HGZ 33 Monterrey", 103),
    (505, "HGZ67/Stock", 5, "HGZ 67 Apodaca", 104),
    (506, "HGZMF6/Stock", 6, "HGZMF 6 San Nicolás", 105),
    (507, "CEDIS-LR/Stock", 7, "CEDIS La Raza", None),
    (508, "LR-QX1/Stock", 8, "La Raza Quirófano 1", 106),
    (509, "LR-QX2/Stock", 9, "La Raza Quirófano 2", 106),
    (510, "LR-UCI/Stock", 10, "La Raza UCI", 106),
    (511, "LR-URG/Stock", 11, "La Raza Urgencias", 106),
]
UBICACION_CLIENTE = (600, "Partners/Customers")


def _ubic_de_hospital(hid: int, rnd: random.Random) -> tuple[int, str, int, str]:
    if hid == 106:
        u = rnd.choice(UBICACIONES[7:11])
    else:
        u = next(x for x in UBICACIONES if x[4] == hid)
    return u[0], u[1], u[2], u[3]


class OdooSimulado:
    """Imita la interfaz de OdooClient sobre datos generados en memoria."""

    def __init__(self, semilla: int = 20260912, meses: int = 18) -> None:
        self.rnd = random.Random(semilla)
        self.uid = 2
        self.db = "cbh-demo"
        self.url = "https://demo.cbh.local"
        self.user = "demo@i-condor.com"
        self.api_key = "demo"
        self.hoy = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.tablas: dict[str, list[dict]] = {}
        self._seq = 900000
        self.mensajes: list[dict] = []
        self._construir(meses)

    # ── generación de datos ─────────────────────────────────────────────────
    def _id(self) -> int:
        self._seq += 1
        return self._seq

    def _construir(self, meses: int) -> None:
        r = self.rnd
        t = self.tablas
        t["res.company"] = [{"id": 1, "name": "Biohealth"}, {"id": 2, "name": "CBH+"}]
        t["res.partner"] = (
            [{"id": h[0], "name": h[1], "is_medical_unit": True, "is_doctor": False, "city": h[2]}
             for h in HOSPITALES]
            + [{"id": m[0], "name": m[1], "is_medical_unit": False, "is_doctor": True,
                "x_studio_unidad_medica": [HOSPITALES[i % 6][0], HOSPITALES[i % 6][1]]}
               for i, m in enumerate(MEDICOS)]
            + [{"id": 701, "name": "Baxter México", "is_medical_unit": False, "is_doctor": False, "is_company": True, "supplier_rank": 1, "comment": ""},
               {"id": 702, "name": "AbbVie Farmacéuticos", "is_medical_unit": False, "is_doctor": False, "is_company": True, "supplier_rank": 1, "comment": ""},
               {"id": 703, "name": "Medix Distribuciones", "is_medical_unit": False, "is_doctor": False, "is_company": True, "supplier_rank": 1, "comment": ""}]
        )
        t["hr.employee"] = [{"id": a[0], "name": a[1],
                             "x_studio_unidad_medica": [a[2], dict((h[0], h[1]) for h in HOSPITALES)[a[2]]],
                             "department_id": [5, "Operaciones"]} for a in AUXILIARES]
        t["uom.uom"] = [{"id": k, "name": v, "factor": UOM_FACTOR[k], "uom_type": "reference" if UOM_FACTOR[k] == 1 else "bigger",
                         "rounding": UOM_ROUNDING.get(k, 1.0)}
                        for k, v in UOMS.items()]
        t["product.product"] = [{
            "id": p[0], "name": p[1], "display_name": f"[{p[2]}] {p[1]}", "default_code": p[2],
            "uom_id": [p[4], p[3]], "categ_id": [len(p[8]), p[8]], "type": "product",
            "detailed_type": "product", "tracking": "lot" if p[9] or p[7] > 20 else "none",
            "standard_price": p[7], "list_price": round(p[7] * 1.35, 2), "active": True,
            "qty_available": 0.0, "uom_po_id": [UOM_COMPRA.get(p[0], p[4]), UOMS[UOM_COMPRA.get(p[0], p[4])]],
            "product_tmpl_id": [p[0], p[1]],
            "x_studio_densidad": p[6] if p[9] else False, "x_studio_contenido_ml": p[5] if p[9] else False,
        } for p in PRODUCTOS]
        t["stock.warehouse"] = [{"id": u[2], "name": u[3], "code": u[1].split("/")[0][:5],
                                 "lot_stock_id": [u[0], u[1]], "company_id": [2, "CBH+"]}
                                for u in UBICACIONES]
        t["stock.location"] = [{"id": u[0], "name": u[1].split("/")[-1], "complete_name": u[1],
                                "usage": "internal", "warehouse_id": [u[2], u[3]],
                                "location_id": [u[2] * 10, u[1].split("/")[0]]} for u in UBICACIONES] + [
            {"id": 600, "name": "Customers", "complete_name": "Partners/Customers", "usage": "customer",
             "warehouse_id": False, "location_id": False},
            {"id": 601, "name": "Production", "complete_name": "Virtual Locations/Production",
             "usage": "production", "warehouse_id": False, "location_id": False},
        ]
        t["stock.picking.type"] = [
            {"id": 21, "name": "Transferencias internas", "code": "internal",
             "warehouse_id": [1, "CEDIS Monterrey"], "default_location_src_id": [501, "CEDIS-MTY/Stock"],
             "default_location_dest_id": [501, "CEDIS-MTY/Stock"], "company_id": [2, "CBH+"]},
            {"id": 22, "name": "Recepciones", "code": "incoming", "warehouse_id": [1, "CEDIS Monterrey"],
             "default_location_src_id": [610, "Partners/Vendors"],
             "default_location_dest_id": [501, "CEDIS-MTY/Stock"], "company_id": [2, "CBH+"]},
            {"id": 23, "name": "Transferencias internas LR", "code": "internal",
             "warehouse_id": [7, "CEDIS La Raza"], "default_location_src_id": [507, "CEDIS-LR/Stock"],
             "default_location_dest_id": [507, "CEDIS-LR/Stock"], "company_id": [2, "CBH+"]},
        ]
        t["product.supplierinfo"] = []
        for p in PRODUCTOS:
            prov = r.choice([701, 702, 703])
            t["product.supplierinfo"].append({
                "id": self._id(), "product_id": [p[0], p[1]], "product_tmpl_id": [p[0], p[1]],
                "partner_id": [prov, next(x["name"] for x in t["res.partner"] if x["id"] == prov)],
                "delay": r.choice([3, 5, 7, 10, 14]), "price": p[7] * 0.92, "min_qty": 1, "sequence": 1,
            })
            # proveedor secundario más rápido pero que NO es el principal (no debe usarse como lead time)
            t["product.supplierinfo"].append({
                "id": self._id(), "product_id": [p[0], p[1]], "product_tmpl_id": [p[0], p[1]],
                "partner_id": [703, "Medix Distribuciones"], "delay": 1, "price": p[7] * 1.3, "min_qty": 50, "sequence": 5,
            })

        # ── lotes ─────────────────────────────────────────────────────────
        t["stock.lot"] = []
        lotes_por_prod: dict[int, list[dict]] = {}
        for p in PRODUCTOS:
            if p[9] or p[7] > 20:
                for k in range(6):
                    lid = self._id()
                    cad = self.hoy + timedelta(days=r.randint(-30, 540))
                    lot = {"id": lid, "name": f"L{p[2][-3:]}-{2025 + k // 3}{r.randint(100, 999)}",
                           "product_id": [p[0], p[1]], "expiration_date": cad.strftime("%Y-%m-%d %H:%M:%S")}
                    t["stock.lot"].append(lot)
                    lotes_por_prod.setdefault(p[0], []).append(lot)

        # ── folios y líneas de consumo ────────────────────────────────────
        folios: list[dict] = []
        lineas: list[dict] = []
        inicio = self.hoy - timedelta(days=30 * meses)
        dia = inicio
        prod_por_id = {p[0]: p for p in PRODUCTOS}
        med_por_hosp: dict[int, list] = {}
        for i, m in enumerate(MEDICOS):
            med_por_hosp.setdefault(HOSPITALES[i % 6][0], []).append(m)
        aux_por_hosp: dict[int, list] = {}
        for a in AUXILIARES:
            aux_por_hosp.setdefault(a[2], []).append(a)

        # Cada hospital recibe un subconjunto de lotes (2 de 6) por producto, como en la vida real
        self.lotes_hosp: dict[tuple[int, int], list[dict]] = {}
        for h in HOSPITALES:
            for pid, lts in lotes_por_prod.items():
                k = (h[0] // 100) % 6  # desplazamiento por hospital
                self.lotes_hosp[(h[0], pid)] = [lts[(h[0] + i) % len(lts)] for i in range(2)]
        nfolio = 0
        # hasta ayer: las lecturas cortan en la hora actual y los folios de «hoy» entrarían o no según la hora del día
        while dia < self.hoy.replace(hour=0):
            dow = dia.weekday()
            # Tendencia (+12 % anual) y estacionalidad (más cirugías en invierno)
            meses_transc = (dia - inicio).days / 30.0
            factor_t = 1 + 0.01 * meses_transc
            factor_s = 1 + 0.18 * math.cos((dia.timetuple().tm_yday - 15) / 365 * 2 * math.pi)
            for h in HOSPITALES:
                base = {101: 4, 102: 5, 103: 4, 104: 3, 105: 3, 106: 9}[h[0]]
                if dow >= 5:
                    base = max(1, base // 3)
                n = max(0, int(round(r.gauss(base * factor_t * factor_s, 1.0))))
                for _ in range(n):
                    nfolio += 1
                    med = r.choice(med_por_hosp[h[0]])
                    aux = r.choice(aux_por_hosp[h[0]])
                    cir = r.choice(CIRUGIAS)
                    dur = max(20, int(r.gauss((cir[1] + cir[2]) / 2, (cir[2] - cir[1]) / 4)))
                    hora = dia.replace(hour=r.choice([7, 8, 9, 10, 11, 12, 13, 14, 15, 16]),
                                       minute=r.choice([0, 15, 30, 45]))
                    uid_, uname, wid, wname = _ubic_de_hospital(h[0], r)
                    fid = self._id()
                    folio = {
                        "id": fid, "name": f"FOL-{hora.strftime('%y%m')}-{nfolio:05d}",
                        "fecha": hora.strftime("%Y-%m-%d %H:%M:%S"), "state": "done",
                        "hospital_id": [h[0], h[1]], "medico_id": [med[0], med[1]],
                        "employee_id": [aux[0], aux[1]], "paciente": f"PAC-{r.randint(100000, 999999)}",
                        "tipo_cirugia": cir[0], "duracion_min": dur,
                        "hora_inicio": hora.strftime("%Y-%m-%d %H:%M:%S"),
                        "hora_fin": (hora + timedelta(minutes=dur)).strftime("%Y-%m-%d %H:%M:%S"),
                        "warehouse_id": [wid, wname], "quirofano": f"QX-{r.randint(1, 6)}",
                        "company_id": [2, "CBH+"],
                    }
                    folios.append(folio)
                    self._lineas_folio(folio, dur, uid_, uname, aux, h, prod_por_id, lotes_por_prod, lineas)
            dia += timedelta(days=1)

        self._sembrar_anomalias(folios, lineas, prod_por_id, lotes_por_prod, aux_por_hosp, med_por_hosp)
        t["cbh.operacion.medica"] = folios
        t["cbh.operacion.medica.line"] = lineas
        self._construir_existencias(lotes_por_prod)
        self._construir_movimientos(lineas)
        t["stock.picking"], t["stock.move"] = t.get("stock.picking", []), t.get("stock.move", [])
        t["stock.warehouse.orderpoint"], t["purchase.order"], t["purchase.order.line"] = [], [], []
        self._construir_programados(folios, med_por_hosp, aux_por_hosp)
        self._construir_pendientes()
        t["mail.activity"], t["mail.activity.type"] = [], [{"id": 4, "name": "Por hacer"}]
        t["helpdesk.ticket"], t["helpdesk.team"] = [], [{"id": 1, "name": "Soporte Odoo"}, {"id": 2, "name": "Operaciones"},
                                                       {"id": 3, "name": "Calidad"}, {"id": 4, "name": "Facturación"}]
        t["stock.scrap"] = []
        t["stock.location"].append({"id": 650, "name": "Cuarentena", "complete_name": "CEDIS-MTY/Cuarentena", "usage": "internal",
                                    "warehouse_id": [1, "CEDIS Monterrey"], "location_id": [10, "CEDIS-MTY"]})
        # usuarios y grupos (para los avisos a personas): contabilidad, inventario y administradores de Agentes de IA
        t["res.groups"] = [{"id": 31, "name": "Facturación / Contabilidad", "full_name": "Contabilidad / Facturación"},
                           {"id": 32, "name": "Administrador", "full_name": "Inventario / Administrador"},
                           {"id": 33, "name": "Administrador", "full_name": "Agentes de IA / Administrador"},
                           {"id": 34, "name": "Operación", "full_name": "Agentes de IA / Operación"}]
        t["ir.model.data"] = [{"id": 1, "module": "account", "name": "group_account_manager", "model": "res.groups", "res_id": 31},
                              {"id": 2, "module": "stock", "name": "group_stock_manager", "model": "res.groups", "res_id": 32},
                              {"id": 3, "module": "cbh_agentes_ia", "name": "group_admin", "model": "res.groups", "res_id": 33},
                              {"id": 4, "module": "cbh_agentes_ia", "name": "group_operacion", "model": "res.groups", "res_id": 34}]
        t["res.partner"] += [{"id": 801, "name": "Laura Contreras", "email": "lcontreras@cbh.mx"},
                             {"id": 802, "name": "Roberto Salas", "email": "rsalas@cbh.mx"},
                             {"id": 803, "name": "Ana Villaseñor", "email": "avillasenor@cbh.mx"},
                             {"id": 804, "name": "Agente IA", "email": "agente.ia@i-condor.com"}]
        t["res.users"] = [{"id": 11, "name": "Laura Contreras", "login": "lcontreras@cbh.mx", "partner_id": [801, "Laura Contreras"],
                           "email": "lcontreras@cbh.mx", "active": True, "share": False, "group_ids": [31, 34]},
                          {"id": 12, "name": "Roberto Salas", "login": "rsalas@cbh.mx", "partner_id": [802, "Roberto Salas"],
                           "email": "rsalas@cbh.mx", "active": True, "share": False, "group_ids": [32, 33]},
                          {"id": 13, "name": "Ana Villaseñor", "login": "avillasenor@cbh.mx", "partner_id": [803, "Ana Villaseñor"],
                           "email": "avillasenor@cbh.mx", "active": True, "share": False, "group_ids": [33]},
                          {"id": 14, "name": "Agente IA", "login": "agente.ia@i-condor.com", "partner_id": [804, "Agente IA"],
                           "email": "agente.ia@i-condor.com", "active": True, "share": False, "group_ids": [33]}]
        t["product.template"] = [{"id": p["product_tmpl_id"][0], "name": p["name"]} for p in t["product.product"]]
        t["mail.mail"], t["ir.attachment"] = [], []
        # facturación al IMSS (una factura mensual por hospital con las líneas de consumo del mes) para el análisis contable
        t["account.move"], t["account.move.line"] = [], []
        por_mes_hosp: dict = {}
        for l in lineas:
            k = (l["fecha"][:7], l["hospital_id"][0])
            por_mes_hosp.setdefault(k, []).append(l)
        for (mes, hid), ls in sorted(por_mes_hosp.items()):
            hname = next(h[1] for h in HOSPITALES if h[0] == hid)
            fid = self._id()
            total = round(sum(float(x.get("price_subtotal") or 0) for x in ls), 2)
            fecha = f"{mes}-28"
            vencida = fecha < (self.hoy - timedelta(days=60)).strftime("%Y-%m-%d")
            pagada = fecha < (self.hoy - timedelta(days=90)).strftime("%Y-%m-%d")
            t["account.move"].append({"id": fid, "name": f"INV/{mes.replace('-', '/')}/{hid:04d}", "partner_id": [hid, hname], "invoice_date": fecha,
                                      "invoice_date_due": f"{mes}-28", "amount_untaxed": total, "amount_total": round(total * 1.16, 2),
                                      "amount_residual": 0.0 if pagada else round(total * 1.16, 2), "state": "posted",
                                      "payment_state": "paid" if pagada else "not_paid", "move_type": "out_invoice",
                                      "currency_id": [33, "MXN"], "invoice_origin": "", "ref": "", "company_id": [2, "CBH+"], "vencida_sim": vencida})
            por_prod: dict = {}
            for x in ls:
                por_prod.setdefault(x["product_id"][0], [x["product_id"][1], 0.0, 0.0])
                por_prod[x["product_id"][0]][1] += float(x["cantidad"]); por_prod[x["product_id"][0]][2] += float(x.get("price_subtotal") or 0)
            for pid, (pname, qty, imp) in por_prod.items():
                t["account.move.line"].append({"id": self._id(), "move_id": [fid, t["account.move"][-1]["name"]], "partner_id": [hid, hname],
                                               "product_id": [pid, pname], "quantity": round(qty, 2), "price_unit": round(imp / qty, 4) if qty else 0.0,
                                               "price_subtotal": round(imp, 2), "date": fecha, "display_type": "product",
                                               "product_uom_id": [prod_por_id[pid][4], prod_por_id[pid][3]], "name": pname,
                                               "move_type": "out_invoice"})
        t["ir.model"] = [{"id": i + 1, "model": m, "name": m, "modules": "cbh"} for i, m in enumerate(t)]

    def _lineas_folio(self, folio, dur, uid_, uname, aux, h, prod_por_id, lotes_por_prod, lineas,
                      factor_volatil: float = 1.0, ruido_bascula: float = 0.4) -> None:
        r = self.rnd
        # Anestésico volátil principal: consumo ~ 0.18 mL/min (± 25 %)
        pid = r.choices([1001, 1002, 1003], weights=[0.72, 0.18, 0.10])[0]
        p = prod_por_id[pid]
        ml = max(3.0, r.gauss(0.18 * dur, 0.045 * dur)) * factor_volatil
        ml = round(ml, 1)
        lote = r.choice(self.lotes_hosp.get((h[0], pid)) or lotes_por_prod[pid])
        peso_ini = round(r.uniform(80, p[5] * p[6]), 1)
        consumo_g = ml * p[6]
        peso_fin = round(peso_ini - consumo_g + r.gauss(0, ruido_bascula), 1)
        lineas.append(self._linea(folio, p, ml, lote, uid_, uname, aux, h, peso_ini, peso_fin, dur))
        # Insumos acompañantes
        for pid2, prob, qmin, qmax in [(1004, 0.95, 1, 3), (1005, 0.85, 1, 3), (1006, 0.6, 1, 2),
                                       (1007, 0.5, 1, 1), (1008, 0.3, 1, 1), (1009, 0.9, 1, 1),
                                       (1011, 0.35, 1, 1), (1012, 0.55, 1, 1), (1013, 0.98, 2, 4),
                                       (1014, 0.95, 2, 6), (1015, 0.4, 1, 1), (1016, 0.03, 1, 1),
                                       (1010, 0.08, 1, 1)]:
            if r.random() < prob:
                p2 = prod_por_id[pid2]
                q = r.randint(qmin, qmax)
                lote2 = (r.choice(self.lotes_hosp.get((h[0], pid2)) or lotes_por_prod[pid2])
                         if pid2 in lotes_por_prod else None)
                lineas.append(self._linea(folio, p2, q, lote2, uid_, uname, aux, h, None, None, dur))

    def _linea(self, folio, p, cant, lote, uid_, uname, aux, h, peso_ini, peso_fin, dur) -> dict:
        return {
            "id": self._id(), "operacion_id": [folio["id"], folio["name"]], "fecha": folio["fecha"],
            "product_id": [p[0], p[1]], "cantidad": float(cant), "product_uom_id": [p[4], p[3]],
            "lot_id": [lote["id"], lote["name"]] if lote else False,
            "expiration_date": lote["expiration_date"] if lote else False,
            "location_id": [uid_, uname], "location_dest_id": [600, "Partners/Customers"],
            "hospital_id": folio["hospital_id"], "medico_id": folio["medico_id"],
            "auxiliar_id": [aux[0], aux[1]], "subalmacen_id": [uid_, uname],
            "price_subtotal": round(float(cant) * p[7], 2),
            "peso_inicial": peso_ini, "peso_final": peso_fin,
            "consumo_peso": round(peso_ini - peso_fin, 1) if peso_ini is not None else False,
            "duracion_min": dur, "company_id": [2, "CBH+"],
        }

    def _sembrar_anomalias(self, folios, lineas, prod_por_id, lotes_por_prod, aux_por_hosp, med_por_hosp):
        """Anomalías con respuesta conocida (ver tests)."""
        r = self.rnd
        h17 = HOSPITALES[1]
        h67 = HOSPITALES[3]
        sev = prod_por_id[1001]
        des = prod_por_id[1002]
        self.anomalias_sembradas: dict[str, list[int]] = {}

        # A1 · Roberto Cadena (303): +45 % de sevoflurano los últimos 60 días (merma sistemática)
        ids = []
        for l in lineas:
            if (l["auxiliar_id"][0] == 303 and l["product_id"][0] == 1001
                    and datetime.strptime(l["fecha"], "%Y-%m-%d %H:%M:%S") > self.hoy - timedelta(days=60)):
                l["cantidad"] = round(l["cantidad"] * 1.45, 1)
                # La báscula sí registra lo que salió del frasco: el sobreconsumo es real
                l["peso_final"] = round(l["peso_inicial"] - l["cantidad"] * sev[6] + r.gauss(0, 0.4), 1)
                l["consumo_peso"] = round(l["peso_inicial"] - l["peso_final"], 1)
                l["price_subtotal"] = round(l["cantidad"] * sev[7], 2)
                ids.append(l["id"])
        self.anomalias_sembradas["sobreconsumo_auxiliar_303"] = ids

        # A2 · Consumos nocturnos / fin de semana sin cirugía (folio sin médico ni paciente) — mismo auxiliar
        ids = []
        for k in range(9):
            f = self.hoy - timedelta(days=r.randint(2, 55))
            f = f.replace(hour=r.choice([1, 2, 3, 22, 23]), minute=r.choice([5, 20, 40]))
            if k % 3 == 0:  # forzar fin de semana en algunos (hacia atrás, nunca al futuro)
                f = f - timedelta(days=(f.weekday() - 5) % 7)
            fid = self._id()
            folio = {"id": fid, "name": f"FOL-{f.strftime('%y%m')}-N{k:03d}", "fecha": f.strftime("%Y-%m-%d %H:%M:%S"),
                     "state": "done", "hospital_id": [h17[0], h17[1]], "medico_id": False,
                     "employee_id": [303, "Roberto Cadena"], "paciente": False, "tipo_cirugia": False,
                     "duracion_min": 0, "hora_inicio": False, "hora_fin": False,
                     "warehouse_id": [3, "HGZ 17 Monterrey"], "quirofano": False, "company_id": [2, "CBH+"]}
            folios.append(folio)
            ml = round(r.uniform(40, 90), 1)
            lote = r.choice(self.lotes_hosp[(102, 1001)])
            l = self._linea(folio, sev, ml, lote, 503, "HGZ17/Stock", (303, "Roberto Cadena"), h17, None, None, 0)
            l["medico_id"] = False
            lineas.append(l)
            ids.append(l["id"])
        self.anomalias_sembradas["consumo_sin_cirugia"] = ids

        # A3 · Báscula HGZ 67: 4 lecturas imposibles (peso_final > peso_inicial) y 10 discrepancias > 15 %
        cand = [l for l in lineas if l["hospital_id"][0] == 104 and l["product_id"][0] in (1001, 1002)
                and datetime.strptime(l["fecha"], "%Y-%m-%d %H:%M:%S") > self.hoy - timedelta(days=45)]
        r.shuffle(cand)
        ids_imp, ids_disc = [], []
        for l in cand[:4]:
            l["peso_final"] = round(l["peso_inicial"] + r.uniform(3, 12), 1)
            l["consumo_peso"] = round(l["peso_inicial"] - l["peso_final"], 1)
            ids_imp.append(l["id"])
        for l in cand[4:14]:
            dens = prod_por_id[l["product_id"][0]][6]
            # la báscula dice que salió mucho más de lo capturado
            real_g = l["cantidad"] * dens * r.uniform(1.35, 1.9)
            l["peso_final"] = round(l["peso_inicial"] - real_g, 1)
            l["consumo_peso"] = round(l["peso_inicial"] - l["peso_final"], 1)
            ids_disc.append(l["id"])
        self.anomalias_sembradas["bascula_imposible"] = ids_imp
        self.anomalias_sembradas["bascula_discrepancia"] = ids_disc

        # A4 · Lote viajero: mismo lote de sevoflurano en HGZ 4 y HGZ 33 el mismo día (3 días)
        ids = []
        lote_v = self.lotes_hosp[(101, 1001)][0]   # lote que solo debería estar en HGZ 4
        for d in (3, 9, 16):
            f = self.hoy - timedelta(days=d)
            for hid, uid_, uname, aux in ((101, 502, "HGZ4/Stock", (301, "Ana Karen Ledezma")),
                                          (103, 504, "HGZ33/Stock", (305, "Daniel Ortiz"))):
                h = next(x for x in HOSPITALES if x[0] == hid)
                med = r.choice(med_por_hosp[hid])
                fid = self._id()
                folio = {"id": fid, "name": f"FOL-{f.strftime('%y%m')}-V{d:02d}{hid}", "fecha": f.replace(hour=10).strftime("%Y-%m-%d %H:%M:%S"),
                         "state": "done", "hospital_id": [hid, h[1]], "medico_id": [med[0], med[1]],
                         "employee_id": [aux[0], aux[1]], "paciente": f"PAC-{r.randint(100000, 999999)}",
                         "tipo_cirugia": "Apendicectomía", "duracion_min": 80, "hora_inicio": False, "hora_fin": False,
                         "warehouse_id": [uid_ - 500, h[1]], "quirofano": "QX-1", "company_id": [2, "CBH+"]}
                folios.append(folio)
                l = self._linea(folio, sev, 14.0, lote_v, uid_, uname, aux, h, 300.0, 300.0 - 14 * sev[6], 80)
                lineas.append(l)
                ids.append(l["id"])
        self.anomalias_sembradas["lote_viajero"] = ids

        # A5 · Duplicados exactos (5)
        ids = []
        recientes = [l for l in lineas if l["product_id"][0] == 1001
                     and datetime.strptime(l["fecha"], "%Y-%m-%d %H:%M:%S") > self.hoy - timedelta(days=30)]
        originales_dup = []
        for l in r.sample(recientes, 5):
            dup = dict(l)
            dup["id"] = self._id()
            lineas.append(dup)
            ids.append(dup["id"])
            originales_dup.append(l["id"])
        self.anomalias_sembradas["duplicado"] = ids

        # A6 · Consumo mayor al contenido del frasco (3)
        ids = []
        for l in r.sample([x for x in recientes if x["id"] not in ids], 3):
            l["cantidad"] = round(r.uniform(280, 340), 1)
            l["price_subtotal"] = round(l["cantidad"] * sev[7], 2)
            ids.append(l["id"])
        self.anomalias_sembradas["excede_envase"] = ids

        # A7 · Lote caducado usado (3)
        lote_cad = {"id": self._id(), "name": "LSEV-CAD01", "product_id": [1001, sev[1]],
                    "expiration_date": (self.hoy - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")}
        self.tablas["stock.lot"].append(lote_cad)
        ids = []
        for l in r.sample([x for x in recientes if x["cantidad"] < 200], 3):
            l["lot_id"] = [lote_cad["id"], lote_cad["name"]]
            l["expiration_date"] = lote_cad["expiration_date"]
            ids.append(l["id"])
        self.anomalias_sembradas["lote_caducado"] = ids

        # A8 · Cambio de nivel: Desflurano en LR-QX2 se duplica los últimos 45 días
        ids = []
        for l in lineas:
            if (l["subalmacen_id"][0] == 509 and l["product_id"][0] == 1002
                    and datetime.strptime(l["fecha"], "%Y-%m-%d %H:%M:%S") > self.hoy - timedelta(days=45)):
                l["cantidad"] = round(l["cantidad"] * 2.0, 1)
                l["peso_final"] = round(l["peso_inicial"] - l["cantidad"] * des[6] + r.gauss(0, 0.4), 1)
                l["consumo_peso"] = round(l["peso_inicial"] - l["peso_final"], 1)
                l["price_subtotal"] = round(l["cantidad"] * des[7], 2)
                ids.append(l["id"])
        self.anomalias_sembradas["cambio_nivel_lrqx2_desflurano"] = ids

        # A9 · Tasa clínica imposible: 60 mL de sevoflurano en cirugías de 25 min (4 casos, Dr. 208)
        ids = []
        cortas = [l for l in lineas if l["product_id"][0] == 1001 and l["duracion_min"] and l["duracion_min"] <= 45
                  and datetime.strptime(l["fecha"], "%Y-%m-%d %H:%M:%S") > self.hoy - timedelta(days=40)]
        for l in r.sample(cortas, min(4, len(cortas))):
            l["cantidad"] = 60.0
            l["medico_id"] = [208, "Dr. Andrés Quiroga"]
            l["peso_final"] = round(l["peso_inicial"] - 60 * sev[6], 1)
            l["consumo_peso"] = round(l["peso_inicial"] - l["peso_final"], 1)
            ids.append(l["id"])
        self.anomalias_sembradas["tasa_clinica"] = ids

        # Los duplicados sembrados deben seguir siendo copias EXACTAS aunque otra siembra (A6–A9) haya modificado el
        # original después: se vuelven a copiar al final (sin alterar el orden del muestreo aleatorio).
        por_id = {l["id"]: l for l in lineas}
        for orig_id, dup_id in zip(originales_dup, self.anomalias_sembradas["duplicado"]):
            dup = por_id.get(dup_id)
            if dup is not None:
                dup.update({k: v for k, v in por_id[orig_id].items() if k != "id"})

    def _construir_programados(self, folios, med_por_hosp, aux_por_hosp):
        """Cirugías programadas para los próximos 7 días (folios en borrador con fecha futura)."""
        r = self.rnd
        self.folios_programados_ids = []
        for d in range(1, 8):
            dia = self.hoy + timedelta(days=d)
            if dia.weekday() >= 5:
                continue
            for h in HOSPITALES:
                n = {101: 4, 102: 5, 103: 4, 104: 3, 105: 3, 106: 9}[h[0]]
                if h[0] == 102:
                    n = 11  # HGZ 17 tiene una jornada quirúrgica extraordinaria la próxima semana
                for k in range(n):
                    med = r.choice(med_por_hosp[h[0]]); aux = r.choice(aux_por_hosp[h[0]])
                    cir = r.choice(CIRUGIAS)
                    fid = self._id()
                    folios.append({"id": fid, "name": f"FOL-PROG-{h[0]}-{d}{k:02d}", "fecha": dia.replace(hour=8 + k % 8).strftime("%Y-%m-%d %H:%M:%S"),
                                   "state": "draft", "hospital_id": [h[0], h[1]], "medico_id": [med[0], med[1]],
                                   "employee_id": [aux[0], aux[1]], "paciente": f"PAC-{r.randint(100000, 999999)}",
                                   "tipo_cirugia": cir[0], "duracion_min": (cir[1] + cir[2]) // 2, "hora_inicio": False, "hora_fin": False,
                                   "warehouse_id": [2, h[1]], "quirofano": f"QX-{k % 4 + 1}", "company_id": [2, "CBH+"]})
                    self.folios_programados_ids.append(fid)

    def _construir_pendientes(self):
        """Compras confirmadas por recibir (una retrasada) y transferencias internas pendientes."""
        r = self.rnd
        t = self.tablas
        ptype = t["stock.picking.type"]
        # Compra confirmada de sevoflurano por recibir en CEDIS-MTY dentro de 4 días (12 frascos = 3,000 mL)
        po1 = self._id()
        t["purchase.order"].append({"id": po1, "name": "P00341", "state": "purchase", "partner_id": [701, "Baxter México"],
                                    "picking_type_id": [22, "Recepciones"], "date_order": (self.hoy - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
                                    "date_planned": (self.hoy + timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S"), "origin": "Compra manual"})
        t["purchase.order.line"].append({"id": self._id(), "order_id": [po1, "P00341"], "product_id": [1001, "Sevoflurano 250 mL frasco"],
                                         "product_qty": 12.0, "qty_received": 0.0, "product_uom": [14, "Frasco 250 mL"],   # 12 frascos = 3,000 mL
                                         "date_planned": (self.hoy + timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S"), "state": "purchase",
                                         "create_date": (self.hoy - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")})
        # Recepción de esa compra ya creada en Odoo (stock.move desde Proveedores): NO debe contarse además de la compra
        rec = self._id()
        t["stock.picking"].append({"id": rec, "name": "CEDIS/IN/00210", "state": "assigned", "picking_type_id": [22, "Recepciones"],
                                   "location_id": [610, "Partners/Vendors"], "location_dest_id": [501, "CEDIS-MTY/Stock"],
                                   "scheduled_date": (self.hoy + timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S"), "origin": "P00341"})
        t["stock.move"].append({"id": self._id(), "picking_id": [rec, "CEDIS/IN/00210"], "product_id": [1001, "Sevoflurano 250 mL frasco"],
                                "product_uom_qty": 12.0, "product_qty": 3000.0, "quantity": 0.0, "product_uom": [14, "Frasco 250 mL"], "state": "assigned",
                                "location_id": [610, "Partners/Vendors"], "location_dest_id": [501, "CEDIS-MTY/Stock"],
                                "date": (self.hoy + timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S"), "reference": "CEDIS/IN/00210",
                                "location_dest_id.usage": "internal", "location_id.usage": "supplier"})
        # Compra de jeringas RETRASADA (debió llegar hace 5 días)
        po2 = self._id()
        t["purchase.order"].append({"id": po2, "name": "P00338", "state": "purchase", "partner_id": [703, "Medix Distribuciones"],
                                    "picking_type_id": [22, "Recepciones"], "date_order": (self.hoy - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S"),
                                    "date_planned": (self.hoy - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"), "origin": "Compra manual"})
        t["purchase.order.line"].append({"id": self._id(), "order_id": [po2, "P00338"], "product_id": [1014, "Jeringa 10 mL"],
                                         "product_qty": 20.0, "qty_received": 5.0, "product_uom": [17, "Caja 100 pz"],   # 15 cajas pendientes = 1,500 pz
                                         "date_planned": (self.hoy - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"), "state": "purchase",
                                         "create_date": (self.hoy - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")})
        # Transferencia interna pendiente CEDIS-MTY → HGZ17 de fentanilo (60 pz), llega mañana
        pk = self._id()
        t["stock.picking"].append({"id": pk, "name": "CEDIS/INT/00912", "state": "assigned", "picking_type_id": [21, "Transferencias internas"],
                                   "location_id": [501, "CEDIS-MTY/Stock"], "location_dest_id": [503, "HGZ17/Stock"],
                                   "scheduled_date": (self.hoy + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "origin": "Resurtido semanal"})
        t["stock.move"].append({"id": self._id(), "picking_id": [pk, "CEDIS/INT/00912"], "product_id": [1005, "Fentanilo 0.5 mg/10 mL ampolleta"],
                                "product_uom_qty": 60.0, "product_qty": 60.0, "product_uom": [1, "Unidades"], "quantity": 60.0, "state": "assigned", "location_id": [501, "CEDIS-MTY/Stock"],
                                "location_dest_id": [503, "HGZ17/Stock"], "date": (self.hoy + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                                "reference": "CEDIS/INT/00912", "location_dest_id.usage": "internal", "location_id.usage": "internal"})

    def _construir_existencias(self, lotes_por_prod):
        r = self.rnd
        quants = []
        for u in UBICACIONES:
            es_cedis = u[4] is None
            for p in PRODUCTOS:
                pid = p[0]
                # Perfil de inventario: CEDIS con mucho, hospitales con poco
                if es_cedis:
                    base = {1001: 2600, 1002: 900, 1003: 300}.get(pid, r.randint(60, 400))
                else:
                    base = {1001: 420, 1002: 120, 1003: 60}.get(pid, r.randint(4, 40))
                # Sembrar: desabasto de Fentanilo en HGZ 17; exceso muerto de Bloqueador en HGZMF 6
                if u[0] == 503 and pid == 1005:
                    base = 2
                if u[0] == 506 and pid == 1016:
                    base = 38
                if u[0] == 509 and pid == 1002:
                    base = 25  # LR-QX2 con desflurano bajo y consumo duplicado
                lotes = lotes_por_prod.get(pid)
                if lotes:
                    partes = max(1, min(3, len(lotes)))
                    for lot in r.sample(lotes, partes):
                        q = round(base / partes * r.uniform(0.7, 1.3), 1)
                        quants.append({"id": self._id(), "product_id": [pid, p[1]], "location_id": [u[0], u[1]],
                                       "warehouse_id": [u[2], u[3]], "quantity": q, "available_quantity": q,
                                       "reserved_quantity": 0.0, "lot_id": [lot["id"], lot["name"]],
                                       "expiration_date": lot["expiration_date"], "company_id": [2, "CBH+"],
                                       "location_id.usage": "internal"})
                else:
                    q = float(base)
                    quants.append({"id": self._id(), "product_id": [pid, p[1]], "location_id": [u[0], u[1]],
                                   "warehouse_id": [u[2], u[3]], "quantity": q, "available_quantity": q,
                                   "reserved_quantity": 0.0, "lot_id": False, "expiration_date": False,
                                   "company_id": [2, "CBH+"], "location_id.usage": "internal"})
        # reserva: 60 pz de fentanilo en CEDIS-MTY comprometidos en la transferencia pendiente
        for qv in quants:
            if qv["product_id"][0] == 1005 and qv["location_id"][0] == 501 and qv["quantity"] >= 60:
                qv["reserved_quantity"] = 60.0
                qv["available_quantity"] = qv["quantity"] - 60.0
                break
        self.tablas["stock.quant"] = quants

    def _construir_movimientos(self, lineas):
        """stock.move.line equivalentes, para probar el modo de respaldo."""
        mls = []
        for l in lineas:
            mls.append({"id": l["id"], "date": l["fecha"], "product_id": l["product_id"],
                        "qty_done": l["cantidad"], "quantity": l["cantidad"], "product_uom_id": l["product_uom_id"],
                        "lot_id": l["lot_id"], "location_id": l["location_id"], "location_dest_id": l["location_dest_id"],
                        "picking_id": l["operacion_id"], "move_id": False, "reference": l["operacion_id"][1],
                        "company_id": l["company_id"], "state": "done",
                        "location_id.usage": "internal", "location_dest_id.usage": "customer"})
        self.tablas["stock.move.line"] = mls

    # ── evaluación de dominios ──────────────────────────────────────────────
    _RELACION_POR_CAMPO = {"product_id": "product.product", "lot_id": "stock.lot", "location_id": "stock.location", "location_dest_id": "stock.location",
                           "partner_id": "res.partner", "picking_id": "stock.picking", "order_id": "purchase.order", "request_id": "cbh.medical.service.request",
                           "operacion_id": "cbh.operacion.medica", "warehouse_id": "stock.warehouse", "company_id": "res.company", "user_id": "res.users"}

    def _registro_relacionado(self, campo: str, rid: int, modelo_actual: str | None = None) -> dict | None:
        """Registro al que apunta un many2one (para dominios con ruta, p. ej. move_id.state o request_id.surgery_date)."""
        candidatos = []
        if campo == "move_id":
            candidatos = ["account.move", "stock.move"] if (modelo_actual or "").startswith("account.") else ["stock.move", "account.move"]
        elif campo in self._RELACION_POR_CAMPO:
            candidatos = [self._RELACION_POR_CAMPO[campo]]
        idx = getattr(self, "_indice", None)
        if idx is None:
            idx = self._indice = {}
        for tabla in candidatos or list(self.tablas):
            if tabla in ("ir.model",):
                continue
            t_idx = idx.get(tabla)
            if t_idx is None or len(t_idx) != len(self.tablas.get(tabla, [])):
                t_idx = idx[tabla] = {r["id"]: r for r in self.tablas.get(tabla, []) if "id" in r}
            r = t_idx.get(rid)
            if r is not None:
                return r
        return None

    def _valor(self, rec: dict, campo: str, modelo: str | None = None):
        if campo == "write_date" and campo not in rec:
            return self._write_date(rec)
        if campo in rec:
            v = rec[campo]
        elif "." in campo:
            base, _, resto = campo.partition(".")
            v = rec.get(base)
            if isinstance(v, (list, tuple)) and resto == "name":
                return v[1]
            if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], int):
                rel = self._registro_relacionado(base, v[0], modelo)
                return self._valor(rel, resto, None) if rel is not None else None
            return rec.get(campo)
        else:
            return None
        if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
            return v[0]          # many2one [id, nombre]
        return v

    def _cumple(self, rec: dict, cond, modelo: str | None = None) -> bool:
        if cond in ("&", "|", "!"):
            return True
        campo, op, val = cond
        v = self._valor(rec, campo, modelo)
        if isinstance(v, (list, tuple)) and op in ("in", "not in", "=", "!="):
            # many2many: coincide si algún id está en el valor buscado
            vals = list(val) if isinstance(val, (list, tuple)) else [val]
            hay = any(x in vals for x in v)
            return hay if op in ("in", "=") else not hay
        if op == "=":
            return v == val or (val is False and not v)
        if op == "!=":
            return v != val
        if op == "in":
            return v in (val or [])
        if op == "not in":
            return v not in (val or [])
        if v is None or v is False:
            return False
        if op == ">=":
            return v >= val
        if op == "<=":
            return v <= val
        if op == ">":
            return v > val
        if op == "<":
            return v < val
        if op in ("like", "ilike"):
            return str(val).lower() in str(v).lower()
        return True

    def _filtrar(self, modelo: str, dominio: list) -> list[dict]:
        rows = self.tablas.get(modelo, [])
        # Soporte simple de '|': se evalúa como OR de los dos siguientes términos
        conds = [c for c in dominio if isinstance(c, (list, tuple))]
        ors: list[tuple] = []
        i = 0
        plain: list = []
        while i < len(dominio):
            c = dominio[i]
            if c == "|" and i + 2 < len(dominio):
                ors.append((dominio[i + 1], dominio[i + 2]))
                i += 3
                continue
            if isinstance(c, (list, tuple)):
                plain.append(c)
            i += 1
        out = []
        for r_ in rows:
            if all(self._cumple(r_, c, modelo) for c in plain) and all(
                    self._cumple(r_, a, modelo) or self._cumple(r_, b, modelo) for a, b in ors):
                out.append(r_)
        return out

    # ── interfaz OdooClient ─────────────────────────────────────────────────
    def login(self, forzar: bool = False) -> int:
        return self.uid

    def version(self) -> dict:
        return {"server_version": "19.0-demo", "server_serie": "19.0"}

    def probar(self) -> dict:
        return {"ok": True, "version": "19.0 (simulado)", "uid": 2, "usuario": "Demo CBH",
                "companias": self.tablas["res.company"], "demo": True}

    def execute(self, modelo: str, metodo: str, args=None, **kw):
        args = args or []
        if metodo == "search_read":
            return self.search_read(modelo, args[0] if args else [], kw.get("fields"),
                                    kw.get("limit", 0), kw.get("order"), kw.get("offset", 0))
        if metodo == "search_count":
            return self.search_count(modelo, args[0] if args else [])
        if metodo == "search":
            return [r_["id"] for r_ in self.search_read(modelo, args[0] if args else [], ["id"], kw.get("limit", 0), kw.get("order"), kw.get("offset", 0))]
        if metodo == "read":
            ids = set(args[0]); campos = args[1] if len(args) > 1 else None
            return [self._proyectar(r_, campos) for r_ in self.tablas.get(modelo, []) if r_["id"] in ids]
        if metodo == "fields_get":
            return self.fields_get(modelo)
        if metodo == "create":
            return self.create(modelo, args[0])
        if metodo == "write":
            return self.write(modelo, args[0], args[1])
        if metodo == "unlink":
            return self.unlink(modelo, args[0])
        if metodo == "send" and modelo == "mail.mail":
            for r_ in self.tablas.get("mail.mail", []):
                if r_["id"] in args[0]:
                    r_["state"] = "sent"
            return True
        if metodo == "message_post":
            self.mensajes.append({"modelo": modelo, "id": args[0][0], "cuerpo": kw.get("body"), "partner_ids": list(kw.get("partner_ids") or [])})
            return len(self.mensajes)
        if metodo in ("action_confirm", "action_assign", "button_validate", "button_confirm",
                      "action_cancel", "action_done", "action_draft", "button_cancel"):
            if modelo == "stock.picking":
                return self._boton_picking(list(args[0]), metodo)
            estado = {"action_confirm": "confirmed", "action_assign": "assigned", "button_validate": "done",
                      "button_confirm": "purchase", "action_cancel": "cancel", "action_done": "done",
                      "action_draft": "draft", "button_cancel": "cancel"}[metodo]
            for r_ in self.tablas.get(modelo, []):
                if r_["id"] in args[0]:
                    r_["state"] = estado
            return True
        if metodo == "name_search":
            n = (kw.get("name") or "").lower()
            return [[r_["id"], r_.get("display_name") or r_.get("name")] for r_ in self.tablas.get(modelo, [])
                    if n in str(r_.get("display_name") or r_.get("name", "")).lower()][: kw.get("limit", 10)]
        raise NotImplementedError(f"simulado: {modelo}.{metodo}")

    # ── comportamiento realista de transferencias: confirmar, RESERVAR quants, validar (o pedir decisión), cancelar ──
    def _moves_de(self, ids: list[int]) -> list[dict]:
        return [m for m in self.tablas.get("stock.move", []) if isinstance(m.get("picking_id"), (list, tuple)) and m["picking_id"][0] in ids]

    def _quants(self, producto_id: int, ubicacion_id: int, lote_ids: list[int] | None = None) -> list[dict]:
        qs = [q for q in self.tablas.get("stock.quant", [])
              if q["product_id"][0] == producto_id and q["location_id"][0] == ubicacion_id
              and (not lote_ids or (q.get("lot_id") and q["lot_id"][0] in lote_ids))]
        return sorted(qs, key=lambda q: (q.get("expiration_date") or "9999"))

    def _boton_picking(self, ids: list[int], metodo: str):
        pickings = [p for p in self.tablas.get("stock.picking", []) if p["id"] in ids]
        moves = self._moves_de(ids)
        mls = self.tablas.setdefault("stock.move.line", [])
        if metodo in ("action_confirm",):
            for p in pickings:
                if p.get("state") == "draft":
                    p["state"] = "confirmed"
            for m in moves:
                if m.get("state", "draft") == "draft":
                    m["state"] = "confirmed"
            return True
        if metodo == "action_assign":
            for p in pickings:
                todos = True
                for m in [x for x in moves if x["picking_id"][0] == p["id"]]:
                    pedido = float(m.get("product_uom_qty") or 0)
                    ya = float(m.get("quantity") or 0)
                    faltante = pedido - ya
                    lotes = m.get("lot_ids") if isinstance(m.get("lot_ids"), list) else None
                    for q in self._quants(m["product_id"][0], m["location_id"][0], lotes):
                        if faltante <= 1e-9:
                            break
                        libre = float(q["quantity"]) - float(q.get("reserved_quantity") or 0)
                        toma = min(libre, faltante)
                        if toma <= 0:
                            continue
                        q["reserved_quantity"] = float(q.get("reserved_quantity") or 0) + toma
                        q["available_quantity"] = float(q["quantity"]) - q["reserved_quantity"]
                        mls.append({"id": self._id(), "move_id": [m["id"], m.get("name", "")], "picking_id": m["picking_id"],
                                    "product_id": m["product_id"], "lot_id": q.get("lot_id") or False, "quantity": toma,
                                    "reserved_uom_qty": toma, "location_id": m["location_id"], "location_dest_id": m["location_dest_id"]})
                        ya += toma
                        faltante -= toma
                    m["quantity"] = ya
                    m["state"] = "assigned" if ya + 1e-9 >= pedido else ("partially_available" if ya > 0 else "confirmed")
                    todos = todos and m["state"] == "assigned"
                p["state"] = "assigned" if todos and any(x["picking_id"][0] == p["id"] for x in moves) else "confirmed"
            return True
        if metodo == "button_validate":
            for p in pickings:
                mis = [x for x in moves if x["picking_id"][0] == p["id"]]
                if p.get("state") != "assigned" or any(float(m.get("quantity") or 0) <= 0 for m in mis):
                    return {"type": "ir.actions.act_window", "res_model": "stock.immediate.transfer", "name": "Transferencia inmediata"}
                if any(float(m.get("quantity") or 0) + 1e-9 < float(m.get("product_uom_qty") or 0) for m in mis):
                    return {"type": "ir.actions.act_window", "res_model": "stock.backorder.confirmation", "name": "Crear orden parcial"}
                for m in mis:
                    for ml in [x for x in mls if isinstance(x.get("move_id"), (list, tuple)) and x["move_id"][0] == m["id"]]:
                        q = float(ml["quantity"])
                        lote = [ml["lot_id"][0]] if ml.get("lot_id") else None
                        for qu in self._quants(m["product_id"][0], m["location_id"][0], lote):
                            if q <= 0:
                                break
                            toma = min(q, float(qu["quantity"]))
                            qu["quantity"] = float(qu["quantity"]) - toma
                            qu["reserved_quantity"] = max(0.0, float(qu.get("reserved_quantity") or 0) - toma)
                            qu["available_quantity"] = qu["quantity"] - qu["reserved_quantity"]
                            q -= toma
                        dest = [d for d in self.tablas.get("stock.quant", []) if d["product_id"][0] == m["product_id"][0]
                                and d["location_id"][0] == m["location_dest_id"][0] and (d.get("lot_id") or False) == (ml.get("lot_id") or False)]
                        if dest:
                            dest[0]["quantity"] = float(dest[0]["quantity"]) + float(ml["quantity"])
                            dest[0]["available_quantity"] = dest[0]["quantity"] - float(dest[0].get("reserved_quantity") or 0)
                        else:
                            self.tablas.setdefault("stock.quant", []).append({
                                "id": self._id(), "product_id": m["product_id"], "location_id": m["location_dest_id"],
                                "quantity": float(ml["quantity"]), "available_quantity": float(ml["quantity"]), "reserved_quantity": 0.0,
                                "lot_id": ml.get("lot_id") or False, "expiration_date": False, "company_id": [2, "CBH+"], "location_id.usage": "internal"})
                    m["state"] = "done"
                p["state"], p["date_done"] = "done", self.hoy.strftime("%Y-%m-%d %H:%M:%S")
            return True
        if metodo in ("action_cancel", "button_cancel"):
            for p in pickings:
                for m in [x for x in moves if x["picking_id"][0] == p["id"]]:
                    for ml in [x for x in mls if isinstance(x.get("move_id"), (list, tuple)) and x["move_id"][0] == m["id"]]:
                        lote = [ml["lot_id"][0]] if ml.get("lot_id") else None
                        for qu in self._quants(m["product_id"][0], m["location_id"][0], lote):
                            qu["reserved_quantity"] = max(0.0, float(qu.get("reserved_quantity") or 0) - float(ml["quantity"]))
                            qu["available_quantity"] = float(qu["quantity"]) - qu["reserved_quantity"]
                            break
                    self.tablas["stock.move.line"] = [x for x in self.tablas["stock.move.line"]
                                                      if not (isinstance(x.get("move_id"), (list, tuple)) and x["move_id"][0] == m["id"])]
                    m["state"], m["quantity"] = "cancel", 0.0
                p["state"] = "cancel"
            return True
        if metodo == "action_draft":
            for p in pickings:
                p["state"] = "draft"
            return True
        return True

    WRITE_DATE_BASE = "2026-01-01 00:00:00"   # registros sembrados sin write_date explícito

    @classmethod
    def _write_date(cls, rec: dict) -> str:
        return rec.get("write_date") or rec.get("create_date") or cls.WRITE_DATE_BASE

    @classmethod
    def _proyectar(cls, rec: dict, campos: list[str] | None) -> dict:
        if not campos:
            return {k: v for k, v in rec.items() if "." not in k}
        out = {"id": rec["id"]}
        for c in campos:
            out[c] = cls._write_date(rec) if c == "write_date" else rec.get(c, False)
        return out

    def search_read(self, modelo, dominio, campos=None, limite=0, orden=None, offset=0):
        rows = self._filtrar(modelo, dominio or [])
        if orden:
            campo_o = orden.split()[0]
            rev = orden.lower().endswith(" desc")
            rows = sorted(rows, key=lambda r_: (self._valor(r_, campo_o) is None, self._valor(r_, campo_o) or 0),
                          reverse=rev)
        rows = rows[offset:]
        if limite:
            rows = rows[:limite]
        return [self._proyectar(r_, campos) for r_ in rows]

    def search_read_por_ids(self, modelo, ids, campos, dominio_extra=None, campo="id", bloque=1000):
        ids = sorted({int(x) for x in ids})
        out = []
        for i in range(0, len(ids), bloque):
            out.extend(self.search_read(modelo, [[campo, "in", ids[i:i + bloque]]] + list(dominio_extra or []), campos, limite=0))
        return out

    def search_read_all(self, modelo, dominio, campos, pagina=5000, tope=200_000, orden="id"):
        return self.search_read(modelo, dominio, campos, limite=tope, orden=orden)

    def search_count(self, modelo, dominio):
        return len(self._filtrar(modelo, dominio or []))

    def read_group(self, modelo, dominio, campos, groupby, lazy=False, limite=0):
        rows = self._filtrar(modelo, dominio)
        grupos: dict = {}
        for r_ in rows:
            k = tuple(self._valor(r_, g) for g in groupby)
            grupos.setdefault(k, []).append(r_)
        out = []
        for k, rs in grupos.items():
            d = {g: rs[0].get(g) for g in groupby}
            d["__count"] = len(rs)
            for c in campos:
                base = c.split(":")[0]
                if base not in groupby:
                    d[base] = sum(float(x.get(base) or 0) for x in rs)
            out.append(d)
        return out[:limite] if limite else out

    def fields_get(self, modelo, atributos=None):
        rows = self.tablas.get(modelo, [])
        if not rows:
            return {}
        campos: dict = {}
        muestra = {}
        for r_ in rows[:50]:
            muestra.update({k: v for k, v in r_.items() if v is not None and v is not False})
        relaciones = {"product_id": "product.product", "lot_id": "stock.lot", "product_uom_id": "uom.uom", "product_uom": "uom.uom",
                      "uom_id": "uom.uom", "location_id": "stock.location", "location_dest_id": "stock.location",
                      "partner_id": "res.partner", "medico_id": "res.partner", "hospital_id": "res.partner", "auxiliar_id": "hr.employee",
                      "employee_id": "hr.employee", "user_id": "res.users", "company_id": "res.company", "warehouse_id": "stock.warehouse",
                      "picking_id": "stock.picking", "move_id": "stock.move", "order_id": "purchase.order", "picking_type_id": "stock.picking.type",
                      "subalmacen_id": "stock.location", "categ_id": "product.category", "operacion_id": "cbh.operacion.medica"}
        for k, v in muestra.items():
            if "." in k:
                continue
            if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
                tipo = "many2one"
            elif isinstance(v, (list, tuple)):
                tipo = "one2many" if k.endswith("_ids") or k.endswith("line_ids") else "many2many"
            elif isinstance(v, bool):
                tipo = "boolean"
            elif isinstance(v, (int, float)):
                tipo = "float"
            elif isinstance(v, str) and len(v) == 19 and v[4] == "-" and v[10] == " ":
                tipo = "datetime"
            elif isinstance(v, str) and len(v) == 10 and v[4] == "-":
                tipo = "date"
            else:
                tipo = "char"
            meta = {"string": k.replace("_id", "").replace("_", " ").title(), "type": tipo}
            if tipo == "many2one":
                rel = relaciones.get(k)
                if not rel:
                    # relación desconocida: se busca la tabla que contiene ese id (como haría ir.model.fields)
                    for tabla, filas in self.tablas.items():
                        if tabla != modelo and tabla != "ir.model" and any(f.get("id") == v[0] for f in filas[:5000]):
                            rel = tabla
                            break
                if rel:
                    meta["relation"] = rel
            campos[k] = meta
        # columnas mágicas de todo modelo Odoo (las usa la memoria incremental del consumo)
        campos.setdefault("write_date", {"string": "Last Updated on", "type": "datetime"})
        campos.setdefault("create_date", {"string": "Created on", "type": "datetime"})
        # one2many de cabecera a líneas (folio → líneas de consumo), como en el modelo real
        if modelo in ("cbh.operacion.medica",) or any(t.startswith(modelo + ".") for t in self.tablas):
            for t in self.tablas:
                if t.startswith(modelo + ".") and t != modelo:
                    campos.setdefault("line_ids", {"string": "Líneas", "type": "one2many", "relation": t})
        return campos

    def modelos(self, patron=""):
        return [m for m in self.tablas["ir.model"] if patron in m["model"]]

    def existe_modelo(self, modelo):
        return modelo in self.tablas

    def create(self, modelo, valores):
        if isinstance(valores, list):
            return [self.create(modelo, v) for v in valores]
        if modelo == "purchase.order" and not valores.get("partner_id") and getattr(self, "proveedor_obligatorio", True):
            # como Odoo: el proveedor es obligatorio en la solicitud de cotización
            raise OdooError("Odoo Server Error: The following fields are invalid: Vendor (partner_id) is required")
        rec = dict(valores)
        rec["id"] = self._id()
        rec.setdefault("state", "draft")
        rec.setdefault("write_date", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        rec.setdefault("name", f"{modelo.split('.')[-1].upper()}/{rec['id']}")
        # resolver many2one a [id, nombre] para lecturas posteriores
        for k, v in list(rec.items()):
            if k.endswith("_id") and isinstance(v, int):
                rec[k] = [v, self._nombre_de(k, v)]
        # líneas embebidas (0, 0, vals)
        for k, v in list(rec.items()):
            if isinstance(v, list) and v and isinstance(v[0], (list, tuple)) and len(v[0]) == 3:
                hijos = []
                sub = {"move_ids_without_package": "stock.move", "move_ids": "stock.move",
                       "order_line": "purchase.order.line"}.get(k, k)
                enlace = {"stock.move": "picking_id", "purchase.order.line": "order_id"}.get(sub, "parent_id")
                for cmd in v:
                    if cmd[0] == 0:
                        hid = self.create(sub, {**cmd[2], enlace: [rec["id"], rec["name"]]})
                        hijos.append(hid)
                    elif cmd[0] == 6:
                        hijos.extend(cmd[2])
                rec[k] = hijos
        self.tablas.setdefault(modelo, []).append(rec)
        return rec["id"]

    def _nombre_de(self, campo: str, id_: int) -> str:
        tabla = {"product_id": "product.product", "location_id": "stock.location",
                 "location_dest_id": "stock.location", "picking_type_id": "stock.picking.type",
                 "partner_id": "res.partner", "warehouse_id": "stock.warehouse", "user_id": "res.users",
                 "product_uom": "uom.uom", "product_uom_id": "uom.uom"}.get(campo)
        if tabla:
            for r_ in self.tablas.get(tabla, []):
                if r_["id"] == id_:
                    return r_.get("display_name") or r_.get("complete_name") or r_.get("name", "")
        return ""

    def write(self, modelo, ids, valores):
        ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for r_ in self.tablas.get(modelo, []):
            if r_["id"] in ids:
                r_.update(valores)
                r_["write_date"] = ahora
        return True

    def unlink(self, modelo, ids):
        self.tablas[modelo] = [r_ for r_ in self.tablas.get(modelo, []) if r_["id"] not in ids]
        return True

    def call_button(self, modelo, ids, metodo):
        return self.execute(modelo, metodo, [ids])

    def name_search(self, modelo, nombre, limite=10, dominio=None):
        return self.execute(modelo, "name_search", [], name=nombre, limit=limite)

    def mensaje_chatter(self, modelo, res_id, cuerpo, asunto=""):
        return self.execute(modelo, "message_post", [[res_id]], body=cuerpo, subject=asunto)
