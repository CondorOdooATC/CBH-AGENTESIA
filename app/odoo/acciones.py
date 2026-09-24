"""Acciones de escritura en Odoo que ejecuta el motor de autonomía (siempre tras aprobación).

Cada función devuelve un dict serializable con ``modelo``, ``id`` y ``ref`` para
poder rastrear y, si hace falta, revertir lo creado.
"""
from __future__ import annotations

from typing import Any

from .. import db
from .client import OdooClient, OdooError, get_client


# ── resolución de identificadores ───────────────────────────────────────────
def ubicacion_por_nombre(nombre: str, cli: OdooClient | None = None) -> dict | None:
    cli = cli or get_client()
    if not nombre:
        return None
    rows = cli.search_read("stock.location", [["complete_name", "ilike", nombre], ["usage", "=", "internal"]],
                           ["id", "complete_name", "warehouse_id"], limite=5)
    if not rows:
        rows = cli.search_read("stock.location", [["name", "ilike", nombre.split("/")[0]], ["usage", "=", "internal"]],
                               ["id", "complete_name", "warehouse_id"], limite=5)
    exactas = [r for r in rows if r["complete_name"].lower() == nombre.lower()]
    return (exactas or rows or [None])[0]


def producto_por_nombre(nombre: str, cli: OdooClient | None = None) -> dict | None:
    cli = cli or get_client()
    rows = cli.search_read("product.product", ["|", ["name", "ilike", nombre], ["default_code", "ilike", nombre]],
                           ["id", "display_name", "uom_id"], limite=5)
    return rows[0] if rows else None


def producto_por_nombre_o_id(pid: int, cli: OdooClient | None = None) -> dict | None:
    cli = cli or get_client()
    rows = cli.search_read("product.product", [["id", "=", int(pid)]], ["id", "display_name", "uom_id"], limite=1)
    return rows[0] if rows else None


def tipo_operacion(codigo: str, ubicacion_id: int | None = None, cli: OdooClient | None = None) -> dict | None:
    """Tipo de operación (interna/compra) preferentemente del almacén de la ubicación dada."""
    cli = cli or get_client()
    dominio: list = [["code", "=", codigo]]
    if ubicacion_id:
        loc = cli.search_read("stock.location", [["id", "=", ubicacion_id]], ["warehouse_id"], limite=1)
        wid = loc[0]["warehouse_id"][0] if loc and loc[0].get("warehouse_id") else None
        if wid:
            rows = cli.search_read("stock.picking.type", dominio + [["warehouse_id", "=", wid]],
                                   ["id", "name", "warehouse_id"], limite=1)
            if rows:
                return rows[0]
    rows = cli.search_read("stock.picking.type", dominio, ["id", "name", "warehouse_id"], limite=1)
    return rows[0] if rows else None


def proveedor_de(producto_id: int, cli: OdooClient | None = None) -> dict | None:
    cli = cli or get_client()
    rows = cli.search_read("product.supplierinfo", [["product_id", "=", producto_id]],
                           ["partner_id", "delay", "price", "min_qty"], limite=1)
    if not rows:
        prod = cli.search_read("product.product", [["id", "=", producto_id]], ["product_tmpl_id"], limite=1)
        if prod and prod[0].get("product_tmpl_id"):
            rows = cli.search_read("product.supplierinfo", [["product_tmpl_id", "=", prod[0]["product_tmpl_id"][0]]],
                                   ["partner_id", "delay", "price", "min_qty"], limite=1)
    return rows[0] if rows else None


# ── acciones ────────────────────────────────────────────────────────────────
class RequiereDecision(OdooError):
    """Odoo devolvió un asistente o pide datos físicos (lotes, cantidades, entrega parcial): la decisión es humana."""


_MODELO_POR_TIPO = {"transferencia_interna": "stock.picking", "cuarentena_lote": "stock.picking", "solicitud_compra": "purchase.order",
                    "ticket_helpdesk": "helpdesk.ticket", "aviso_equipo": "mail.activity", "correo": "mail.mail"}


def recuperar_por_referencia(tipo: str, referencia: str, cli: OdooClient | None = None) -> dict | None:
    """Tras un fallo parcial o una respuesta perdida: localiza el documento que Odoo alcanzó a crear con esta
    referencia (origin) para ligarlo a la acción en vez de crear otro."""
    cli = cli or get_client()
    modelo = _MODELO_POR_TIPO.get(tipo)
    if not modelo or not referencia:
        return None
    if modelo == "helpdesk.ticket":
        r = cli.search_read(modelo, [["description", "ilike", referencia]], ["id", "name", "stage_id"], limite=1)
        return {"modelo": modelo, "id": r[0]["id"], "ref": r[0]["name"], "estado": "abierto", "recuperado": True} if r else None
    if modelo == "mail.mail":
        r = cli.search_read(modelo, [["body_html", "ilike", referencia]], ["id", "subject", "state"], limite=1)
        return {"modelo": modelo, "id": r[0]["id"], "ref": f"correo «{r[0].get('subject')}»", "estado": r[0].get("state"), "recuperado": True} if r else None
    if modelo == "mail.activity":
        r = cli.search_read(modelo, [["note", "ilike", referencia]], ["id", "summary", "user_id"], limite=50)
        if not r:
            return None
        return {"modelo": modelo, "id": r[0]["id"], "ref": f"aviso a {len(r)} persona(s)", "estado": "enviado", "recuperado": True,
                "anterior": {"ids": [x["id"] for x in r]}}
    r = _existente_por_referencia(modelo, referencia, cli)
    return {"modelo": modelo, "id": r["id"], "ref": r["name"], "estado": r.get("state"), "recuperado": True} if r else None


def _existente_por_referencia(modelo: str, referencia: str, cli: OdooClient) -> dict | None:
    """Idempotencia: si ya existe un documento con esta referencia (origin), se reutiliza."""
    if not referencia:
        return None
    try:
        rows = cli.search_read(modelo, [["origin", "=", referencia], ["state", "!=", "cancel"]], ["id", "name", "state"], limite=1)
    except OdooError:
        return None
    return rows[0] if rows else None


def _crear_con_referencia(modelo: str, vals: dict, referencia: str, cli: OdooClient) -> tuple[int, bool]:
    """Crea el documento SIN reintentos automáticos. Si la respuesta se pierde (falla de red tras enviar), busca el
    documento por su referencia única antes de darlo por fallido: así una respuesta perdida nunca duplica."""
    try:
        return int(cli.create(modelo, vals)), False
    except OdooError as e:
        if "conexión" in str(e).lower() or "connection" in str(e).lower() or "timeout" in str(e).lower():
            prev = _existente_por_referencia(modelo, referencia, cli)
            if prev:
                db.log("warn", "odoo", f"Respuesta perdida al crear {modelo}; recuperado por referencia", f"{referencia} → {prev['name']}")
                return int(prev["id"]), True
        raise


def crear_transferencia_interna(producto_id: int, cantidad: float, origen_id: int, destino_id: int,
                                uom_id: int | None = None, referencia: str = "", nota: str = "",
                                confirmar: bool = False, cli: OdooClient | None = None) -> dict:
    cli = cli or get_client()
    previo = _existente_por_referencia("stock.picking", referencia, cli)
    if previo:
        return {"modelo": "stock.picking", "id": previo["id"], "ref": previo["name"], "estado": previo["state"],
                "reutilizado": True}
    tipo = tipo_operacion("internal", origen_id, cli)
    if not tipo:
        raise OdooError("No existe un tipo de operación 'Transferencia interna' en Odoo.")
    prod = cli.search_read("product.product", [["id", "=", producto_id]], ["display_name", "uom_id"], limite=1)
    if not prod:
        raise OdooError(f"Producto {producto_id} no encontrado.")
    uom = uom_id or (prod[0]["uom_id"][0] if prod[0].get("uom_id") else None)
    from .queries import campo_uom
    vals = {
        "picking_type_id": tipo["id"], "location_id": origen_id, "location_dest_id": destino_id,
        "origin": referencia or "Agentes de IA · CBH", "note": nota or False,
        "move_ids_without_package": [(0, 0, {
            "name": prod[0]["display_name"], "product_id": producto_id, "product_uom_qty": float(cantidad),
            campo_uom("stock.move", cli): uom, "location_id": origen_id, "location_dest_id": destino_id,
        })],
    }
    pid, recuperado = _crear_con_referencia("stock.picking", vals, referencia, cli)
    ref = cli.search_read("stock.picking", [["id", "=", pid]], ["name", "state"], limite=1)
    nombre = ref[0]["name"] if ref else str(pid)
    estado = ref[0]["state"] if ref else "draft"
    advertencia = None
    if confirmar and estado == "draft":
        try:
            cli.call_button("stock.picking", [pid], "action_confirm")
            try:
                cli.call_button("stock.picking", [pid], "action_assign")
            except OdooError:
                pass
            est = cli.search_read("stock.picking", [["id", "=", pid]], ["state"], limite=1)
            estado = est[0]["state"] if est else "confirmed"
        except OdooError as e:
            # el documento YA existe en borrador: se informa la advertencia en lugar de fingir éxito o duplicar
            advertencia = f"La transferencia {nombre} se creó pero no pudo confirmarse: {e}"
    if nota:
        try:
            cli.mensaje_chatter("stock.picking", pid, nota, "Agentes de IA · CBH")
        except OdooError:
            pass
    return {"modelo": "stock.picking", "id": pid, "ref": nombre, "estado": estado, "reutilizado": recuperado,
            "cantidad_base": float(cantidad), "advertencia": advertencia}


def crear_solicitud_compra(producto_id: int, cantidad: float, proveedor_id: int | None = None,
                           referencia: str = "", nota: str = "", cli: OdooClient | None = None) -> dict:
    cli = cli or get_client()
    previo = _existente_por_referencia("purchase.order", referencia, cli)
    if previo:
        return {"modelo": "purchase.order", "id": previo["id"], "ref": previo["name"], "estado": previo["state"],
                "reutilizado": True}
    prod = cli.search_read("product.product", [["id", "=", producto_id]], ["display_name", "uom_id", "standard_price"], limite=1)
    if not prod:
        raise OdooError(f"Producto {producto_id} no encontrado.")
    # la RFQ se pide en la unidad de compra del producto (frascos, cajas), redondeando hacia arriba a unidades enteras.
    # Odoo ≤ 18: uom_po_id del producto; Odoo 19: unidad del proveedor principal (queries.unidad_compra_de lo resuelve)
    from .queries import unidad_compra_de
    uc = unidad_compra_de(producto_id, cli)
    uom_base = uc.get("uom_base") or (prod[0]["uom_id"][0] if prod[0].get("uom_id") else False)
    uom_po = uc.get("uom_id") or uom_base
    cantidad_doc, uom_doc, base_por_unidad_compra = float(cantidad), uom_base, 1.0
    if uom_po and uom_base and uom_po != uom_base:
        if uc.get("ratio") is None:
            raise OdooError(f"Odoo conoce la unidad de compra «{uc.get('nombre')}» de {prod[0]['display_name']} pero no su conversión a la unidad base; "
                            "corrígela en Inventario ▸ Unidades de medida antes de comprar.")
        import math
        cantidad_doc, uom_doc = float(math.ceil(float(cantidad) / float(uc["ratio"]) - 1e-9)), uom_po
        base_por_unidad_compra = float(uc["ratio"])
    prov = None
    if not proveedor_id:
        info = proveedor_de(producto_id, cli)
        if info and info.get("partner_id"):
            proveedor_id = info["partner_id"][0]
            prov = info
    sin_proveedor = not proveedor_id
    tipo = tipo_operacion("incoming", None, cli)
    from .queries import campo_uom
    vals: dict[str, Any] = {
        "origin": referencia or "Agentes de IA · CBH",
        "order_line": [(0, 0, {
            "product_id": producto_id, "name": prod[0]["display_name"], "product_qty": cantidad_doc,
            campo_uom("purchase.order.line", cli): uom_doc,     # Odoo 19: product_uom_id; anteriores: product_uom
            # el precio del proveedor ya está en la unidad de compra; el costo estándar es por unidad base
            "price_unit": float((prov or {}).get("price") or (float(prod[0].get("standard_price") or 0.0) * base_por_unidad_compra)),
        })],
    }
    if proveedor_id:
        vals["partner_id"] = proveedor_id
    if tipo:
        vals["picking_type_id"] = tipo["id"]
    advertencia = None
    if sin_proveedor:
        # Petición del cliente: sin proveedor configurado la RFQ se crea de todos modos, con el proveedor en blanco. Odoo
        # exige un proveedor en la solicitud (campo obligatorio); si lo rechaza, se usa el contacto «por definir» que la
        # plataforma mantiene para eso, y Compras lo sustituye por el proveedor real antes de confirmar.
        try:
            poid, recuperado = _crear_con_referencia("purchase.order", vals, referencia or "Agentes de IA · CBH", cli)
            advertencia = "Producto sin proveedor configurado: la RFQ se creó con el proveedor en blanco; asígnalo en Odoo antes de confirmar."
        except OdooError as e:
            if not any(k in str(e).lower() for k in ("partner", "vendor", "proveedor", "required", "obligatorio", "inválid", "invalid")):
                raise
            marcador = proveedor_por_definir(cli)
            vals["partner_id"] = marcador["id"]
            poid, recuperado = _crear_con_referencia("purchase.order", vals, referencia or "Agentes de IA · CBH", cli)
            advertencia = (f"Producto sin proveedor configurado: Odoo exige un proveedor en la RFQ, así que se creó con «{marcador['name']}»; "
                           "sustitúyelo por el proveedor real en Odoo antes de confirmar.")
    else:
        poid, recuperado = _crear_con_referencia("purchase.order", vals, referencia or "Agentes de IA · CBH", cli)
    ref = cli.search_read("purchase.order", [["id", "=", poid]], ["name"], limite=1)
    for texto in (nota, advertencia):
        if texto:
            try:
                cli.mensaje_chatter("purchase.order", poid, texto, "Agentes de IA · CBH")
            except OdooError:
                pass
    unidad_doc = ""
    if uom_doc:
        try:
            u = cli.search_read("uom.uom", [["id", "=", uom_doc]], ["name"], limite=1)
            unidad_doc = u[0]["name"] if u else ""
        except OdooError:
            pass
    out = {"modelo": "purchase.order", "id": poid, "ref": ref[0]["name"] if ref else str(poid), "estado": "draft", "reutilizado": recuperado,
           "cantidad_doc": cantidad_doc, "unidad_doc": unidad_doc, "cantidad_base": float(cantidad), "sin_proveedor": sin_proveedor}
    if advertencia:
        out["advertencia"] = advertencia
    return out


NOMBRE_PROVEEDOR_POR_DEFINIR = "PROVEEDOR POR DEFINIR"


def proveedor_por_definir(cli: OdooClient | None = None) -> dict:
    """Contacto técnico que ocupa el lugar del proveedor en las RFQ de productos sin proveedor (Odoo no permite dejarlo
    vacío). Se crea una sola vez, marcado como proveedor, con una nota que explica su uso. El nombre se puede cambiar
    en Configuración ▸ Políticas (proveedor_por_definir)."""
    cli = cli or get_client()
    nombre = (db.get_ajuste("politicas", {}) or {}).get("proveedor_por_definir") or NOMBRE_PROVEEDOR_POR_DEFINIR
    r = cli.search_read("res.partner", [["name", "=", nombre]], ["id", "name"], limite=1)
    if r:
        return r[0]
    vals = {"name": nombre, "is_company": True, "supplier_rank": 1, "active": True,
            "comment": ("Contacto técnico de CBH · Agentes de IA: ocupa el lugar del proveedor en las solicitudes de cotización de productos "
                        "que no tienen proveedor configurado. Sustituir por el proveedor real antes de confirmar la compra.")}
    campos = cli.fields_get("res.partner")
    vals = {k: v for k, v in vals.items() if k in campos or k == "name"}
    pid = int(cli.create("res.partner", vals))
    db.log("info", "odoo", f"Contacto «{nombre}» creado para RFQ sin proveedor", f"res.partner #{pid}")
    return {"id": pid, "name": nombre}


def crear_o_actualizar_regla(producto_id: int, ubicacion_id: int, minimo: float, maximo: float,
                             multiplo: float = 1.0, cli: OdooClient | None = None) -> dict:
    cli = cli or get_client()
    existentes = cli.search_read("stock.warehouse.orderpoint",
                                 [["product_id", "=", producto_id], ["location_id", "=", ubicacion_id]],
                                 ["id", "name", "product_min_qty", "product_max_qty", "qty_multiple"], limite=1)
    vals = {"product_min_qty": float(minimo), "product_max_qty": float(maximo), "qty_multiple": float(multiplo or 1.0)}
    if existentes:
        rid = existentes[0]["id"]
        cli.write("stock.warehouse.orderpoint", [rid], vals)
        return {"modelo": "stock.warehouse.orderpoint", "id": rid, "ref": existentes[0].get("name") or str(rid),
                "estado": "actualizada", "anterior": {"product_min_qty": existentes[0]["product_min_qty"],
                                                      "product_max_qty": existentes[0]["product_max_qty"],
                                                      "qty_multiple": existentes[0].get("qty_multiple") or 1.0}}
    loc = cli.search_read("stock.location", [["id", "=", ubicacion_id]], ["warehouse_id"], limite=1)
    vals.update({"product_id": producto_id, "location_id": ubicacion_id, "trigger": "manual"})
    if loc and loc[0].get("warehouse_id"):
        vals["warehouse_id"] = loc[0]["warehouse_id"][0]
    rid = cli.create("stock.warehouse.orderpoint", vals)
    return {"modelo": "stock.warehouse.orderpoint", "id": rid, "ref": f"OP/{rid}", "estado": "creada"}


def crear_actividad(modelo: str, res_id: int, resumen: str, nota: str = "", usuario_id: int | None = None,
                    cli: OdooClient | None = None) -> dict:
    cli = cli or get_client()
    tipo = cli.search_read("mail.activity.type", [["name", "ilike", "hacer"]], ["id"], limite=1) or \
        cli.search_read("mail.activity.type", [], ["id"], limite=1)
    modelo_id = cli.search_read("ir.model", [["model", "=", modelo]], ["id"], limite=1)
    vals = {"res_model": modelo, "res_id": res_id, "summary": resumen[:200], "note": nota or False,
            "activity_type_id": tipo[0]["id"] if tipo else False}
    if modelo_id:
        vals["res_model_id"] = modelo_id[0]["id"]
    if usuario_id:
        vals["user_id"] = usuario_id
    aid = cli.create("mail.activity", vals)
    return {"modelo": "mail.activity", "id": aid, "ref": resumen[:60], "estado": "creada"}


def nota_chatter(modelo: str, res_id: int, cuerpo: str, asunto: str = "Agentes de IA · CBH",
                 cli: OdooClient | None = None) -> dict:
    cli = cli or get_client()
    mid = cli.mensaje_chatter(modelo, res_id, cuerpo, asunto)
    return {"modelo": modelo, "id": res_id, "ref": f"mensaje {mid}", "estado": "publicada"}


def revertir(modelo: str, res_id: int, cli: OdooClient | None = None, anterior: dict | None = None) -> dict:
    """Deshace lo hecho por una acción: cancela transferencias/compras; una regla MODIFICADA recupera
    sus valores anteriores y sólo se elimina si la creó el agente; las actividades se borran."""
    cli = cli or get_client()
    if modelo == "stock.warehouse.orderpoint" and anterior:
        cli.write(modelo, [res_id], {k: float(v) for k, v in anterior.items() if v is not None})
        return {"modelo": modelo, "id": res_id, "estado": "restaurada", "valores": anterior}
    if modelo == "purchase.order" and anterior and anterior.get("lineas"):
        for lid, fecha in anterior["lineas"].items():
            if fecha:
                cli.write("purchase.order.line", [int(lid)], {"date_planned": fecha})
        return {"modelo": modelo, "id": res_id, "estado": "fecha_restaurada", "valores": anterior}
    if modelo == "stock.picking":
        cli.call_button("stock.picking", [res_id], "action_cancel")
        return {"modelo": modelo, "id": res_id, "estado": "cancelada"}
    if modelo == "purchase.order":
        cli.call_button("purchase.order", [res_id], "button_cancel")
        return {"modelo": modelo, "id": res_id, "estado": "cancelada"}
    if modelo == "mail.activity" and anterior and anterior.get("ids"):
        # aviso al equipo: se retiran todas las actividades que creó (las ya marcadas como hechas ya no existen)
        ids = [int(x) for x in anterior["ids"]]
        vivas = [r["id"] for r in cli.search_read(modelo, [["id", "in", ids]], ["id"], limite=len(ids) or 1)]
        if vivas:
            cli.unlink(modelo, vivas)
        return {"modelo": modelo, "id": res_id, "estado": "eliminadas", "retiradas": len(vivas), "ya_atendidas": len(ids) - len(vivas)}
    if modelo in ("stock.warehouse.orderpoint", "mail.activity", "stock.scrap"):
        cli.unlink(modelo, [res_id])
        return {"modelo": modelo, "id": res_id, "estado": "eliminada"}
    if modelo == "helpdesk.ticket":
        try:
            cli.unlink(modelo, [res_id])
        except OdooError:
            cli.write(modelo, [res_id], {"active": False})
        return {"modelo": modelo, "id": res_id, "estado": "eliminado"}
    if modelo == "product.supplierinfo" and anterior:
        cli.write(modelo, [res_id], {"delay": float(anterior.get("delay") or 0)})
        return {"modelo": modelo, "id": res_id, "estado": "restaurado", "valores": anterior}
    raise OdooError(f"No sé revertir {modelo}.")


def estado_documento(modelo: str, res_id: int, cli: OdooClient | None = None, anterior: dict | None = None) -> dict:
    """Estado actual en Odoo de un documento creado por una acción (para seguimiento)."""
    cli = cli or get_client()
    if modelo == "stock.picking":
        r = cli.search_read(modelo, [["id", "=", res_id]], ["name", "state", "scheduled_date", "date_done"], limite=1)
        if not r:
            return {"existe": False}
        return {"existe": True, "ref": r[0]["name"], "estado": r[0]["state"], "fecha_prevista": r[0].get("scheduled_date"),
                "fecha_hecho": r[0].get("date_done"), "concluido": r[0]["state"] == "done", "cancelado": r[0]["state"] == "cancel"}
    if modelo == "purchase.order":
        r = cli.search_read(modelo, [["id", "=", res_id]], ["name", "state", "date_planned", "receipt_status"], limite=1)
        if not r:
            return {"existe": False}
        lineas = cli.search_read("purchase.order.line", [["order_id", "=", res_id]], ["product_qty", "qty_received"], limite=50)
        pedido = sum(float(l.get("product_qty") or 0) for l in lineas)
        recibido = sum(float(l.get("qty_received") or 0) for l in lineas)
        return {"existe": True, "ref": r[0]["name"], "estado": r[0]["state"], "fecha_prevista": r[0].get("date_planned"),
                "pedido": pedido, "recibido": recibido, "concluido": pedido > 0 and recibido >= pedido,
                "cancelado": r[0]["state"] == "cancel"}
    if modelo == "stock.warehouse.orderpoint":
        r = cli.search_read(modelo, [["id", "=", res_id]], ["product_min_qty", "product_max_qty"], limite=1)
        return {"existe": bool(r), "concluido": bool(r), "cancelado": not r}
    if modelo == "mail.activity":
        if anterior and anterior.get("ids"):
            # aviso al equipo: concluido cuando TODAS las personas marcaron su actividad como hecha (desaparecen)
            ids = [int(x) for x in anterior["ids"]]
            vivas = cli.search_read(modelo, [["id", "in", ids]], ["id", "user_id", "date_deadline"], limite=len(ids) or 1)
            return {"existe": True, "estado": f"{len(ids) - len(vivas)}/{len(ids)} atendidas", "concluido": not vivas, "cancelado": False,
                    "pendientes": [(v.get("user_id") or [None, ""])[1] for v in vivas],
                    "fecha_prevista": min((v.get("date_deadline") for v in vivas if v.get("date_deadline")), default=None)}
        r = cli.search_read(modelo, [["id", "=", res_id]], ["summary"], limite=1)
        return {"existe": bool(r), "concluido": not r, "cancelado": False}  # una actividad "hecha" desaparece
    if modelo == "helpdesk.ticket":
        r = cli.search_read(modelo, [["id", "=", res_id]], ["name", "stage_id", "closed_hours" if False else "kanban_state"], limite=1)
        if not r:
            return {"existe": False}
        etapa = (r[0].get("stage_id") or [None, ""])[1].lower()
        return {"existe": True, "ref": r[0]["name"], "estado": etapa, "concluido": any(k in etapa for k in ("resuelto", "cerrado", "solved", "closed", "done")),
                "cancelado": "cancel" in etapa}
    if modelo == "stock.scrap":
        r = cli.search_read(modelo, [["id", "=", res_id]], ["name", "state"], limite=1)
        return {"existe": bool(r), "estado": r[0]["state"] if r else None, "concluido": bool(r) and r[0]["state"] == "done", "cancelado": False}
    return {"existe": True, "concluido": True, "cancelado": False}


# ═══════════════════════════════════════════════════════════════════════════
#  Más escenarios de ejecución en Odoo (todos tras aprobación y reversibles cuando es posible)
# ═══════════════════════════════════════════════════════════════════════════
def _ubicacion_cuarentena(cli: OdooClient) -> dict | None:
    for patron in ("cuarentena", "quarantine", "bloqueado", "retenido"):
        r = cli.search_read("stock.location", [["complete_name", "ilike", patron]], ["id", "complete_name"], limite=1)
        if r:
            return r[0]
    return None


def _lote(nombre_o_id, producto_id: int | None, cli: OdooClient) -> dict | None:
    dominio = [["id", "=", int(nombre_o_id)]] if str(nombre_o_id).isdigit() else [["name", "=", str(nombre_o_id)]]
    if producto_id:
        dominio.append(["product_id", "=", int(producto_id)])
    r = cli.search_read("stock.lot", dominio, ["id", "name", "product_id"], limite=1)
    return r[0] if r else None


# ── aviso al equipo (sin Helpdesk): actividad «por hacer» a cada persona + nota en el chatter que las notifica ──
# El cliente no usa Helpdesk. Un aviso llega a personas concretas de Odoo: los usuarios de los grupos de Odoo del equipo
# (Contabilidad/Facturación, Inventario…) más los administradores de Agentes de IA, y/o logins fijados en Configuración.
EQUIPOS_AVISO: dict[str, dict] = {
    "facturacion": {"nombre": "Contabilidad / Facturación",
                    "grupos": ["account.group_account_manager", "account.group_account_user", "account.group_account_invoice"]},
    "calidad": {"nombre": "Calidad / Responsable sanitario", "grupos": ["stock.group_stock_manager"]},
    "operaciones": {"nombre": "Dirección de Operaciones", "grupos": ["stock.group_stock_manager"]},
    "direccion": {"nombre": "Dirección", "grupos": ["base.group_system"]},
}
GRUPO_ADMIN_AGENTES = "cbh_agentes_ia.group_admin"
_EQUIPO_ALIAS = {"facturación": "facturacion", "contabilidad": "facturacion", "contabilidad / facturación": "facturacion",
                 "calidad / responsable sanitario": "calidad", "dirección de operaciones": "operaciones", "dirección": "direccion"}


def equipo_clave(equipo: str) -> str:
    e = (equipo or "").strip().lower()
    return _EQUIPO_ALIAS.get(e, e if e in EQUIPOS_AVISO else "operaciones")


def configuracion_avisos() -> dict:
    """Destinatarios por equipo (Configuración ▸ Avisos): logins de Odoo fijos por equipo y si se incluye a los
    administradores de Agentes de IA. Los grupos de Odoo por defecto se pueden sustituir por equipo."""
    cfg = db.get_ajuste("avisos", {}) or {}
    out = {"incluir_admin_agentes": bool(cfg.get("incluir_admin_agentes", True)), "equipos": {}}
    for k, v in EQUIPOS_AVISO.items():
        e = (cfg.get("equipos") or {}).get(k) or {}
        out["equipos"][k] = {"nombre": v["nombre"], "grupos": list(e.get("grupos") or v["grupos"]),
                             "logins": [str(x).strip().lower() for x in (e.get("logins") or []) if str(x).strip()]}
    return out


def _campo_existente(cli: OdooClient, modelo: str, candidatos: list[str]) -> str | None:
    try:
        campos = cli.fields_get(modelo)
    except OdooError:
        return None
    for c in candidatos:
        if c in campos:
            return c
    return None


def _grupo_id(cli: OdooClient, xmlid: str) -> int | None:
    if "." not in xmlid:
        return None
    modulo, nombre = xmlid.split(".", 1)
    try:
        r = cli.search_read("ir.model.data", [["module", "=", modulo], ["name", "=", nombre], ["model", "=", "res.groups"]], ["res_id"], limite=1)
    except OdooError:
        return None
    return int(r[0]["res_id"]) if r else None


def destinatarios_equipo(equipo: str, cli: OdooClient | None = None, excluir_logins: list[str] | None = None,
                         incluir_logins: list[str] | None = None) -> dict:
    """Personas de Odoo que recibirán el aviso de un equipo: {"equipo", "nombre", "personas": [{id, nombre, login, partner_id}],
    "fuentes": [...], "avisos": [...]}. Nunca incluye al usuario técnico ni usuarios de portal/inactivos."""
    from ..config import settings
    cli = cli or get_client()
    clave = equipo_clave(equipo)
    cfg = configuracion_avisos()
    eq = cfg["equipos"].get(clave) or {"nombre": clave, "grupos": [], "logins": []}
    excluir = {str(x).lower() for x in (excluir_logins or [])} | {str(settings.ODOO_USER or "").lower(), "agente.ia@i-condor.com"}
    uid_tecnico = getattr(cli, "uid", None)
    campo_grupos = _campo_existente(cli, "res.users", ["all_group_ids", "group_ids", "groups_id"])
    personas: dict[int, dict] = {}
    fuentes: list[str] = []
    avisos: list[str] = []
    xmlids = list(eq["grupos"]) + ([GRUPO_ADMIN_AGENTES] if cfg["incluir_admin_agentes"] else [])
    gids: list[int] = []
    for x in xmlids:
        gid = _grupo_id(cli, x)
        if gid:
            gids.append(gid); fuentes.append(x)
        else:
            avisos.append(f"El grupo de Odoo «{x}» no existe en esta base (módulo no instalado).")
    dominios: list[list] = []
    if gids and campo_grupos:
        dominios.append([[campo_grupos, "in", gids]])
    logins = list(eq["logins"]) + [str(x).strip().lower() for x in (incluir_logins or []) if str(x).strip()]
    if logins:
        dominios.append([["login", "in", logins]]); fuentes.append("logins configurados" if eq["logins"] else "personas indicadas")
    for dom in dominios:
        try:
            rows = cli.search_read("res.users", dom + [["active", "=", True], ["share", "=", False]],
                                   ["name", "login", "partner_id", "email"], limite=200)
        except OdooError as e:
            avisos.append(f"No se pudieron leer los usuarios de Odoo: {str(e)[:160]}")
            continue
        for u in rows:
            if str(u.get("login") or "").lower() in excluir or (uid_tecnico and int(u["id"]) == int(uid_tecnico)):
                continue
            personas[int(u["id"])] = {"id": int(u["id"]), "nombre": u.get("name") or u.get("login"), "login": u.get("login"),
                                      "partner_id": int(u["partner_id"][0]) if isinstance(u.get("partner_id"), (list, tuple)) else None,
                                      "email": u.get("email") or ""}
    if not personas:
        avisos.append(f"Ningún usuario de Odoo recibiría el aviso de «{eq['nombre']}»: agrega logins en Configuración ▸ Avisos "
                      "o asigna a alguien el grupo «Agentes de IA / Administrador».")
    return {"equipo": clave, "nombre": eq["nombre"], "personas": sorted(personas.values(), key=lambda p: p["nombre"] or ""),
            "fuentes": fuentes, "avisos": avisos}


def _registro_para_aviso(cli: OdooClient, modelo: str | None, res_id: int | None, producto_id: int | None, personas: list[dict]) -> tuple[str, int] | None:
    """Dónde cuelga el aviso: el registro del caso (folio) si existe; si no, la plantilla del producto; si no, el
    contacto del primer destinatario (siempre tiene chatter y actividades)."""
    if modelo and res_id:
        try:
            if cli.search_read(modelo, [["id", "=", int(res_id)]], ["id"], limite=1):
                return modelo, int(res_id)
        except OdooError:
            pass
    if producto_id:
        try:
            p = cli.search_read("product.product", [["id", "=", int(producto_id)]], ["product_tmpl_id"], limite=1)
            if p and isinstance(p[0].get("product_tmpl_id"), (list, tuple)):
                return "product.template", int(p[0]["product_tmpl_id"][0])
        except OdooError:
            pass
    for per in personas:
        if per.get("partner_id"):
            return "res.partner", int(per["partner_id"])
    return None


def enviar_aviso_equipo(equipo: str, asunto: str, cuerpo: str, destinatarios: list[dict] | None = None, modelo: str | None = None,
                        res_id: int | None = None, producto_id: int | None = None, plazo_dias: int = 5, referencia: str = "",
                        cli: OdooClient | None = None) -> dict:
    """Aviso a personas concretas de Odoo: una actividad «Por hacer» asignada a cada una (aparece en sus actividades y en
    su bandeja) y una nota en el chatter del registro que las menciona (notificación en Odoo y correo según su preferencia).
    Idempotente por referencia: si ya existen actividades con esta referencia, se reutilizan."""
    from datetime import date, timedelta
    cli = cli or get_client()
    if destinatarios is None:
        destinatarios = destinatarios_equipo(equipo, cli)["personas"]
    if not destinatarios:
        raise OdooError("No hay destinatarios en Odoo para este aviso (Configuración ▸ Avisos).")
    previas = cli.search_read("mail.activity", [["note", "ilike", referencia]], ["id", "user_id"], limite=50) if referencia else []
    if previas:
        return {"modelo": "mail.activity", "id": previas[0]["id"], "ref": f"aviso a {len(previas)} persona(s)", "estado": "existente",
                "reutilizado": True, "anterior": {"ids": [p["id"] for p in previas]}, "destinatarios": [p.get("nombre") for p in destinatarios]}
    destino = _registro_para_aviso(cli, modelo, res_id, producto_id, destinatarios)
    if not destino:
        raise OdooError("No hay un registro de Odoo donde colgar el aviso.")
    modelo_d, id_d = destino
    tipo = cli.search_read("mail.activity.type", [["name", "ilike", "hacer"]], ["id"], limite=1) or \
        cli.search_read("mail.activity.type", [], ["id"], limite=1)
    modelo_id = cli.search_read("ir.model", [["model", "=", modelo_d]], ["id"], limite=1)
    nota = f"{cuerpo}\n\n{referencia}".strip().replace("\n", "<br>")
    ids: list[int] = []
    for per in destinatarios:
        vals: dict[str, Any] = {"res_model": modelo_d, "res_id": id_d, "summary": asunto[:200], "note": nota,
                                "user_id": int(per["id"]), "date_deadline": (date.today() + timedelta(days=int(plazo_dias))).isoformat(),
                                "activity_type_id": tipo[0]["id"] if tipo else False}
        if modelo_id:
            vals["res_model_id"] = modelo_id[0]["id"]
        ids.append(int(cli.create("mail.activity", vals)))
    partners = [int(p["partner_id"]) for p in destinatarios if p.get("partner_id")]
    try:
        cli.execute(modelo_d, "message_post", [[id_d]], body=f"<b>{asunto}</b><br>{nota}", subject=asunto[:120], message_type="comment",
                    subtype_xmlid="mail.mt_note", partner_ids=partners)
        notificados = len(partners)
    except OdooError as e:
        db.log("warn", "odoo", "Aviso: las actividades se crearon pero la nota del chatter falló", str(e))
        notificados = 0
    return {"modelo": "mail.activity", "id": ids[0], "ref": f"aviso a {len(ids)} persona(s)", "estado": "enviado",
            "registro": {"modelo": modelo_d, "id": id_d}, "anterior": {"ids": ids}, "notificados_chatter": notificados,
            "destinatarios": [p.get("nombre") for p in destinatarios]}


def crear_ticket_helpdesk(titulo: str, descripcion: str, equipo: str = "", prioridad: str = "2",
                          referencia: str = "", cli: OdooClient | None = None) -> dict:
    """Ticket de Helpdesk para dar seguimiento a un caso (investigación, conciliación, aclaración a Contabilidad)."""
    cli = cli or get_client()
    if not cli.existe_modelo("helpdesk.ticket"):
        raise OdooError("Helpdesk no está instalado en esta base.")
    previo = cli.search_read("helpdesk.ticket", [["name", "=", titulo]] + ([["description", "ilike", referencia]] if referencia else []),
                             ["id", "name"], limite=1)
    if previo:
        return {"modelo": "helpdesk.ticket", "id": previo[0]["id"], "ref": previo[0]["name"], "estado": "existente", "reutilizado": True}
    vals: dict[str, Any] = {"name": titulo[:120], "description": f"{descripcion}\n\n{referencia}".strip(), "priority": str(prioridad)}
    if equipo:
        eq = cli.search_read("helpdesk.team", [["name", "ilike", equipo]], ["id"], limite=1)
        if eq:
            vals["team_id"] = eq[0]["id"]
    tid = cli.create("helpdesk.ticket", vals)
    return {"modelo": "helpdesk.ticket", "id": tid, "ref": f"Ticket {tid}", "estado": "abierto"}


def cuarentena_lote(producto_id: int, lote: str, cantidad: float, origen_id: int, referencia: str = "", nota: str = "",
                    cli: OdooClient | None = None) -> dict:
    """Bloquea un lote: transferencia interna del lote a Cuarentena CONFIRMADA y RESERVADA. El bloqueo real es la
    reserva (la cantidad reservada no está disponible para otros movimientos); se verifica y se informa cuánto
    quedó bloqueado de verdad. Un borrador sin reservar NO bloquea nada y así se reporta."""
    cli = cli or get_client()
    cuar = _ubicacion_cuarentena(cli)
    if not cuar:
        raise OdooError("No existe una ubicación de cuarentena en Odoo (crear una ubicación interna «Cuarentena»).")
    previo = _existente_por_referencia("stock.picking", referencia, cli)
    tipo = tipo_operacion("internal", origen_id, cli)
    prod = cli.search_read("product.product", [["id", "=", producto_id]], ["display_name", "uom_id", "tracking"], limite=1)
    if not tipo or not prod:
        raise OdooError("Falta tipo de operación interna o producto.")
    lot = _lote(lote, producto_id, cli)
    if prod[0].get("tracking") in ("lot", "serial") and not lot:
        raise OdooError(f"El lote «{lote}» no existe para {prod[0]['display_name']}; no se puede bloquear un lote inexistente.")
    if previo:
        pid, ref_nombre, recuperado = previo["id"], previo["name"], True
    else:
        from .queries import campo_uom
        move = {"name": f"Cuarentena {prod[0]['display_name']} lote {lote}", "product_id": producto_id, "product_uom_qty": float(cantidad),
                campo_uom("stock.move", cli): prod[0]["uom_id"][0] if prod[0].get("uom_id") else False, "location_id": origen_id, "location_dest_id": cuar["id"]}
        if lot:
            move["lot_ids"] = [(6, 0, [lot["id"]])]
        vals = {"picking_type_id": tipo["id"], "location_id": origen_id, "location_dest_id": cuar["id"], "origin": referencia,
                "note": nota or f"Lote {lote} enviado a cuarentena por los Agentes de IA", "move_ids_without_package": [(0, 0, move)]}
        pid, recuperado = _crear_con_referencia("stock.picking", vals, referencia, cli)
        r = cli.search_read("stock.picking", [["id", "=", pid]], ["name"], limite=1)
        ref_nombre = r[0]["name"] if r else str(pid)
    # confirmar y reservar (el bloqueo real); si algo falla, el documento existe y se informa con advertencia
    advertencia = None
    try:
        est = cli.search_read("stock.picking", [["id", "=", pid]], ["state"], limite=1)
        if est and est[0]["state"] == "draft":
            cli.call_button("stock.picking", [pid], "action_confirm")
        cli.call_button("stock.picking", [pid], "action_assign")
    except OdooError as e:
        advertencia = f"Se creó {ref_nombre} pero no se pudo reservar: {e}"
    est = cli.search_read("stock.picking", [["id", "=", pid]], ["state"], limite=1)
    estado = est[0]["state"] if est else "draft"
    reservado = 0.0
    try:
        for ml in cli.search_read("stock.move.line", [["picking_id", "=", pid]], ["quantity", "reserved_uom_qty", "lot_id"], limite=50):
            reservado += float(ml.get("quantity") or ml.get("reserved_uom_qty") or 0)
    except OdooError:
        pass
    bloqueado = estado == "assigned" and reservado + 1e-6 >= float(cantidad)
    if not bloqueado and not advertencia:
        advertencia = (f"{ref_nombre} quedó en estado «{estado}» con {reservado:g} reservados de {float(cantidad):g}: el lote NO está "
                       f"totalmente bloqueado (existencia insuficiente o ya reservada). Revisar en Odoo.")
    return {"modelo": "stock.picking", "id": pid, "ref": ref_nombre, "estado": estado, "cuarentena": cuar["complete_name"],
            "reutilizado": recuperado, "bloqueado": bloqueado, "reservado": reservado, "cantidad_base": float(cantidad), "advertencia": advertencia}


def desechar_lote(producto_id: int, lote: str, cantidad: float, ubicacion_id: int, motivo: str = "", cli: OdooClient | None = None) -> dict:
    """Orden de desecho (stock.scrap) en borrador para un lote caducado; se valida manualmente en Odoo."""
    cli = cli or get_client()
    lot = _lote(lote, producto_id, cli)
    prod_t = cli.search_read("product.product", [["id", "=", producto_id]], ["tracking"], limite=1)
    if prod_t and prod_t[0].get("tracking") in ("lot", "serial") and not lot:
        raise OdooError(f"El lote «{lote}» no existe para el producto; no se genera un desecho sin lote.")
    vals: dict[str, Any] = {"product_id": producto_id, "scrap_qty": float(cantidad), "location_id": ubicacion_id, "origin": motivo[:120]}
    if lot:
        vals["lot_id"] = lot["id"]
    prod = cli.search_read("product.product", [["id", "=", producto_id]], ["uom_id"], limite=1)
    if prod and prod[0].get("uom_id"):
        vals["product_uom_id"] = prod[0]["uom_id"][0]
    sid = cli.create("stock.scrap", vals)
    return {"modelo": "stock.scrap", "id": sid, "ref": f"Desecho {sid}", "estado": "draft", "cantidad_base": float(cantidad),
            "advertencia": "El desecho queda en borrador: no descuenta existencias hasta validarlo en Odoo."}


def reprogramar_compra(orden_ref: str, nueva_fecha: str, nota: str = "", cli: OdooClient | None = None) -> dict:
    """Actualiza la fecha prevista de una orden de compra retrasada y deja constancia en el chatter."""
    cli = cli or get_client()
    po = cli.search_read("purchase.order", [["name", "=", orden_ref]], ["id", "date_planned"], limite=1)
    if not po:
        raise OdooError(f"Orden de compra {orden_ref} no encontrada.")
    anterior = po[0].get("date_planned")
    lineas = cli.search_read("purchase.order.line", [["order_id", "=", po[0]["id"]]], ["id", "date_planned"], limite=200)
    cli.write("purchase.order.line", [l["id"] for l in lineas], {"date_planned": nueva_fecha})
    cli.mensaje_chatter("purchase.order", po[0]["id"], nota or f"Fecha prevista reprogramada a {nueva_fecha} por los Agentes de IA.")
    return {"modelo": "purchase.order", "id": po[0]["id"], "ref": orden_ref, "estado": "reprogramada",
            "anterior": {"date_planned": anterior, "lineas": {l["id"]: l.get("date_planned") for l in lineas}}}


def recordatorio_proveedor(orden_ref: str, cuerpo: str, cli: OdooClient | None = None) -> dict:
    """Mensaje en la orden de compra (visible para compras y, si se envía, para el proveedor)."""
    cli = cli or get_client()
    po = cli.search_read("purchase.order", [["name", "=", orden_ref]], ["id"], limite=1)
    if not po:
        raise OdooError(f"Orden de compra {orden_ref} no encontrada.")
    mid = cli.mensaje_chatter("purchase.order", po[0]["id"], cuerpo, "Recordatorio de entrega · Agentes de IA")
    return {"modelo": "purchase.order", "id": po[0]["id"], "ref": orden_ref, "estado": "recordatorio_publicado", "mensaje_id": mid}


def confirmar_transferencia(picking_ref: str, cli: OdooClient | None = None) -> dict:
    """Confirma y reserva una transferencia pendiente en borrador (p. ej. un resurtido creado antes)."""
    cli = cli or get_client()
    pk = cli.search_read("stock.picking", [["name", "=", picking_ref]], ["id", "state"], limite=1)
    if not pk:
        raise OdooError(f"Transferencia {picking_ref} no encontrada.")
    if pk[0]["state"] in ("done", "cancel"):
        raise OdooError(f"{picking_ref} ya está en estado «{pk[0]['state']}»; no se puede confirmar.")
    if pk[0]["state"] == "draft":
        cli.call_button("stock.picking", [pk[0]["id"]], "action_confirm")
    advertencia = None
    try:
        cli.call_button("stock.picking", [pk[0]["id"]], "action_assign")
    except OdooError as e:
        advertencia = f"Confirmada pero sin reservar: {e}"
    est = cli.search_read("stock.picking", [["id", "=", pk[0]["id"]]], ["state"], limite=1)
    estado = est[0]["state"] if est else "confirmed"
    if estado == "draft":
        raise OdooError(f"Odoo no confirmó {picking_ref} (sigue en borrador).")
    if estado != "assigned" and not advertencia:
        advertencia = f"{picking_ref} quedó «{estado}»: sin existencia suficiente para reservar todo."
    return {"modelo": "stock.picking", "id": pk[0]["id"], "ref": picking_ref, "estado": estado, "advertencia": advertencia}


def validar_recepcion(picking_ref: str, cli: OdooClient | None = None) -> dict:
    """Valida una recepción/transferencia LISTA (mueve existencias de forma definitiva). Antes de tocar nada comprueba
    que el documento esté en estado listo, que todas las líneas tengan cantidad capturada y lote cuando el producto lo
    exige, y que la cantidad capturada coincida con la demandada (si no, sería una entrega parcial: decisión humana).
    Si Odoo devuelve un asistente (backorder, entrega inmediata, SMS…) NO se contesta por suposición. El éxito sólo
    se declara si, tras la llamada, el estado real es «done»."""
    cli = cli or get_client()
    pk = cli.search_read("stock.picking", [["name", "=", picking_ref]], ["id", "state"], limite=1)
    if not pk:
        raise OdooError(f"Documento {picking_ref} no encontrado.")
    pid, estado = pk[0]["id"], pk[0]["state"]
    if estado == "done":
        return {"modelo": "stock.picking", "id": pid, "ref": picking_ref, "estado": "done", "reutilizado": True}
    if estado != "assigned":
        raise RequiereDecision(f"{picking_ref} está en estado «{estado}», no «listo»: hay que reservar/completar en Odoo antes de validar.")
    moves = cli.search_read("stock.move", [["picking_id", "=", pid]], ["product_id", "product_uom_qty", "quantity", "state"], limite=200)
    faltas = []
    for m in moves:
        pedido, hecho = float(m.get("product_uom_qty") or 0), float(m.get("quantity") or 0)
        nombre = (m.get("product_id") or [None, "?"])[1]
        if hecho <= 0:
            faltas.append(f"{nombre}: sin cantidad capturada")
        elif abs(hecho - pedido) > 1e-6:
            faltas.append(f"{nombre}: capturado {hecho:g} de {pedido:g} (entrega parcial)")
        try:
            prod = cli.search_read("product.product", [["id", "=", m["product_id"][0]]], ["tracking"], limite=1)
            if prod and prod[0].get("tracking") in ("lot", "serial"):
                mls = cli.search_read("stock.move.line", [["move_id", "=", m["id"]]], ["lot_id", "lot_name", "quantity"], limite=100)
                if not mls or any(not (ml.get("lot_id") or ml.get("lot_name")) for ml in mls if float(ml.get("quantity") or 0) > 0):
                    faltas.append(f"{nombre}: falta el lote en la captura")
        except OdooError:
            pass
    if faltas:
        raise RequiereDecision(f"{picking_ref} no se puede validar automáticamente: " + "; ".join(faltas) + ". Completar en Odoo.")
    res = cli.call_button("stock.picking", [pid], "button_validate")
    if isinstance(res, dict) and (res.get("res_model") or res.get("type") == "ir.actions.act_window"):
        raise RequiereDecision(f"Odoo pide una decisión para validar {picking_ref} ({res.get('res_model') or res.get('name')}): "
                               "entrega parcial/backorder u otra confirmación; se deja a la operación.")
    est = cli.search_read("stock.picking", [["id", "=", pid]], ["state", "date_done"], limite=1)
    final = est[0]["state"] if est else None
    if final != "done":
        raise OdooError(f"Odoo no validó {picking_ref}: quedó en estado «{final}».")
    return {"modelo": "stock.picking", "id": pid, "ref": picking_ref, "estado": "done", "fecha_hecho": est[0].get("date_done")}


def ajustar_lead_time_proveedor(producto_id: int, dias: float, cli: OdooClient | None = None) -> dict:
    """Corrige el plazo del proveedor principal cuando la realidad lo contradice (reversible)."""
    cli = cli or get_client()
    info = proveedor_de(producto_id, cli)
    if not info:
        raise OdooError("El producto no tiene proveedor configurado.")
    anterior = info.get("delay")
    cli.write("product.supplierinfo", [info["id"]], {"delay": float(dias)})
    return {"modelo": "product.supplierinfo", "id": info["id"], "ref": f"{info['partner_id'][1] if info.get('partner_id') else ''} · {dias:g} días",
            "estado": "actualizado", "anterior": {"delay": anterior}}


def solicitar_conteo(producto_id: int, ubicacion_id: int, motivo: str = "", cli: OdooClient | None = None) -> dict:
    """Pide un conteo físico: marca los quants con fecha de inventario hoy y crea una actividad en la ubicación."""
    cli = cli or get_client()
    from datetime import date
    quants = cli.search_read("stock.quant", [["product_id", "=", producto_id], ["location_id", "=", ubicacion_id]], ["id", "inventory_date"], limite=100)
    anterior = {q["id"]: q.get("inventory_date") for q in quants}
    if quants:
        try:
            cli.write("stock.quant", [q["id"] for q in quants], {"inventory_date": date.today().isoformat()})
        except OdooError:
            pass
    act = crear_actividad("stock.location", ubicacion_id, f"Conteo físico solicitado por Agentes de IA", motivo, cli=cli)
    return {"modelo": "mail.activity", "id": act["id"], "ref": f"Conteo · {len(quants)} quants", "estado": "solicitado",
            "anterior": {"inventory_date": anterior}}



# ── correo electrónico (por el servidor de correo de Odoo, con aprobación) ─────────────────────────────────────────────
def resolver_destinatarios_correo(destinatarios: list[str], cli: OdooClient | None = None) -> dict:
    """Convierte nombres, logins o correos en direcciones reales de Odoo: usuarios internos (por nombre o login) y contactos
    (por nombre). Devuelve {"correos": [...], "no_resueltos": [...], "detalle": [{entrada, correo, nombre}]}."""
    cli = cli or get_client()
    correos, no_res, detalle = [], [], []
    for d in destinatarios or []:
        d = str(d).strip()
        if not d:
            continue
        if "@" in d and " " not in d:
            u = cli.search_read("res.users", [["login", "=", d.lower()]], ["name", "email"], limite=1)
            correo = (u[0].get("email") or d) if u else d
            correos.append(correo); detalle.append({"entrada": d, "correo": correo, "nombre": u[0]["name"] if u else ""})
            continue
        u = cli.search_read("res.users", ["|", ["name", "ilike", d], ["login", "ilike", d]], ["name", "email", "login"], limite=3)
        if len(u) == 1 and (u[0].get("email") or u[0].get("login")):
            correo = u[0].get("email") or u[0]["login"]
            correos.append(correo); detalle.append({"entrada": d, "correo": correo, "nombre": u[0]["name"]}); continue
        pa = cli.search_read("res.partner", [["name", "ilike", d], ["email", "!=", False]], ["name", "email"], limite=3)
        if len(pa) == 1:
            correos.append(pa[0]["email"]); detalle.append({"entrada": d, "correo": pa[0]["email"], "nombre": pa[0]["name"]}); continue
        no_res.append(d + (f" (ambiguo: {', '.join(x['name'] for x in (u or pa))})" if (u or pa) else " (no encontrado o sin correo)"))
    return {"correos": list(dict.fromkeys(correos)), "no_resueltos": no_res, "detalle": detalle}


def enviar_correo(para: list[str], asunto: str, cuerpo_html: str, referencia: str = "", adjunto: dict | None = None,
                  cli: OdooClient | None = None) -> dict:
    """Envía un correo por el servidor de correo saliente de Odoo (mail.mail): así usa el remitente, la firma y la bitácora
    de correo del cliente. ``adjunto`` = {"nombre", "ruta"} (p. ej. un Excel generado por la plataforma). Idempotente por
    referencia (la referencia va oculta al pie del cuerpo)."""
    import base64
    cli = cli or get_client()
    if referencia:
        previo = cli.search_read("mail.mail", [["body_html", "ilike", referencia]], ["id", "subject", "state"], limite=1)
        if previo:
            return {"modelo": "mail.mail", "id": previo[0]["id"], "ref": f"correo «{previo[0].get('subject')}»", "estado": previo[0].get("state"),
                    "reutilizado": True}
    if not para:
        raise OdooError("No hay destinatarios con correo.")
    cuerpo = cuerpo_html if "<" in cuerpo_html else cuerpo_html.replace("\n", "<br>")
    if referencia:
        cuerpo += f'<p style="color:#999;font-size:11px">{referencia}</p>'
    vals: dict[str, Any] = {"subject": asunto[:200], "body_html": cuerpo, "email_to": ", ".join(para), "auto_delete": False}
    if adjunto and adjunto.get("ruta"):
        try:
            with open(adjunto["ruta"], "rb") as f:
                datos = base64.b64encode(f.read()).decode()
            att = cli.create("ir.attachment", {"name": adjunto.get("nombre") or "adjunto", "datas": datos, "res_model": "mail.mail", "res_id": 0,
                                               "mimetype": adjunto.get("mimetype") or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
            vals["attachment_ids"] = [(6, 0, [int(att)])]
        except (OSError, OdooError) as e:
            db.log("warn", "odoo", "No se pudo adjuntar el archivo al correo", str(e))
    mid = int(cli.create("mail.mail", vals))
    try:
        cli.execute("mail.mail", "send", [[mid]])
    except OdooError as e:
        # el correo quedó en la cola de Odoo (lo enviará el cron de correo); se informa sin fingir envío inmediato
        return {"modelo": "mail.mail", "id": mid, "ref": f"correo «{asunto[:60]}»", "estado": "en_cola", "destinatarios": para,
                "advertencia": f"El correo quedó en la cola de Odoo (se enviará con el siguiente ciclo de correo): {str(e)[:160]}"}
    est = cli.search_read("mail.mail", [["id", "=", mid]], ["state"], limite=1)
    estado = (est[0].get("state") if est else "sent") or "sent"
    out = {"modelo": "mail.mail", "id": mid, "ref": f"correo «{asunto[:60]}»", "estado": estado, "destinatarios": para}
    if estado == "exception":
        out["advertencia"] = "Odoo no pudo enviar el correo (revisa el servidor de correo saliente en Ajustes técnicos)."
    return out
