"""Mapeo lógico ▸ físico de los modelos de Odoo en CBH+.

Los agentes nunca escriben nombres de modelos/campos "a mano": piden conceptos
(``consumo.cantidad``, ``consumo.folio``…) y esta capa los traduce al nombre real.

El mapeo se resuelve en tres pasos:
  1. valores por defecto (candidatos ordenados por probabilidad),
  2. auto-descubrimiento contra la instancia real (``descubrir``),
  3. sobreescritura manual desde la UI (queda guardada en el disco persistente).

Así el sistema arranca aunque los módulos custom de CBH tengan otros nombres:
si no encuentra el modelo de operaciones médicas, cae a ``stock.move.line``,
que existe en cualquier base de Odoo.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from .. import db
from .client import OdooClient, get_client

AJUSTE_MAPEO = "mapeo_odoo"

# ── Candidatos por entidad lógica ───────────────────────────────────────────
# "modelos": se prueba en orden hasta encontrar uno que exista.
# "campos" : por cada campo lógico, lista de nombres candidatos + pistas de etiqueta.
CANDIDATOS: dict[str, dict[str, Any]] = {
    "folio": {
        "descripcion": "Cabecera de la operación médica / folio / ticket de consumo",
        # cbh.medical.service.request = «CB Ticket» del módulo cbh_operaciones_medicas (Odoo 19) de CBH+
        "modelos": ["cbh.medical.service.request", "cbh.operacion.medica", "cbh.operaciones.medicas", "operacion.medica",
                    "operaciones.medicas", "medical.operation", "medical.ticket",
                    "cbh.folio", "cb.ticket", "cbh.ticket", "cbticket.ticket", "cb.folio", "ticket.consumo",
                    "cb.operacion", "cb.operacion.medica", "x_folio", "x_ticket", "stock.picking"],
        "campos": {
            "nombre": (["name", "folio", "numero", "referencia"], ["folio interno", "folio", "número", "referencia"]),
            "folio_asignado": (["folio_asignado"], ["folio asignado"]),
            "fecha": (["surgery_date", "fecha", "date", "fecha_operacion", "request_date", "scheduled_date", "date_done", "create_date"],
                      ["fecha"]),
            "fecha_registro": (["request_date"], ["fecha de registro"]),
            "estado": (["state", "estado"], ["estado"]),
            "hospital": (["medical_unit_id", "hospital_id", "unidad_medica_id", "x_studio_unidad_medica", "partner_id"],
                         ["hospital / unidad médica", "hospital", "unidad médica"]),
            "unidad_config": (["unit_config_id"], []),
            "medico": (["anesthesiologist_id", "medico_id", "doctor_id", "anestesiologo_id", "x_studio_medico"],
                       ["anestesiólogo", "médico", "doctor"]),
            "cirujano": (["surgeon_id", "cirujano_id"], ["cirujano"]),
            "paciente": (["patient_name", "paciente", "patient", "paciente_id", "nombre_paciente"], ["paciente"]),
            "almacen": (["warehouse_id", "almacen_id", "picking_type_id"], ["almacén", "bodega"]),
            "ubicacion": (["inventory_source_location_id", "location_id", "ubicacion_id"], ["ubicación origen del ticket", "ubicación"]),
            "empleado": (["technician_employee_id", "employee_id", "empleado_id", "auxiliar_id", "user_id"],
                         ["técnico", "empleado", "auxiliar", "responsable"]),
            "hora_inicio": (["anesthesia_time_start", "hora_inicio", "fecha_inicio", "start_time", "x_studio_hora_inicio"],
                            ["inicio anestesia", "hora de inicio", "inicio de anestesia"]),
            "hora_fin": (["anesthesia_time_end", "hora_fin", "fecha_fin", "end_time", "x_studio_hora_fin"],
                         ["fin anestesia", "hora de fin", "fin de anestesia"]),
            "inicio_dt": (["procedure_datetime_start"], []),
            "fin_dt": (["procedure_datetime_end"], []),
            "duracion_min": (["duracion", "duracion_min", "tiempo_anestesia", "x_studio_duracion"],
                             ["duración", "tiempo de anestesia", "minutos"]),
            "tipo_cirugia": (["surgical_procedure", "tipo_cirugia", "procedimiento", "cirugia", "x_studio_procedimiento"],
                             ["procedimiento quirúrgico", "tipo de cirugía", "procedimiento"]),
            "especialidad": (["surgical_specialty"], ["especialidad quirúrgica"]),
            "tipo_evento": (["event_type"], ["evento"]),
            "turno": (["shift"], ["turno"]),
            "quirofano": (["operating_room_id", "quirofano", "sala", "x_studio_quirofano"], ["quirófano", "sala"]),
            "picking_consumo": (["picking_consumption_id"], ["consumo real"]),
            "importe_paquete": (["package_price"], ["precio del paquete"]),
            "paquete": (["billing_package_name"], ["nombre del paquete"]),
            "compania": (["company_id"], ["compañía"]),
        },
    },
    "consumo": {
        "descripcion": "Línea de consumo de insumos/anestésicos dentro del folio",
        # cbh.medical.service.request.line = «Consumo de CB Ticket» (cbh_operaciones_medicas). La fecha, el médico, el
        # técnico, el quirófano y el sub-almacén viven en la cabecera: queries.consumo() los trae por folio_id.
        "modelos": ["cbh.medical.service.request.line", "cbh.operacion.medica.line", "cbh.operaciones.medicas.line",
                    "operacion.medica.line", "operaciones.medicas.line",
                    "medical.operation.line", "medical.consumption",
                    "cb.ticket.line", "cbh.ticket.line", "cbticket.ticket.line", "cb.ticket.linea", "cb.folio.line",
                    "ticket.consumo.line", "cb.operacion.line", "cb.operacion.medica.line", "x_folio_line", "x_ticket_line",
                    "stock.move.line"],
        "campos": {
            "folio_id": (["request_id", "operacion_id", "operation_id", "folio_id", "order_id", "move_id",
                          "picking_id"], ["cb ticket", "folio", "operación"]),
            "fecha": (["fecha", "date", "fecha_consumo", "create_date"], ["fecha"]),
            "producto": (["product_id", "producto_id"], ["producto"]),
            "producto_efectivo": (["effective_product_id"], ["producto efectivo"]),
            "cantidad": (["qty_used", "cantidad", "quantity", "qty", "qty_done", "product_uom_qty"],
                         ["cantidad utilizada", "cantidad", "consumido"]),
            "cantidad_paquete": (["quantity_standard"], ["cantidad del paquete"]),
            "cantidad_max": (["qty_max"], ["máximo"]),
            "unidad": (["uom_id", "product_uom_id", "product_uom"], ["unidad de medida"]),
            "lote": (["lot_id", "lote_id", "prod_lot_id"], ["lote", "serie"]),
            "caducidad": (["expiration_date", "use_date", "caducidad", "fecha_caducidad"], ["caducidad"]),
            "ubicacion": (["location_id", "ubicacion_id"], ["ubicación origen"]),
            "ubicacion_destino": (["location_dest_id"], ["ubicación destino"]),
            "hospital": (["medical_unit_id", "hospital_id", "unidad_medica_id", "x_studio_unidad_medica"],
                         ["hospital", "unidad médica"]),
            "medico": (["medico_id", "doctor_id", "anestesiologo_id"], ["médico", "anestesiólogo"]),
            "importe": (["price_subtotal", "importe", "amount"], ["importe", "subtotal"]),
            "peso_inicial": (["initial_weight_g", "peso_inicial", "peso_ini", "weight_start", "x_studio_peso_inicial"],
                             ["peso inicial", "peso antes"]),
            "peso_final": (["final_weight_g", "peso_final", "peso_fin", "weight_end", "x_studio_peso_final"],
                           ["peso final", "peso después"]),
            "consumo_peso": (["consumed_weight_g", "consumo_peso", "consumo_gr", "peso_consumido", "x_studio_consumo_bascula"],
                             ["consumo calculado (g)", "consumo báscula", "gramos consumidos"]),
            "consumo_ml": (["consumed_qty_ml"], ["consumo calculado (ml)"]),
            "pesable": (["cbh_weighable"], ["control por pesaje"]),
            "fecha_pesaje_final": (["final_weighing_date"], ["fecha pesaje final"]),
            "tipo_linea": (["line_type"], []),
            "auxiliar": (["auxiliar_id", "employee_id", "empleado_id", "x_studio_auxiliar"],
                         ["auxiliar", "empleado"]),
            "subalmacen": (["subalmacen_id", "sub_almacen_id", "x_studio_subalmacen"],
                           ["sub-almacén", "subalmacén"]),
            "duracion_min": (["duracion", "duracion_min", "tiempo_anestesia"], ["duración", "minutos"]),
            "compania": (["company_id"], ["compañía"]),
        },
    },
    "producto": {
        "descripcion": "Producto / insumo",
        "modelos": ["product.product"],
        "campos": {
            "nombre": (["display_name", "name"], ["nombre"]),
            "codigo": (["default_code"], ["referencia interna"]),
            "categoria": (["categ_id"], ["categoría"]),
            "unidad": (["uom_id"], ["unidad"]),
            "tipo": (["type", "detailed_type"], ["tipo"]),
            "rastreo": (["tracking"], ["trazabilidad"]),
            "costo": (["standard_price"], ["costo"]),
            "precio": (["list_price"], ["precio"]),
            "disponible": (["qty_available"], ["cantidad a mano"]),
            "activo": (["active"], ["activo"]),
            "densidad": (["densidad", "x_studio_densidad", "density"], ["densidad"]),
            "capacidad": (["contenido_ml", "capacidad", "x_studio_contenido_ml", "x_studio_capacidad", "volume"],
                          ["contenido", "capacidad", "volumen"]),
        },
    },
    "existencias": {
        "descripcion": "Existencias por ubicación (stock.quant)",
        "modelos": ["stock.quant"],
        "campos": {
            "producto": (["product_id"], ["producto"]),
            "ubicacion": (["location_id"], ["ubicación"]),
            "almacen": (["warehouse_id"], ["almacén"]),
            "cantidad": (["quantity"], ["cantidad"]),
            "disponible": (["available_quantity"], ["disponible"]),
            "reservado": (["reserved_quantity"], ["reservado"]),
            "lote": (["lot_id"], ["lote"]),
            "caducidad": (["expiration_date", "removal_date"], ["caducidad"]),
            "compania": (["company_id"], ["compañía"]),
        },
    },
    "movimiento": {
        "descripcion": "Movimiento de inventario (stock.move) — respaldo universal de demanda",
        "modelos": ["stock.move"],
        "campos": {
            "fecha": (["date"], ["fecha"]),
            "producto": (["product_id"], ["producto"]),
            "cantidad": (["quantity", "product_uom_qty", "product_qty"], ["cantidad"]),
            "estado": (["state"], ["estado"]),
            "origen": (["location_id"], ["ubicación origen"]),
            "destino": (["location_dest_id"], ["ubicación destino"]),
            "referencia": (["reference", "origin"], ["referencia"]),
            "picking": (["picking_id"], ["albarán", "transferencia"]),
            "compania": (["company_id"], ["compañía"]),
        },
    },
    "almacen": {
        "descripcion": "Almacén",
        "modelos": ["stock.warehouse"],
        "campos": {
            "nombre": (["name"], ["nombre"]),
            "codigo": (["code"], ["código"]),
            "ubicacion_stock": (["lot_stock_id"], ["ubicación de existencias"]),
            "compania": (["company_id"], ["compañía"]),
        },
    },
    "ubicacion": {
        "descripcion": "Ubicación / sub-almacén",
        "modelos": ["stock.location"],
        "campos": {
            "nombre": (["complete_name", "name"], ["nombre"]),
            "uso": (["usage"], ["tipo de ubicación"]),
            "almacen": (["warehouse_id"], ["almacén"]),
            "padre": (["location_id"], ["ubicación padre"]),
        },
    },
    "unidad_medica": {
        "descripcion": "Unidad médica (res.partner con el check Es Unidad Médica)",
        "modelos": ["res.partner"],
        "campos": {
            "nombre": (["name"], ["nombre"]),
            "es_unidad": (["is_medical_unit", "x_studio_es_unidad_medica"], ["es unidad médica"]),
            "es_medico": (["is_doctor", "es_medico", "x_studio_es_medico"], ["es médico"]),
            "unidad_del_medico": (["x_studio_unidad_medica", "unidad_medica_id"], ["unidad médica"]),
        },
    },
    "empleado": {
        "descripcion": "Empleado (hr.employee) con su unidad médica",
        "modelos": ["hr.employee"],
        "campos": {
            "nombre": (["name"], ["nombre"]),
            "unidad_medica": (["x_studio_unidad_medica"], ["unidad médica"]),
            "departamento": (["department_id"], ["departamento"]),
        },
    },
    "regla_reabastecimiento": {
        "descripcion": "Regla de reabastecimiento min/max",
        "modelos": ["stock.warehouse.orderpoint"],
        "campos": {
            "producto": (["product_id"], ["producto"]),
            "almacen": (["warehouse_id"], ["almacén"]),
            "ubicacion": (["location_id"], ["ubicación"]),
            "minimo": (["product_min_qty"], ["cantidad mínima"]),
            "maximo": (["product_max_qty"], ["cantidad máxima"]),
            "multiplo": (["qty_multiple"], ["múltiplo"]),
            "compania": (["company_id"], ["compañía"]),
        },
    },
}

# Modelos de escritura que usa el motor de autonomía
MODELOS_ACCION = {
    "transferencia": "stock.picking",
    "transferencia_linea": "stock.move",
    "tipo_operacion": "stock.picking.type",
    "compra": "purchase.order",
    "compra_linea": "purchase.order.line",
    "regla": "stock.warehouse.orderpoint",
    "actividad": "mail.activity",
    "proveedor_info": "product.supplierinfo",
}


# ── utilidades ──────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def mapeo_por_defecto() -> dict:
    """Mapeo inicial: primer candidato de cada lista, sin tocar Odoo."""
    out: dict[str, Any] = {"_resuelto": False, "entidades": {}}
    for ent, cfg in CANDIDATOS.items():
        out["entidades"][ent] = {
            "modelo": cfg["modelos"][0],
            "campos": {k: v[0][0] for k, v in cfg["campos"].items()},
            "confianza": "por_defecto",
        }
    out["respaldo_consumo"] = "stock.move.line"
    return out


def cargar() -> dict:
    """Mapeo vigente (persistente). Si no hay, devuelve el de fábrica."""
    m = db.get_ajuste(AJUSTE_MAPEO)
    if not m:
        m = mapeo_por_defecto()
    return m


def guardar(mapeo: dict) -> None:
    db.set_ajuste(AJUSTE_MAPEO, mapeo)
    db.log("info", "mapeo", "Mapeo de Odoo actualizado",
           f"entidades={list(mapeo.get('entidades', {}))}")


def modelo(entidad: str) -> str:
    """Modelo físico de la entidad. Nunca devuelve vacío para entidades con modelo estándar (existencias, producto,
    movimiento…): si el descubrimiento no pudo verificarlo, se usa el candidato por defecto y Odoo dirá si no existe.
    Sólo «consumo» y «folio» (módulo custom de CBH) pueden quedar vacíos, y entonces se usa el respaldo."""
    m = cargar()["entidades"].get(entidad, {}).get("modelo", "")
    if not m and entidad in CANDIDATOS and entidad not in ("consumo", "folio"):
        m = CANDIDATOS[entidad]["modelos"][0]
    return m


def en_respaldo(mapeo: dict | None = None) -> bool:
    """True cuando el consumo se lee de movimientos de inventario (``stock.move.line``) y no del módulo de operaciones
    médicas de CBH. En ese modo no hay folio médico, médico, auxiliar, básculas ni importe: las reglas que dependen de
    ello (R04 sin cirugía, R08 duplicado) y los tickets de facturación se desactivan, y la interfaz lo avisa."""
    m = mapeo or cargar()
    cons = m.get("entidades", {}).get("consumo", {}) or {}
    return cons.get("modelo", "") in ("", "stock.move.line") or not cons.get("campos")


def campo(entidad: str, logico: str, default: str = "") -> str:
    return cargar()["entidades"].get(entidad, {}).get("campos", {}).get(logico, default)


def campos(entidad: str, logicos: list[str]) -> list[str]:
    """Nombres físicos existentes para una lista de campos lógicos (omite los no mapeados)."""
    m = cargar()["entidades"].get(entidad, {}).get("campos", {})
    return [m[l] for l in logicos if m.get(l)]


def mapa(entidad: str) -> dict[str, str]:
    return dict(cargar()["entidades"].get(entidad, {}).get("campos", {}))


# ── auto-descubrimiento ─────────────────────────────────────────────────────
# Relación que debe tener un campo lógico cuando es many2one (se usa cuando ni el nombre ni la etiqueta coinciden):
# así «producto» se encuentra aunque el campo se llame «insumo_id», porque apunta a product.product.
RELACION_ESPERADA: dict[str, dict[str, str]] = {
    "consumo": {"producto": "product.product", "unidad": "uom.uom", "lote": "stock.lot", "auxiliar": "hr.employee",
                "ubicacion": "stock.location", "ubicacion_destino": "stock.location", "compania": "res.company"},
    "folio": {"almacen": "stock.warehouse", "empleado": "hr.employee", "compania": "res.company"},
}


def _resolver_campo(fields: dict, candidatos: list[str], pistas: list[str], relacion: str | None = None,
                    excluir: set | None = None) -> tuple[str, str]:
    """Devuelve (nombre_fisico, confianza). Orden: nombre exacto → etiqueta → relación many2one → nombre parecido."""
    excluir = excluir or set()
    for c in candidatos:
        if c in fields and c not in excluir:
            return c, "exacta"
    pistas_n = [_norm(p) for p in pistas]
    for nombre, meta in fields.items():
        etiqueta = _norm(meta.get("string", ""))
        if nombre not in excluir and etiqueta and any(p and p in etiqueta for p in pistas_n):
            return nombre, "por_etiqueta"
    if relacion:
        rel = [n for n, m in fields.items() if m.get("type") == "many2one" and m.get("relation") == relacion and n not in excluir]
        if rel:
            # si hay varios con la misma relación, gana el que se parezca por nombre; si no, el primero
            rel.sort(key=lambda n: (not any(_norm(c) in _norm(n) for c in candidatos), n))
            return rel[0], "por_relacion"
    # último recurso: coincidencia por nombre técnico parecido
    for nombre in fields:
        n = _norm(nombre)
        if nombre not in excluir and any(_norm(c) in n for c in candidatos):
            return nombre, "aproximada"
    return "", "no_encontrado"


# Espacios de nombres de los modelos estándar de Odoo: todo lo demás es candidato a módulo del cliente.
_NAMESPACES_ESTANDAR = {
    "ir", "res", "mail", "bus", "base", "web", "iap", "digest", "auth", "portal", "website", "account", "stock", "purchase", "sale",
    "product", "uom", "hr", "mrp", "project", "crm", "helpdesk", "pos", "calendar", "discuss", "onboarding", "payment", "utm",
    "resource", "report", "sms", "snailmail", "spreadsheet", "documents", "knowledge", "sign", "planning", "quality", "maintenance",
    "repair", "fleet", "event", "survey", "lunch", "gamification", "im_livechat", "mailing", "marketing", "social", "analytic",
    "delivery", "barcode", "decimal", "privacy", "phone", "link", "google", "microsoft", "html", "change", "reset", "rating", "avatar",
    "fetchmail", "loyalty", "coupon", "approval", "timesheet", "attachment", "board", "note", "expense", "recruitment", "appraisal",
    "referral", "skill", "attendance", "payroll", "worksheet", "industry", "fsm", "plm", "wizard", "theme", "test", "data", "odoo",
    "cloud", "iot", "voip", "whatsapp", "contacts", "partner", "currency", "fiscal", "tax", "bank", "sequence", "cron", "queue",
    "dashboard", "studio", "esg", "mass", "im", "format", "address", "tz", "lang", "country", "company", "users", "groups", "module",
    "actions", "config", "translation", "http", "asset", "openid", "sale_management", "mrp_subcontracting", "stock_landed_costs",
    "product_expiry", "quality_control", "hr_holidays", "hr_timesheet", "mail_bot", "web_studio", "web_editor", "web_tour", "web_unsplash",
    "product_margin", "purchase_stock", "sale_stock", "sale_purchase", "account_edi", "l10n", "l10n_mx", "l10n_mx_edi", "point_of_sale",
    "cash", "generic", "decimal_precision", "onboarding", "digest", "base_setup", "base_import", "base_automation", "base_geolocalize",
    "hw", "spreadsheet_dashboard", "documents_project", "sale_project", "sale_timesheet", "stock_account", "purchase_requisition",
    "mrp_account", "stock_picking_batch", "stock_barcode", "quality_mrp", "stock_dropshipping", "sale_margin", "product_matrix",
}


def _es_personalizado(meta: dict) -> bool:
    """¿Este modelo pertenece a un módulo del cliente (custom o Studio)?"""
    nombre = str(meta.get("model") or "")
    if not nombre or meta.get("transient"):
        return False
    if nombre.startswith("x_") or meta.get("state") == "manual":
        return True
    primero = nombre.split(".")[0]
    if primero.startswith("l10n") or primero in _NAMESPACES_ESTANDAR:
        # aún puede ser custom si lo define un módulo del cliente (p. ej. «cbh_ticket» sobre «stock.xxx»); se decide por módulos
        modulos = [m.strip() for m in str(meta.get("modules") or "").split(",") if m.strip()]
        return bool(modulos) and all(_modulo_personalizado(m) for m in modulos)
    return True


def _modulo_personalizado(modulo: str) -> bool:
    m = modulo.lower()
    if m.startswith("l10n") or m in _NAMESPACES_ESTANDAR:
        return False
    return not any(m.startswith(p + "_") for p in ("account", "stock", "sale", "purchase", "hr", "mrp", "web", "base", "mail", "product",
                                                    "pos", "website", "crm", "project", "helpdesk", "calendar", "event", "survey", "sign",
                                                    "documents", "knowledge", "planning", "quality", "maintenance", "repair", "fleet",
                                                    "lunch", "iap", "digest", "utm", "resource", "report", "sms", "snailmail", "payment",
                                                    "delivery", "spreadsheet", "google", "microsoft", "html", "rating", "loyalty", "timesheet",
                                                    "board", "expense", "recruitment", "appraisal", "referral", "attendance", "payroll",
                                                    "industry", "plm", "theme", "test", "odoo", "cloud", "iot", "voip", "whatsapp", "mass",
                                                    "im", "auth", "analytic", "onboarding", "bus", "portal", "gamification", "discuss",
                                                    "marketing", "social", "phone", "partner", "barcodes", "decimal", "privacy", "uom",
                                                    "point"))


def _puntuar_linea(fields: dict) -> tuple[int, list[str]]:
    """Cuánto se parece un modelo a la LÍNEA de consumo del folio (producto + cantidad + lote + báscula + médico…)."""
    pts, por = 0, []
    rel = {n: m.get("relation") for n, m in fields.items() if m.get("type") == "many2one"}
    if "product.product" in rel.values():
        pts += 4; por.append("producto")
    if any(m.get("type") in ("float", "integer") and any(k in _norm(n) + " " + _norm(m.get("string", "")) for k in ("cantidad", "quantity", "qty", "consum"))
           for n, m in fields.items()):
        pts += 3; por.append("cantidad")
    if "stock.lot" in rel.values() or "stock.production.lot" in rel.values():
        pts += 1; por.append("lote")
    if any("peso" in _norm(n) + " " + _norm(m.get("string", "")) or "weight" in _norm(n) for n, m in fields.items()):
        pts += 2; por.append("báscula")
    if any(k in _norm(n) + " " + _norm(m.get("string", "")) for n, m in fields.items() for k in ("medic", "doctor", "anestes")):
        pts += 1; por.append("médico")
    if "hr.employee" in rel.values():
        pts += 1; por.append("auxiliar")
    if "uom.uom" in rel.values():
        pts += 1; por.append("unidad")
    return pts, por


def _puntuar_cabecera(fields: dict, modelo_linea: str | None = None) -> tuple[int, list[str]]:
    """Cuánto se parece un modelo a la CABECERA del folio (fecha + hospital + médico + paciente + estado + líneas)."""
    pts, por = 0, []
    if modelo_linea and any(m.get("type") == "one2many" and m.get("relation") == modelo_linea for m in fields.values()):
        pts += 4; por.append("líneas")
    if any(m.get("type") in ("date", "datetime") for m in fields.values()):
        pts += 1; por.append("fecha")
    texto = " ".join(_norm(n) + " " + _norm(m.get("string", "")) for n, m in fields.items())
    for k, p, etiqueta in (("hospital", 2, "hospital"), ("unidad medica", 2, "unidad médica"), ("medic", 1, "médico"), ("doctor", 1, "médico"),
                           ("paciente", 2, "paciente"), ("patient", 2, "paciente"), ("quirof", 1, "quirófano"), ("anestes", 1, "anestesia"),
                           ("cirug", 1, "cirugía"), ("folio", 1, "folio"), ("ticket", 1, "ticket")):
        if k in texto:
            pts += p; por.append(etiqueta)
    if "state" in fields:
        pts += 1; por.append("estado")
    return pts, por


def detectar_modelos_cbh(cli: OdooClient, maximo: int = 200) -> dict:
    """Localiza SOLO el módulo de folios del cliente aunque su nombre técnico no esté en la lista de candidatos:
    lista los modelos que no son estándar de Odoo (módulo custom o Studio), lee sus campos y puntúa cuál parece la línea
    de consumo (producto + cantidad + lote + báscula…) y cuál la cabecera (fecha + hospital + médico + paciente + líneas).
    Devuelve {"consumo": modelo|"", "folio": modelo|"", "candidatos": [...], "avisos": [...]}."""
    out: dict[str, Any] = {"consumo": "", "folio": "", "candidatos": [], "avisos": []}
    try:
        metas = cli.search_read("ir.model", [["transient", "=", False]], ["model", "name", "modules", "state"], limite=0)
    except Exception as e:  # noqa: BLE001
        out["avisos"].append(f"No se pudo listar ir.model para detectar el módulo de folios: {str(e)[:160]}")
        return out
    personalizados = [m for m in metas if _es_personalizado(m)][:maximo]
    if not personalizados:
        out["avisos"].append("Odoo no expone ningún modelo de módulo personalizado al usuario técnico (¿falta acceso de lectura?).")
        return out
    campos_por_modelo: dict[str, dict] = {}
    for m in personalizados:
        try:
            campos_por_modelo[m["model"]] = cli.fields_get(m["model"])
        except Exception:  # noqa: BLE001
            continue
    lineas = []
    for modelo_, f in campos_por_modelo.items():
        pts, por = _puntuar_linea(f)
        if pts >= 7:      # como mínimo producto + cantidad
            lineas.append((pts, modelo_, por))
    lineas.sort(reverse=True)
    for pts, modelo_, por in lineas[:5]:
        out["candidatos"].append({"modelo": modelo_, "rol": "consumo", "puntos": pts, "por": por})
    if not lineas:
        out["avisos"].append("Ningún modelo personalizado tiene producto + cantidad: no se detectó la línea de consumo del folio. "
                             f"Modelos revisados: {', '.join(sorted(campos_por_modelo))[:600]}")
        return out
    pts_l, linea, _ = lineas[0]
    out["consumo"] = linea
    # cabecera: el modelo al que apunta la línea (many2one) con mejor puntuación; si no, el mejor global con one2many a la línea
    padres = [m.get("relation") for m in campos_por_modelo[linea].values()
              if m.get("type") == "many2one" and m.get("relation") in campos_por_modelo and m.get("relation") != linea]
    cabeceras = []
    for modelo_, f in campos_por_modelo.items():
        if modelo_ == linea:
            continue
        pts, por = _puntuar_cabecera(f, linea)
        if modelo_ in padres:
            pts += 3; por = ["la línea apunta aquí"] + por
        if pts >= 4:
            cabeceras.append((pts, modelo_, por))
    cabeceras.sort(reverse=True)
    for pts, modelo_, por in cabeceras[:5]:
        out["candidatos"].append({"modelo": modelo_, "rol": "folio", "puntos": pts, "por": por})
    if cabeceras:
        out["folio"] = cabeceras[0][1]
    else:
        out["avisos"].append(f"Se detectó la línea de consumo ({linea}) pero no una cabecera de folio clara.")
    return out


def descubrir(cliente: OdooClient | None = None, guardar_resultado: bool = True) -> dict:
    """Introspecciona la base real y construye el mapeo.

    No falla si algo no existe: marca la entidad como no disponible y deja el
    respaldo universal (``stock.move.line`` / ``stock.move``) para que los
    agentes sigan operando.
    """
    cli = cliente or get_client()
    resultado: dict[str, Any] = {"_resuelto": True, "entidades": {}, "avisos": []}

    modelos_existentes = set()
    try:
        modelos_existentes = {m["model"] for m in cli.search_read("ir.model", [], ["model"], limite=0)}
    except Exception as e:  # noqa: BLE001
        resultado["avisos"].append(f"No se pudo listar ir.model (el usuario técnico quizá no tiene acceso de lectura a ir.model): {e}")
    if modelos_existentes and "product.product" not in modelos_existentes:
        # ir.model respondió pero con una lista incompleta (reglas de registro): no es fiable para decidir qué NO existe
        resultado["avisos"].append("ir.model devolvió una lista incompleta; los modelos se comprueban uno por uno.")
        modelos_existentes = set()

    # Ajustes manuales previos (Configuración ▸ Ajuste manual) ganan sobre cualquier detección
    previo = db.get_ajuste(AJUSTE_MAPEO) or {}
    manuales = {e: v for e, v in (previo.get("entidades") or {}).items() if v.get("confianza") == "manual" and v.get("modelo")}

    GENERICOS = {"folio": "stock.picking", "consumo": "stock.move.line"}   # último candidato: sólo si no hay módulo del cliente
    detectados: dict[str, str] = {}
    confianza_modelo: dict[str, str] = {}
    for ent, cfg in CANDIDATOS.items():
        elegido = ""
        if ent in manuales:
            elegido, confianza_modelo[ent] = manuales[ent]["modelo"], "manual"
        for cand in ([] if elegido else cfg["modelos"]):
            if cand == GENERICOS.get(ent):
                break                      # el genérico se decide después de intentar la detección automática
            if cand in modelos_existentes:
                elegido = cand
                break
            # Se comprueba pidiendo los campos del modelo directamente: funciona aunque el usuario técnico no pueda leer
            # ir.model, y falla (→ no existe o sin acceso) cuando el modelo no está o no es accesible.
            try:
                if cli.fields_get(cand):
                    elegido = cand
                    break
            except Exception:  # noqa: BLE001
                continue
        if not elegido and ent in GENERICOS:
            # Detección automática del módulo de folios del cliente por la forma de sus modelos (una sola vez para ambas entidades)
            if not detectados:
                det = detectar_modelos_cbh(cli)
                detectados = {k: det[k] for k in ("folio", "consumo") if det.get(k)}
                resultado["deteccion"] = det
                resultado["avisos"].extend(det.get("avisos") or [])
                if det.get("candidatos"):
                    resultado["avisos"].append("Detección automática: " + "; ".join(
                        f"{c['modelo']} como {c['rol']} ({c['puntos']} pts: {', '.join(c['por'])})" for c in det["candidatos"][:6]))
            if detectados.get(ent):
                elegido, confianza_modelo[ent] = detectados[ent], "detectada"
        if not elegido and ent in GENERICOS:
            try:
                if cli.fields_get(GENERICOS[ent]):
                    elegido = GENERICOS[ent]
            except Exception:  # noqa: BLE001
                pass
        if not elegido:
            resultado["entidades"][ent] = {"modelo": "", "campos": {}, "confianza": "no_disponible"}
            resultado["avisos"].append(
                f"No se encontró ningún modelo para «{ent}». Probados: {', '.join(cfg['modelos'])}")
            continue

        try:
            fields = cli.fields_get(elegido)
        except Exception as e:  # noqa: BLE001
            resultado["entidades"][ent] = {"modelo": elegido, "campos": {}, "confianza": "error"}
            resultado["avisos"].append(f"No se pudieron leer los campos de {elegido}: {e}")
            continue

        mapa_campos, detalle = {}, {}
        relaciones = dict(RELACION_ESPERADA.get(ent, {}))
        if ent == "consumo" and resultado["entidades"].get("folio", {}).get("modelo"):
            relaciones["folio_id"] = resultado["entidades"]["folio"]["modelo"]    # la línea apunta a la cabecera detectada
        usados: set = set()
        for logico, (cands, pistas) in cfg["campos"].items():
            fisico, conf = _resolver_campo(fields, cands, pistas, relaciones.get(logico), excluir=usados)
            if fisico:
                mapa_campos[logico] = fisico
                usados.add(fisico)
            detalle[logico] = {"fisico": fisico, "confianza": conf,
                               "etiqueta": fields.get(fisico, {}).get("string", ""),
                               "tipo": fields.get(fisico, {}).get("type", "")}
            if conf == "no_encontrado":
                resultado["avisos"].append(f"{ent}.{logico}: sin equivalente en {elegido}")
        if ent in manuales and manuales[ent].get("campos"):
            mapa_campos.update(manuales[ent]["campos"])        # campos fijados a mano ganan

        resultado["entidades"][ent] = {
            "modelo": elegido, "campos": mapa_campos, "detalle": detalle,
            "confianza": confianza_modelo.get(ent, "descubierta"),
        }

    # Respaldo: si el modelo de consumo cayó en stock.move.line o no existe,
    # los agentes usarán movimientos de inventario como fuente de demanda.
    cons = resultado["entidades"].get("consumo", {})
    resultado["respaldo_consumo"] = "stock.move.line"
    resultado["usa_respaldo"] = cons.get("modelo") in ("", "stock.move.line")
    if resultado["usa_respaldo"]:
        resultado["avisos"].append(
            "No se detectó el módulo de operaciones médicas de CBH: los agentes "
            "trabajarán sobre movimientos de inventario (stock.move.line).")

    if guardar_resultado:
        guardar(resultado)
    return resultado


def _existe(cli: OdooClient, modelo_: str):
    """True/False si se pudo comprobar en ir.model; None si Odoo no dejó comprobarlo (p. ej. sin acceso a ir.model)."""
    try:
        return cli.existe_modelo(modelo_)
    except Exception:  # noqa: BLE001
        return None


def sobreescribir(entidad: str, modelo_: str | None = None, campos_: dict | None = None) -> dict:
    """Ajuste manual desde la UI (gana sobre el auto-descubrimiento)."""
    m = copy.deepcopy(cargar())
    e = m["entidades"].setdefault(entidad, {"modelo": "", "campos": {}})
    if modelo_:
        e["modelo"] = modelo_
    if campos_:
        e["campos"].update({k: v for k, v in campos_.items() if v})
    e["confianza"] = "manual"
    guardar(m)
    return m
