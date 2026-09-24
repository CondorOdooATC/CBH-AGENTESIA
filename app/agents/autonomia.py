"""Motor de autonomía con barandales.

Niveles (ajuste ``nivel_autonomia`` persistente) — NADA se escribe en Odoo sin aprobación humana:
  0 · Observa: los agentes reportan, no proponen acciones.
  1 · Propone: cada acción entra a la cola; al APROBARLA se crea en Odoo en borrador.   ← por defecto
  2 · Propone: al aprobarla se crea y se confirma (la transferencia queda lista para procesar).

Controles:
  • Políticas: tipos permitidos, topes de cantidad e importe, ubicaciones permitidas/bloqueadas,
    vigencia de una propuesta, doble aprobación para riesgo alto (dos personas distintas, la segunda admin).
  • Revalidación al aprobar: se vuelve a evaluar la política y se comprueban existencias en el origen;
    si la realidad cambió de forma material la acción pasa a «requiere_revision» con la propuesta actualizada.
  • Transiciones atómicas de estado (dos clics simultáneos no ejecutan dos veces) e idempotencia en Odoo
    (cada acción lleva una referencia única; si el documento ya existe, se reutiliza).
  • Seguimiento: las acciones ejecutadas se verifican en Odoo hasta concluir (recibido / hecho) o retrasarse.
  • Reversión: cancela lo creado; una regla modificada recupera sus valores anteriores.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from ..config import settings
from ..odoo import acciones as odoo_acciones
from ..odoo.client import OdooError, get_client

# Un solo hilo revalida-aprueba-ejecuta a la vez: las comprobaciones de existencia y el libro de compromisos
# se evalúan en serie, también en la aprobación agrupada, el copiloto y las tareas programadas.
_CANDADO = threading.RLock()

POLITICAS_DEFAULT = {
    "acciones_permitidas": ["transferencia_interna", "solicitud_compra", "regla_reabastecimiento", "actividad", "nota_chatter", "alerta",
                            "aviso_equipo", "ticket_helpdesk", "cuarentena_lote", "desechar_lote", "reprogramar_compra", "recordatorio_proveedor",
                            "confirmar_transferencia", "validar_recepcion", "ajustar_lead_time_proveedor", "solicitar_conteo", "correo"],
    # El cliente no usa Helpdesk: los seguimientos se avisan a personas (actividad + notificación). Si algún día lo usa,
    # con «usar_helpdesk» los avisos de caso se convierten en tickets.
    "usar_helpdesk": False,
    "tope_cantidad": 5000.0,
    "tope_importe": 150000.0,
    "importe_riesgo_alto": 50000.0,          # a partir de aquí la acción es de riesgo alto
    # Riesgo alto: basta UNA aprobación de un administrador (petición del cliente). Con True exigiría dos personas
    # distintas (la segunda, administrador). Se cambia en Configuración ▸ Políticas.
    "doble_aprobacion_riesgo_alto": False,
    "ubicaciones_permitidas": [],             # vacío = todas
    "ubicaciones_bloqueadas": [],
    "vigencia_propuesta_dias": 7,             # una propuesta más vieja caduca al intentar aprobarla
    "tolerancia_revalidacion_pct": 10.0,      # cambio de cantidad tolerado sin pedir nueva aprobación
    "max_acciones_por_corrida": 60,
    # Acciones cuya ejecución automática NO está validada en producción: se proponen, se revisan y se aprueban,
    # pero en producción quedan «aprobada_manual» (la operación las ejecuta en Odoo). En staging sí se ejecutan.
    "acciones_solo_propuesta": ["validar_recepcion", "confirmar_transferencia", "desechar_lote", "reprogramar_compra",
                                "ajustar_lead_time_proveedor", "cuarentena_lote"],
    "tolerancia_dedupe_pct": 2.0,             # cambio menor a esto entre corridas no genera versión nueva
    # Productos sin proveedor: la RFQ se crea igual; si Odoo exige proveedor, se usa este contacto (Compras lo sustituye)
    "proveedor_por_definir": "PROVEEDOR POR DEFINIR",
}
NIVELES = {0: "Observa", 1: "Propone → aprobación crea en borrador", 2: "Propone → aprobación crea y confirma"}
ESTADOS_PENDIENTES = ("propuesta", "aprobada_parcial", "requiere_revision")
ESTADOS_ACTIVOS = ESTADOS_PENDIENTES + ("aprobada", "ejecutando", "aprobada_manual")
ESTADOS_FINALES_ODOO = ("done", "cancel", "concluida", "cancelada")
TIPOS_CON_CANTIDAD = ("transferencia_interna", "solicitud_compra", "cuarentena_lote", "desechar_lote")

EFECTOS = {
    "transferencia_interna": ("Creará una transferencia interna en Odoo ({origen} → {destino}, {cantidad:g} {unidad}) en BORRADOR; el almacén la confirma y procesa",
                              "Creará y CONFIRMARÁ una transferencia interna en Odoo ({origen} → {destino}, {cantidad:g} {unidad}); quedará lista para procesar"),
    "solicitud_compra": ("Creará una solicitud de cotización (RFQ) en BORRADOR por {cantidad:g} {unidad} (en la unidad de compra del producto, "
                         "redondeada a unidades enteras); no compromete compra hasta confirmarla en Odoo",) * 2,
    "regla_reabastecimiento": ("Creará o MODIFICARÁ de inmediato la regla min/max de {ubicacion} (mín {minimo:g} / máx {maximo:g}); se puede revertir",) * 2,
    "actividad": ("Creará una actividad «por hacer» en el registro de Odoo (visible de inmediato)",) * 2,
    "nota_chatter": ("Publicará una nota interna en el chatter del registro (visible de inmediato)",) * 2,
    "alerta": ("Sólo registra una alerta interna en esta plataforma; no toca Odoo",) * 2,
    "aviso_equipo": ("Avisará en Odoo a {n_destinatarios:g} persona(s) de {equipo_nombre} ({destinatarios_texto}): una actividad «por hacer» "
                     "para cada una con plazo de {plazo_dias:g} días y una nota en el chatter que las notifica (en Odoo y por correo según su "
                     "preferencia). No mueve inventario ni dinero; se puede retirar",) * 2,
    "ticket_helpdesk": ("Creará un ticket de Helpdesk «{titulo_ticket}» para dar seguimiento al caso",) * 2,
    "correo": ("Enviará un correo desde el servidor de correo de Odoo a {n_destinatarios:g} destinatario(s) ({destinatarios_texto}) con el asunto "
               "«{asunto}»{adjunto_texto}. No toca inventario ni dinero; una vez enviado no se puede retirar",) * 2,
    "cuarentena_lote": ("Creará, CONFIRMARÁ y RESERVARÁ una transferencia del lote {lote} ({cantidad:g} {unidad}) hacia Cuarentena: "
                        "la cantidad reservada queda bloqueada para otros movimientos hasta validarla o cancelarla en Odoo",) * 2,
    "desechar_lote": ("Creará una orden de DESECHO en borrador del lote {lote} ({cantidad:g} {unidad}); se valida manualmente en Odoo",) * 2,
    "reprogramar_compra": ("Cambiará la fecha prevista de la compra {orden} a {nueva_fecha} y lo anotará en el chatter (reversible)",) * 2,
    "recordatorio_proveedor": ("Publicará un recordatorio de entrega en la compra {orden}",) * 2,
    "confirmar_transferencia": ("Confirmará y reservará la transferencia {picking} que hoy está en borrador",) * 2,
    "validar_recepcion": ("VALIDARÁ {picking} de forma DEFINITIVA (mueve existencias; no reversible). Antes comprueba que esté lista, con "
                          "cantidades y lotes capturados; si Odoo pide una decisión (entrega parcial, lotes) NO la toma: la deja a la operación",) * 2,
    "ajustar_lead_time_proveedor": ("Cambiará el plazo del proveedor principal del producto a {dias:g} días (reversible)",) * 2,
    "solicitar_conteo": ("Marcará los quants de {ubicacion} para conteo hoy y creará una actividad de conteo físico",) * 2,
}


def nivel() -> int:
    return max(0, min(2, int(db.get_ajuste("nivel_autonomia", 1))))


def set_nivel(n: int, usuario: str = "") -> int:
    n = max(0, min(2, int(n)))
    db.set_ajuste("nivel_autonomia", n)
    db.log("info", "autonomia", f"Nivel de autonomía cambiado a {n} · {NIVELES[n]}", usuario=usuario)
    return n


def politicas() -> dict:
    p = dict(POLITICAS_DEFAULT)
    p.update({k: v for k, v in (db.get_ajuste("politicas", {}) or {}).items() if k in POLITICAS_DEFAULT})
    return p


def set_politicas(cambios: dict, usuario: str = "") -> dict:
    p = politicas()
    p.update({k: v for k, v in cambios.items() if k in POLITICAS_DEFAULT})
    db.set_ajuste("politicas", p)
    db.log("info", "autonomia", "Políticas actualizadas", json.dumps(cambios, ensure_ascii=False), usuario)
    return p


def efecto(tipo: str, payload: dict) -> str:
    plantillas = EFECTOS.get(tipo)
    if not plantillas:
        return ""
    t = plantillas[1] if nivel() >= 2 else plantillas[0]
    vals = {"unidad": "u"}
    vals.update({k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in payload.items()})
    try:
        txt = t.format(**vals)
    except (KeyError, ValueError, TypeError):
        txt = t.replace("{", "").replace("}", "")
    if solo_propuesta(tipo):
        txt += " · En PRODUCCIÓN esta acción no se ejecuta automáticamente: al aprobarla queda «aprobada para ejecución manual» y la operación la hace en Odoo"
    return txt


def solo_propuesta(tipo: str) -> bool:
    """True si el tipo está restringido a propuesta + revisión manual en producción (sin ejecución automática)."""
    return settings.APP_ENV == "production" and tipo in (politicas().get("acciones_solo_propuesta") or [])


TIPOS_PLAN = ("transferencia_interna", "solicitud_compra", "regla_reabastecimiento")


def clave_de(tipo: str, payload: dict, titulo: str = "") -> str:
    """Identidad estable de una necesidad entre corridas: tipo + producto + origen/destino/ubicación/lote/documento.
    Para acciones sin producto ni documento (alertas, tickets, actividades) la identidad incluye el título, de modo
    que dos alertas distintas nunca se confundan."""
    import hashlib
    p = payload or {}
    partes = [tipo, str(p.get("producto_id", ""))]
    for k in ("origen", "destino", "ubicacion", "lote", "orden", "picking", "modelo", "res_id", "equipo", "caso_id"):
        if p.get(k) not in (None, ""):
            partes.append(f"{k}={p[k]}")
    if tipo not in TIPOS_PLAN and len(partes) == 2:
        base = titulo or p.get("titulo_ticket") or p.get("resumen") or p.get("cuerpo") or json.dumps(p, sort_keys=True, default=str)
        partes.append("t=" + hashlib.sha1(str(base).encode()).hexdigest()[:12])
    return "|".join(partes)


def precision_unidades() -> dict[str, int]:
    return db.get_ajuste("precision_uom", {}) or {}


def formato_cantidad(cantidad: float, unidad: str = "") -> str:
    from ..ml.pronostico import decimales_de
    dec = decimales_de(unidad, precision_unidades())
    q = float(cantidad)
    txt = f"{q:,.0f}" if abs(q - round(q)) < 1e-9 else f"{q:,.{dec}f}"
    return txt + (f" {unidad}" if unidad else "")


def titulo_para(tipo: str, payload: dict) -> str | None:
    """Una sola fuente de verdad para el título: se deriva del payload (cantidad redondeada a la unidad)."""
    p = payload or {}
    if not p.get("producto") or p.get("cantidad") is None:
        return None
    q = formato_cantidad(float(p["cantidad"]), str(p.get("unidad") or ""))
    if tipo == "transferencia_interna":
        return f"Transferir {q} de {p['producto']} · {p.get('origen', '?')} → {p.get('destino', '?')}"
    if tipo == "solicitud_compra":
        uc = f" ({formato_cantidad(p['cantidad_compra'], p.get('unidad_compra', ''))})" if p.get("cantidad_compra") else ""
        return f"Solicitud de compra: {q}{uc} de {p['producto']}"
    if tipo == "cuarentena_lote":
        return f"Cuarentena de {q} del lote {p.get('lote', '?')} · {p['producto']} en {p.get('origen', '?')}"
    if tipo == "desechar_lote":
        return f"Desecho de {q} del lote {p.get('lote', '?')} · {p['producto']}"
    return None


# ── libro de compromisos: lo que las propuestas activas ya tienen apartado ──
def compromisos_activos(excluir_id: int | None = None, refs_confirmadas: set | None = None, firmes: bool = False) -> dict:
    """Cantidades ya comprometidas por propuestas activas de los agentes, por (producto_id, ubicación):
      • «origen»: saldrán de ahí (transferencias y cuarentenas pendientes, aprobadas o creadas en Odoo en borrador);
      • «destino»: llegarán ahí (transferencias) o a la red (compras) por la misma vía.
    Una acción ya creada en Odoo deja de contarse aquí en cuanto Odoo la confirma (entonces la ve el motor como
    movimiento pendiente) o la concluye/cancela; así nada se descuenta dos veces."""
    refs_confirmadas = refs_confirmadas or set()
    out = {"origen": {}, "destino": {}, "red": {}, "detalle": []}
    # planeación: cuentan TODAS las activas (pendientes incluidas) para no proponer dos veces lo mismo;
    # aprobación/ejecución (firmes=True): sólo lo ya aprobado o creado en Odoo, porque una propuesta pendiente
    # todavía puede rechazarse y no debe bloquear a otra
    estados = ("aprobada", "ejecutando", "aprobada_manual") if firmes else ESTADOS_ACTIVOS
    filas = [a for e in estados for a in db.acciones(estado=e, limite=2000)]
    filas += [a for e in ("ejecutada", "retrasada") for a in db.acciones(estado=e, limite=2000)
              if str(a.get("estado_odoo") or "draft") not in ("confirmed", "assigned", "partially_available", "waiting", "done", "cancel", "purchase")
              and (a.get("odoo_ref") or "") not in refs_confirmadas]
    for a in filas:
        if excluir_id and a["id"] == excluir_id:
            continue
        p, pid = a.get("payload") or {}, (a.get("payload") or {}).get("producto_id")
        if pid is None or a["tipo"] not in TIPOS_CON_CANTIDAD:
            continue
        q = float(p.get("cantidad") or 0)
        if q <= 0:
            continue
        pid = int(pid)
        if a["tipo"] in ("transferencia_interna", "cuarentena_lote") and p.get("origen"):
            k = (pid, str(p["origen"]))
            out["origen"][k] = out["origen"].get(k, 0.0) + q
        if a["tipo"] == "transferencia_interna" and p.get("destino"):
            k = (pid, str(p["destino"]))
            out["destino"][k] = out["destino"].get(k, 0.0) + q
        if a["tipo"] == "solicitud_compra":
            out["red"][pid] = out["red"].get(pid, 0.0) + q
        out["detalle"].append({"id": a["id"], "tipo": a["tipo"], "estado": a["estado"], "producto_id": pid, "cantidad": q,
                               "origen": p.get("origen"), "destino": p.get("destino")})
    return out


def propuesta_pendiente_por_clave(clave: str, excluir_id: int | None = None) -> dict | None:
    for e in ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, limite=2000):
            if a.get("clave") == clave and a["id"] != excluir_id:
                return a
    return None


# ── evaluación ──────────────────────────────────────────────────────────────
def evaluar(tipo: str, payload: dict, impacto: dict | None = None) -> dict:
    """Devuelve {"permitido", "riesgo", "nivel_requerido", "motivos"}."""
    p = politicas()
    impacto = impacto or {}
    motivos: list[str] = []
    permitido = True
    if tipo not in p["acciones_permitidas"]:
        permitido = False
        motivos.append(f"El tipo de acción «{tipo}» no está permitido por política.")
    cant = float(impacto.get("cantidad") or payload.get("cantidad") or 0)
    imp = float(impacto.get("importe") or 0)
    if cant > p["tope_cantidad"]:
        permitido = False
        motivos.append(f"Cantidad {cant:,.0f} supera el tope de {p['tope_cantidad']:,.0f}.")
    if imp > p["tope_importe"]:
        permitido = False
        motivos.append(f"Importe ${imp:,.2f} supera el tope de ${p['tope_importe']:,.2f}.")
    for k in ("origen", "destino", "ubicacion"):
        u = str(payload.get(k, "") or impacto.get(k, ""))
        if u and p["ubicaciones_permitidas"] and not any(x.lower() in u.lower() for x in p["ubicaciones_permitidas"]):
            permitido = False
            motivos.append(f"La ubicación «{u}» no está en la lista permitida.")
        if u and any(x.lower() in u.lower() for x in p["ubicaciones_bloqueadas"]):
            permitido = False
            motivos.append(f"La ubicación «{u}» está bloqueada.")
    if tipo in ("alerta", "nota_chatter", "actividad", "aviso_equipo", "ticket_helpdesk", "recordatorio_proveedor", "solicitar_conteo", "correo"):
        riesgo = "bajo"
    elif tipo == "validar_recepcion":
        riesgo = "alto"
    elif imp >= p["importe_riesgo_alto"] or cant >= p["tope_cantidad"] * 0.5:
        riesgo = "alto"
    elif tipo in ("solicitud_compra", "regla_reabastecimiento", "cuarentena_lote", "desechar_lote", "reprogramar_compra",
                  "confirmar_transferencia", "ajustar_lead_time_proveedor"):
        riesgo = "medio"
    else:
        riesgo = "bajo" if imp < p["importe_riesgo_alto"] * 0.2 else "medio"
    if riesgo == "alto":
        motivos.append("Riesgo alto: " + ("requiere dos aprobaciones (la segunda de un administrador)."
                                          if p["doble_aprobacion_riesgo_alto"] else "requiere aprobación de un administrador."))
    return {"permitido": permitido, "riesgo": riesgo, "nivel_requerido": 1, "motivos": motivos}


# ── proponer ────────────────────────────────────────────────────────────────
def proponer(agente: str, tipo: str, titulo: str, payload: dict, motivo: str = "",
             impacto: dict | None = None, corrida_id: int | None = None, usuario: str = "",
             fecha_requerida: str | None = None, sincronizar: bool = True) -> dict:
    """Registra una propuesta. Si ya existe una propuesta PENDIENTE con la misma clave (misma necesidad de una corrida
    anterior), no crea otra: la actualiza (nueva versión si cambió de forma material) y la liga a esta corrida."""
    if nivel() == 0:
        return {"id": None, "estado": "omitida", "motivo": "Nivel de autonomía 0: sólo observa."}
    payload = dict(payload or {})
    impacto = dict(impacto or {})
    if payload.get("cantidad") is not None and payload.get("unidad"):
        from ..ml.pronostico import redondear
        payload["cantidad"] = redondear(float(payload["cantidad"]), str(payload["unidad"]), precision_unidades(), arriba=True)
        impacto["cantidad"] = payload["cantidad"]
        if impacto.get("costo_unit") is not None:
            impacto["importe"] = round(payload["cantidad"] * float(impacto["costo_unit"]), 2)
    if payload.get("conversion_faltante"):
        ev = {"permitido": False, "riesgo": "medio", "motivos": [f"Falta la conversión de unidades del producto ({payload.get('unidad')} → {payload.get('unidad_compra')}) en Odoo; "
                                                                 "capturarla en la ficha del producto antes de proponer la compra."]}
    else:
        ev = evaluar(tipo, payload, impacto)
    titulo = titulo_para(tipo, payload) or titulo
    clave = clave_de(tipo, payload, titulo)
    with _CANDADO:
        previa = propuesta_pendiente_por_clave(clave) if sincronizar else None
        if previa:
            tol = float(politicas().get("tolerancia_dedupe_pct", 2.0))
            q0, q1 = float((previa.get("payload") or {}).get("cantidad") or 0), float(payload.get("cantidad") or 0)
            material = (q0 and abs(q1 - q0) / q0 * 100 > tol) or (not q0 and q1) or \
                any(str((previa.get("payload") or {}).get(k)) != str(payload.get(k)) for k in ("origen", "destino", "ubicacion", "lote"))
            if material:
                version = db.modificar_propuesta(previa["id"], payload=json.dumps(db._limpio(payload), ensure_ascii=False, default=str),
                                                 impacto=json.dumps(db._limpio(impacto), ensure_ascii=False, default=str), titulo=titulo,
                                                 motivo=motivo, efecto=efecto(tipo, payload), riesgo=ev["riesgo"], ultima_corrida_id=corrida_id,
                                                 fecha_requerida=fecha_requerida, unidad=payload.get("unidad"))
                db.log("info", "autonomia", f"Propuesta #{previa['id']} actualizada por la corrida {corrida_id} (v{version})",
                       f"{q0:g} → {q1:g}", usuario)
                estado = "actualizada"
            else:
                db.actualizar_accion(previa["id"], ultima_corrida_id=corrida_id, motivo=motivo,
                                     impacto=json.dumps(db._limpio(impacto), ensure_ascii=False, default=str))
                version, estado = int(previa.get("version") or 1), "vigente"
            if not ev["permitido"] and previa["estado"] != "bloqueada":
                db.transicion_accion(previa["id"], ESTADOS_PENDIENTES, "bloqueada", error=" ".join(ev["motivos"]))
                return {"id": previa["id"], "estado": "bloqueada", "motivos": ev["motivos"], "titulo": titulo, "reutilizada": True}
            return {"id": previa["id"], "estado": estado, "riesgo": ev["riesgo"], "motivos": ev["motivos"], "titulo": titulo,
                    "version": version, "reutilizada": True}
        aid = db.proponer_accion(agente, tipo, titulo, payload, motivo, impacto, ev["riesgo"], 1, corrida_id)
        db.actualizar_accion(aid, efecto=efecto(tipo, payload), referencia=f"Agente IA · acción #{aid}",
                             fecha_requerida=fecha_requerida, clave=clave, ultima_corrida_id=corrida_id, unidad=payload.get("unidad"))
    if not ev["permitido"]:
        db.actualizar_accion(aid, estado="bloqueada", error=" ".join(ev["motivos"]))
        db.log("warn", "autonomia", f"Acción bloqueada por política: {titulo}", " ".join(ev["motivos"]))
        return {"id": aid, "estado": "bloqueada", "motivos": ev["motivos"], "titulo": titulo}
    db.log("info", "autonomia", f"Acción propuesta: {titulo}", f"riesgo={ev['riesgo']} tipo={tipo}", usuario)
    return {"id": aid, "estado": "propuesta", "riesgo": ev["riesgo"], "motivos": ev["motivos"], "titulo": titulo, "version": 1}


def depurar_duplicadas(agente: str | None = None) -> int:
    """Si dos propuestas pendientes comparten clave (p. ej. creadas antes de la deduplicación), se conserva la más
    reciente y las demás se caducan como duplicadas; así la cola nunca muestra dos veces la misma necesidad."""
    por_clave: dict[str, list[dict]] = {}
    for e in ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, agente=agente, limite=5000):
            if a.get("clave") and a["tipo"] in TIPOS_PLAN:
                por_clave.setdefault(a["clave"], []).append(a)
    n = 0
    for clave, lista in por_clave.items():
        if len(lista) < 2:
            continue
        lista.sort(key=lambda a: a["id"])
        vigente = lista[-1]
        for a in lista[:-1]:
            if db.transicion_accion(a["id"], a["estado"], "caducada", revalidacion=f"Duplicada de la propuesta #{vigente['id']} (misma necesidad)."):
                n += 1
    if n:
        db.log("info", "autonomia", f"{n} propuestas duplicadas caducadas", agente or "")
    return n


def caducar_no_vigentes(agente: str, corrida_id: int, claves_vigentes: set[str], tipos: tuple = ("transferencia_interna", "solicitud_compra", "regla_reabastecimiento")) -> int:
    """Propuestas pendientes de corridas anteriores cuya necesidad ya no existe en la corrida actual → caducadas
    (así una agenda que se movió o se canceló no deja propuestas huérfanas ni necesidades duplicadas)."""
    n = 0
    for e in ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, agente=agente, limite=2000):
            if a["tipo"] in tipos and a.get("clave") and a["clave"] not in claves_vigentes and a.get("ultima_corrida_id") != corrida_id:
                if db.transicion_accion(a["id"], e, "caducada", revalidacion=f"La corrida #{corrida_id} ya no requiere esta acción (la necesidad desapareció o cambió de fecha)."):
                    n += 1
    if n:
        db.log("info", "autonomia", f"{n} propuestas caducadas por la corrida #{corrida_id}", agente)
    return n


# ── revalidación al aprobar ─────────────────────────────────────────────────
def revalidar(a: dict) -> dict:
    """Comprueba que la propuesta siga siendo válida hoy. Devuelve {"ok", "motivo", "payload"}."""
    p = politicas()
    creado = datetime.fromisoformat(a["creado_en"])
    if creado.tzinfo is None:
        creado = creado.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - creado > timedelta(days=int(p["vigencia_propuesta_dias"])):
        return {"ok": False, "estado": "caducada", "motivo": f"La propuesta tiene más de {p['vigencia_propuesta_dias']} días; "
                                                              "vuelve a ejecutar el agente para una propuesta vigente."}
    ev = evaluar(a["tipo"], a["payload"], a["impacto"])
    if not ev["permitido"]:
        return {"ok": False, "estado": "bloqueada", "motivo": " ".join(ev["motivos"])}
    payload = dict(a["payload"])
    if a["tipo"] in ("transferencia_interna", "cuarentena_lote"):
        try:
            cli = get_client()
            dominio = [["product_id", "=", int(payload["producto_id"])], ["location_id", "=", int(payload["origen_id"])]]
            if a["tipo"] == "cuarentena_lote" and payload.get("lote"):
                dominio.append(["lot_id.name", "=", str(payload["lote"])])
            q = cli.search_read("stock.quant", dominio, ["quantity", "reserved_quantity"], limite=200)
            disponible_odoo = sum(float(x.get("quantity") or 0) - float(x.get("reserved_quantity") or 0) for x in q)
        except (OdooError, KeyError, ValueError, TypeError) as e:
            return {"ok": False, "estado": "requiere_revision", "motivo": f"No se pudo comprobar la existencia en el origen: {e}"}
        # lo que otras propuestas activas ya apartaron de este origen (borradores del agente incluidos)
        comprometido = float(compromisos_activos(excluir_id=a["id"], firmes=True)["origen"].get((int(payload["producto_id"]), str(payload.get("origen"))), 0.0))
        disponible = disponible_odoo - comprometido
        cant = float(payload["cantidad"])
        unidad = str(payload.get("unidad") or "")
        if disponible <= 0:
            return {"ok": False, "estado": "requiere_revision",
                    "motivo": f"El origen {payload.get('origen', '')} no tiene existencia disponible para esta acción "
                              f"({disponible_odoo:g} en Odoo, {comprometido:g} ya comprometidos en otras propuestas)."}
        if disponible < cant:
            from ..ml.pronostico import redondear
            cambio = 100 * (cant - disponible) / cant
            payload["cantidad"] = redondear(disponible, unidad, precision_unidades(), abajo=True)
            if cambio > float(p["tolerancia_revalidacion_pct"]) or payload["cantidad"] <= 0:
                return {"ok": False, "estado": "requiere_revision", "payload": payload,
                        "motivo": f"En el origen sólo hay {disponible:g} {unidad} disponibles de {cant:g} propuestos "
                                  f"({disponible_odoo:g} en Odoo menos {comprometido:g} comprometidos; {cambio:.0f} % menos). "
                                  f"La propuesta se actualizó a {payload['cantidad']:g}; requiere nueva aprobación."}
            return {"ok": True, "payload": payload, "motivo": f"Cantidad ajustada a {payload['cantidad']:g} {unidad} (disponible en origen tras otros compromisos)."}
    if a["tipo"] == "solicitud_compra":
        try:
            cli = get_client()
            desde = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
            abiertas = cli.search_read("purchase.order.line",
                                       [["product_id", "=", int(payload["producto_id"])], ["state", "in", ["draft", "sent", "to approve", "purchase"]],
                                        ["create_date", ">=", desde], ["order_id.origin", "ilike", "Agente IA"]],
                                       ["order_id", "product_qty"], limite=20)
            if abiertas:
                return {"ok": False, "estado": "requiere_revision",
                        "motivo": f"Ya existe una solicitud de compra reciente creada por los agentes para este producto "
                                  f"({abiertas[0]['order_id'][1] if abiertas[0].get('order_id') else ''}). Revisa antes de duplicar."}
        except (OdooError, KeyError, ValueError):
            pass
    return {"ok": True, "payload": payload, "motivo": ""}


# ── aprobar / rechazar / ejecutar / revertir ────────────────────────────────
def aprobar(accion_id: int, usuario: str, rol: str = "consulta", confirmar: bool | None = None) -> dict:
    with _CANDADO:
        return _aprobar(accion_id, usuario, rol, confirmar)


def _aprobar(accion_id: int, usuario: str, rol: str, confirmar: bool | None) -> dict:
    a = db.accion(accion_id)
    if not a:
        raise ValueError("Acción no encontrada.")
    if rol not in ("operacion", "admin", "condor"):
        return {"id": accion_id, "estado": a["estado"], "mensaje": "Tu rol no permite aprobar acciones."}
    if a["estado"] not in ("propuesta", "aprobada_parcial", "requiere_revision"):
        return {"id": accion_id, "estado": a["estado"], "mensaje": "La acción ya no está pendiente."}
    p = politicas()

    # ── riesgo alto: dos personas distintas; la segunda debe ser admin ──
    if a["riesgo"] == "alto":
        if p["doble_aprobacion_riesgo_alto"]:
            if a["estado"] in ("propuesta", "requiere_revision"):
                if not db.transicion_accion(accion_id, a["estado"], "aprobada_parcial", aprobado_por=usuario, aprobado_en=db.now()):
                    return {"id": accion_id, "estado": "propuesta", "mensaje": "Otra persona ya actuó sobre esta acción."}
                db.log("info", "autonomia", f"Acción #{accion_id}: primera aprobación", a["titulo"], usuario)
                return {"id": accion_id, "estado": "aprobada_parcial",
                        "mensaje": "Primera aprobación registrada. Falta la segunda, de un administrador distinto."}
            if a["estado"] == "aprobada_parcial":
                if a["aprobado_por"] == usuario:
                    return {"id": accion_id, "estado": "aprobada_parcial", "mensaje": "La segunda aprobación debe darla otra persona."}
                if rol not in ("admin", "condor"):
                    return {"id": accion_id, "estado": "aprobada_parcial", "mensaje": "La segunda aprobación debe darla un administrador."}
        elif rol not in ("admin", "condor"):
            return {"id": accion_id, "estado": a["estado"], "mensaje": "Riesgo alto: sólo un administrador puede aprobarla."}

    # ── revalidar contra la realidad actual ──
    rv = revalidar(a)
    if not rv["ok"]:
        campos = {"revalidacion": rv["motivo"]}
        if rv.get("payload"):
            campos["payload"] = json.dumps(rv["payload"], ensure_ascii=False, default=str)
            campos["efecto"] = efecto(a["tipo"], rv["payload"])
        if rv["estado"] in ("caducada", "bloqueada"):
            campos["error"] = rv["motivo"]
        db.transicion_accion(accion_id, a["estado"], rv["estado"], **campos)
        db.log("warn", "autonomia", f"Acción #{accion_id} no ejecutada: {rv['estado']}", rv["motivo"], usuario)
        return {"id": accion_id, "estado": rv["estado"], "mensaje": rv["motivo"]}
    version = int(a.get("version") or 1)
    campos: dict[str, Any] = {}
    if rv.get("payload") and rv["payload"] != a["payload"]:
        # ajuste tolerado (≤ tolerancia, siempre a la baja): queda como versión nueva y es la que se aprueba
        version += 1
        campos.update(payload=json.dumps(rv["payload"], ensure_ascii=False, default=str), efecto=efecto(a["tipo"], rv["payload"]),
                      revalidacion=rv["motivo"], version=version, titulo=titulo_para(a["tipo"], rv["payload"]) or a["titulo"])
        a["payload"] = rv["payload"]
    if solo_propuesta(a["tipo"]):
        campos.update(revalidacion=(rv.get("motivo") or "") + " Acción restringida en producción: aprobada para ejecución MANUAL en Odoo (no se ejecuta automáticamente).")

    # ── transición atómica a aprobada (ligada a la versión exacta y al efecto concreto de la propuesta) ──
    campos.update({"version_aprobada": version, "efecto_aprobado": efecto(a["tipo"], a["payload"]),
                   "cantidad_aprobada": a["payload"].get("cantidad"), "importe_aprobado": (a.get("impacto") or {}).get("importe")})
    if a["estado"] == "aprobada_parcial":
        campos.update({"aprobado_por_2": usuario, "aprobado_en_2": db.now()})
    else:
        campos.update({"aprobado_por": usuario, "aprobado_en": db.now()})
    destino = "aprobada_manual" if solo_propuesta(a["tipo"]) else "aprobada"
    if not db.transicion_accion(accion_id, a["estado"], destino, **campos):
        return {"id": accion_id, "estado": (db.accion(accion_id) or {}).get("estado"), "mensaje": "Otra persona ya actuó sobre esta acción."}
    db.log("info", "autonomia", f"Acción #{accion_id} {destino.replace('_', ' ')}", a["titulo"], usuario)
    if destino == "aprobada_manual":
        return {"id": accion_id, "estado": "aprobada_manual",
                "mensaje": "Aprobada para ejecución manual: esta acción no se ejecuta automáticamente en producción hasta validarse en staging."}
    return ejecutar(accion_id, usuario=usuario, confirmar=confirmar)


def rechazar(accion_id: int, usuario: str, motivo: str = "") -> dict:
    a = db.accion(accion_id)
    if not a:
        raise ValueError("Acción no encontrada.")
    if not db.transicion_accion(accion_id, ESTADOS_PENDIENTES, "rechazada", aprobado_por=usuario, aprobado_en=db.now(),
                                resultado=motivo or "Rechazada por el usuario"):
        return {"id": accion_id, "estado": a["estado"], "mensaje": "La acción ya no está pendiente."}
    db.log("info", "autonomia", f"Acción #{accion_id} rechazada", motivo, usuario)
    if motivo:
        db.agregar_aprendizaje("agente", a["agente"], f"Acción rechazada «{a['titulo']}»: {motivo}", origen="sistema", usuario=usuario)
    return {"id": accion_id, "estado": "rechazada"}


def ejecutar(accion_id: int, usuario: str = "", confirmar: bool | None = None) -> dict:
    with _CANDADO:
        return _ejecutar(accion_id, usuario, confirmar)


def _ejecutar(accion_id: int, usuario: str, confirmar: bool | None) -> dict:
    # sólo un hilo puede pasar de aprobada → ejecutando
    if not db.transicion_accion(accion_id, "aprobada", "ejecutando"):
        a = db.accion(accion_id) or {}
        return {"id": accion_id, "estado": a.get("estado"), "mensaje": "Sólo se ejecutan acciones aprobadas (o ya se está ejecutando)."}
    a = db.accion(accion_id)
    # la aprobación corresponde a la versión exacta y al efecto concreto: si algo cambió, no se ejecuta
    if int(a.get("version") or 1) != int(a.get("version_aprobada") or 0) or \
            (a.get("efecto_aprobado") and a.get("efecto_aprobado") != efecto(a["tipo"], a["payload"])):
        db.transicion_accion(accion_id, "ejecutando", "propuesta", revalidacion="La propuesta cambió después de aprobarse; requiere nueva aprobación.",
                             aprobado_por=None, aprobado_en=None, aprobado_por_2=None, aprobado_en_2=None)
        return {"id": accion_id, "estado": "propuesta", "mensaje": "La propuesta cambió después de aprobarse; requiere nueva aprobación."}
    if solo_propuesta(a["tipo"]):
        db.transicion_accion(accion_id, "ejecutando", "aprobada_manual", revalidacion="Acción restringida en producción: ejecución manual en Odoo.")
        return {"id": accion_id, "estado": "aprobada_manual", "mensaje": "Acción restringida en producción: ejecución manual en Odoo."}
    # justo antes de escribir se revalidan políticas, vigencia y disponibilidad con la información más reciente;
    # si la información indispensable no se pudo consultar, NO se ejecuta
    rv = revalidar(a)
    if not rv["ok"]:
        db.transicion_accion(accion_id, "ejecutando", rv["estado"], revalidacion=rv["motivo"],
                             **({"error": rv["motivo"]} if rv["estado"] in ("caducada", "bloqueada") else {}))
        db.log("warn", "autonomia", f"Acción #{accion_id} detenida antes de ejecutar: {rv['estado']}", rv["motivo"], usuario)
        return {"id": accion_id, "estado": rv["estado"], "mensaje": rv["motivo"]}
    if rv.get("payload") and rv["payload"] != a["payload"]:
        db.transicion_accion(accion_id, "ejecutando", "propuesta", revalidacion=f"La disponibilidad cambió justo antes de ejecutar: {rv['motivo']} Requiere nueva aprobación.",
                             payload=json.dumps(rv["payload"], ensure_ascii=False, default=str), version=int(a.get("version") or 1) + 1,
                             titulo=titulo_para(a["tipo"], rv["payload"]) or a["titulo"], efecto=efecto(a["tipo"], rv["payload"]),
                             aprobado_por=None, aprobado_en=None, aprobado_por_2=None, aprobado_en_2=None)
        return {"id": accion_id, "estado": "propuesta", "mensaje": "La disponibilidad cambió justo antes de ejecutar; la propuesta se actualizó y requiere nueva aprobación."}
    p = a["payload"]
    ref = a.get("referencia") or f"Agente IA · acción #{accion_id}"
    confirmar = (nivel() >= 2) if confirmar is None else confirmar
    try:
        cli = get_client()
        if a["tipo"] == "transferencia_interna":
            res = odoo_acciones.crear_transferencia_interna(int(p["producto_id"]), float(p["cantidad"]), int(p["origen_id"]),
                                                            int(p["destino_id"]), p.get("uom_id"), referencia=ref,
                                                            nota=a.get("motivo", ""), confirmar=confirmar, cli=cli)
        elif a["tipo"] == "solicitud_compra":
            res = odoo_acciones.crear_solicitud_compra(int(p["producto_id"]), float(p["cantidad"]), p.get("proveedor_id"),
                                                       referencia=ref, nota=a.get("motivo", ""), cli=cli)
        elif a["tipo"] == "regla_reabastecimiento":
            res = odoo_acciones.crear_o_actualizar_regla(int(p["producto_id"]), int(p["ubicacion_id"]), float(p["minimo"]),
                                                         float(p["maximo"]), float(p.get("multiplo", 1.0)), cli=cli)
        elif a["tipo"] == "actividad":
            res = odoo_acciones.crear_actividad(p["modelo"], int(p["res_id"]), p["resumen"], p.get("nota", ""), p.get("usuario_id"), cli=cli)
        elif a["tipo"] == "nota_chatter":
            res = odoo_acciones.nota_chatter(p["modelo"], int(p["res_id"]), p["cuerpo"], cli=cli)
        elif a["tipo"] == "alerta":
            res = {"modelo": "", "id": None, "ref": "alerta interna", "estado": "registrada"}
        elif a["tipo"] == "ticket_helpdesk":
            res = odoo_acciones.crear_ticket_helpdesk(p["titulo_ticket"], p.get("descripcion", ""), p.get("equipo", ""), str(p.get("prioridad", "2")),
                                                      referencia=ref, cli=cli)
        elif a["tipo"] == "correo":
            adj = None
            if p.get("reporte_id"):
                rep = db.reporte(int(p["reporte_id"]))
                if rep:
                    adj = {"nombre": rep["archivo"], "ruta": rep["ruta"]}
            res = odoo_acciones.enviar_correo(list(p.get("correos") or []), p.get("asunto") or a["titulo"], p.get("cuerpo") or a.get("motivo", ""),
                                              referencia=ref, adjunto=adj, cli=cli)
        elif a["tipo"] == "aviso_equipo":
            # los destinatarios se resuelven de nuevo al ejecutar (los grupos de Odoo pueden haber cambiado desde la propuesta)
            dest = odoo_acciones.destinatarios_equipo(p.get("equipo", ""), cli=cli, incluir_logins=p.get("logins_extra"))
            res = odoo_acciones.enviar_aviso_equipo(p.get("equipo", ""), p.get("asunto") or a["titulo"], p.get("cuerpo") or a.get("motivo", ""),
                                                    dest["personas"], modelo=p.get("modelo"), res_id=p.get("res_id"), producto_id=p.get("producto_id"),
                                                    plazo_dias=int(p.get("plazo_dias") or 5), referencia=ref, cli=cli)
        elif a["tipo"] == "cuarentena_lote":
            res = odoo_acciones.cuarentena_lote(int(p["producto_id"]), str(p["lote"]), float(p["cantidad"]), int(p["origen_id"]),
                                                referencia=ref, nota=a.get("motivo", ""), cli=cli)
        elif a["tipo"] == "desechar_lote":
            res = odoo_acciones.desechar_lote(int(p["producto_id"]), str(p["lote"]), float(p["cantidad"]), int(p["ubicacion_id"]),
                                              motivo=a.get("motivo", ""), cli=cli)
        elif a["tipo"] == "reprogramar_compra":
            res = odoo_acciones.reprogramar_compra(str(p["orden"]), str(p["nueva_fecha"]), a.get("motivo", ""), cli=cli)
        elif a["tipo"] == "recordatorio_proveedor":
            res = odoo_acciones.recordatorio_proveedor(str(p["orden"]), p.get("cuerpo") or a.get("motivo", ""), cli=cli)
        elif a["tipo"] == "confirmar_transferencia":
            res = odoo_acciones.confirmar_transferencia(str(p["picking"]), cli=cli)
        elif a["tipo"] == "validar_recepcion":
            res = odoo_acciones.validar_recepcion(str(p["picking"]), cli=cli)
        elif a["tipo"] == "ajustar_lead_time_proveedor":
            res = odoo_acciones.ajustar_lead_time_proveedor(int(p["producto_id"]), float(p["dias"]), cli=cli)
        elif a["tipo"] == "solicitar_conteo":
            res = odoo_acciones.solicitar_conteo(int(p["producto_id"]), int(p["ubicacion_id"]), a.get("motivo", ""), cli=cli)
        else:
            raise ValueError(f"Tipo de acción desconocido: {a['tipo']}")
        advertencia = res.get("advertencia")
        db.transicion_accion(accion_id, "ejecutando", "ejecutada", odoo_modelo=res.get("modelo"), odoo_id=res.get("id"),
                             odoo_ref=res.get("ref"), resultado=json.dumps(res, ensure_ascii=False, default=str),
                             ejecutado_en=db.now(), estado_odoo=res.get("estado"),
                             cantidad_ejecutada=res.get("cantidad_base", p.get("cantidad")), importe_ejecutado=(a.get("impacto") or {}).get("importe"),
                             **({"revalidacion": advertencia} if advertencia else {}))
        db.log("info", "autonomia", f"Acción #{accion_id} ejecutada en Odoo", f"{res.get('modelo')} {res.get('ref')}" + (f" · {advertencia}" if advertencia else ""), usuario)
        return {"id": accion_id, "estado": "ejecutada", "odoo": res, "advertencia": advertencia}
    except odoo_acciones.RequiereDecision as e:
        # Odoo pidió una decisión (asistente: entrega parcial, lotes, cantidades): no se completa por suposición
        db.transicion_accion(accion_id, "ejecutando", "requiere_revision", revalidacion=str(e)[:2000],
                             aprobado_por=None, aprobado_en=None, aprobado_por_2=None, aprobado_en_2=None)
        db.log("warn", "autonomia", f"Acción #{accion_id} requiere decisión manual en Odoo", str(e), usuario)
        return {"id": accion_id, "estado": "requiere_revision", "mensaje": str(e)}
    except (OdooError, ValueError, KeyError, TypeError) as e:
        # fallo parcial: si Odoo alcanzó a crear el documento, se liga por referencia (nunca se duplica) y queda con advertencia
        doc = None
        try:
            doc = odoo_acciones.recuperar_por_referencia(a["tipo"], ref)
        except Exception:  # noqa: BLE001
            doc = None
        if doc:
            db.transicion_accion(accion_id, "ejecutando", "ejecutada", odoo_modelo=doc.get("modelo"), odoo_id=doc.get("id"), odoo_ref=doc.get("ref"),
                                 estado_odoo=doc.get("estado"), ejecutado_en=db.now(), resultado=json.dumps(doc, ensure_ascii=False, default=str),
                                 revalidacion=f"Se creó {doc.get('ref')} en Odoo pero una operación posterior falló: {str(e)[:300]}. Revisar el documento.",
                                 cantidad_ejecutada=p.get("cantidad"))
            db.log("error", "autonomia", f"Acción #{accion_id} ejecutada con advertencia", str(e), usuario)
            return {"id": accion_id, "estado": "ejecutada", "odoo": doc, "advertencia": str(e)}
        db.transicion_accion(accion_id, "ejecutando", "error", error=str(e)[:2000])
        db.log("error", "autonomia", f"Acción #{accion_id} falló", str(e), usuario)
        return {"id": accion_id, "estado": "error", "error": str(e)}


def revertir(accion_id: int, usuario: str) -> dict:
    a = db.accion(accion_id)
    if not a or a["estado"] not in ("ejecutada", "concluida", "retrasada") or not a.get("odoo_id"):
        return {"id": accion_id, "estado": (a or {}).get("estado"), "mensaje": "Sólo se revierten acciones ejecutadas en Odoo."}
    if a["tipo"] in ("recordatorio_proveedor", "nota_chatter", "validar_recepcion", "confirmar_transferencia", "correo"):
        return {"id": accion_id, "estado": a["estado"], "mensaje": "Esta acción no es reversible desde aquí (mensaje publicado, correo enviado o validación en Odoo)."}
    anterior = None
    try:
        anterior = (json.loads(a.get("resultado") or "{}") or {}).get("anterior")
    except json.JSONDecodeError:
        pass
    try:
        res = odoo_acciones.revertir(a["odoo_modelo"], int(a["odoo_id"]), anterior=anterior)
        db.transicion_accion(accion_id, a["estado"], "revertida", resultado=json.dumps(res, ensure_ascii=False))
        db.log("info", "autonomia", f"Acción #{accion_id} revertida", f"{a['odoo_modelo']} {a['odoo_ref']}", usuario)
        return {"id": accion_id, "estado": "revertida", "odoo": res}
    except OdooError as e:
        db.log("error", "autonomia", f"No se pudo revertir #{accion_id}", str(e), usuario)
        return {"id": accion_id, "estado": a["estado"], "error": str(e)}


def aprobar_varias(ids: list[int], usuario: str, rol: str) -> list[dict]:
    """Aprobación agrupada: en serie y bajo el mismo candado, de modo que cada revalidación ve los compromisos
    que dejaron las anteriores (dos transferencias del mismo origen no pueden aprobarse por más de lo que hay)."""
    return [aprobar(int(i), usuario, rol) for i in ids]


# ── seguimiento de lo ejecutado ─────────────────────────────────────────────
def verificar_ejecutadas(limite: int = 200) -> dict:
    """Consulta en Odoo el estado de cada acción ejecutada: concluida (recibida/hecha), retrasada o cancelada."""
    pendientes = db.acciones(estado="ejecutada", limite=limite) + db.acciones(estado="retrasada", limite=limite)
    resumen = {"revisadas": 0, "concluidas": 0, "retrasadas": 0, "canceladas": 0, "errores": 0}
    hoy = datetime.now()
    for a in pendientes:
        if not a.get("odoo_modelo") or not a.get("odoo_id"):
            if a["tipo"] == "alerta":
                db.transicion_accion(a["id"], a["estado"], "concluida", verificado_en=db.now(), verificacion="alerta interna")
            continue
        anterior = None
        try:
            anterior = (json.loads(a.get("resultado") or "{}") or {}).get("anterior")
        except json.JSONDecodeError:
            pass
        try:
            est = odoo_acciones.estado_documento(a["odoo_modelo"], int(a["odoo_id"]), anterior=anterior)
        except OdooError as e:
            resumen["errores"] += 1
            db.actualizar_accion(a["id"], verificacion=f"No se pudo verificar: {e}"[:500], verificado_en=db.now())
            continue
        resumen["revisadas"] += 1
        campos = {"verificado_en": db.now(), "estado_odoo": est.get("estado"), "verificacion": json.dumps(est, ensure_ascii=False, default=str)[:1500]}
        if not est.get("existe"):
            db.transicion_accion(a["id"], a["estado"], "cancelada_en_odoo", **campos); resumen["canceladas"] += 1
        elif est.get("cancelado"):
            db.transicion_accion(a["id"], a["estado"], "cancelada_en_odoo", **campos); resumen["canceladas"] += 1
        elif est.get("concluido"):
            db.transicion_accion(a["id"], a["estado"], "concluida", **campos); resumen["concluidas"] += 1
        else:
            fecha = a.get("fecha_requerida") or est.get("fecha_prevista")
            retrasada = False
            if fecha:
                try:
                    retrasada = datetime.fromisoformat(str(fecha)[:19].replace(" ", "T")) < hoy
                except ValueError:
                    pass
            if retrasada and a["estado"] != "retrasada":
                db.transicion_accion(a["id"], a["estado"], "retrasada", **campos); resumen["retrasadas"] += 1
                db.log("warn", "autonomia", f"Acción #{a['id']} retrasada en Odoo", a.get("odoo_ref") or "")
            else:
                db.actualizar_accion(a["id"], **campos)
    db.log("info", "autonomia", "Seguimiento de acciones ejecutadas", json.dumps(resumen))
    return resumen


def importes(agente: str | None = None) -> dict:
    """Importes separados por etapa: propuesto (pendiente de decisión), aprobado (aprobado/ejecutando/manual),
    ejecutado (creado en Odoo o concluido). Nunca se mezclan con lo 'sugerido' por el motor."""
    out = {"propuesto": 0.0, "aprobado": 0.0, "ejecutado": 0.0, "n_propuesto": 0, "n_aprobado": 0, "n_ejecutado": 0}
    con_dinero = ("transferencia_interna", "solicitud_compra")     # una regla min/max o un ticket no mueven dinero
    for e in ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, agente=agente, limite=5000):
            out["propuesto"] += float((a.get("impacto") or {}).get("importe") or 0) if a["tipo"] in con_dinero else 0.0; out["n_propuesto"] += 1
    for e in ("aprobada", "ejecutando", "aprobada_manual"):
        for a in db.acciones(estado=e, agente=agente, limite=5000):
            out["aprobado"] += float(a.get("importe_aprobado") or (a.get("impacto") or {}).get("importe") or 0) if a["tipo"] in con_dinero else 0.0; out["n_aprobado"] += 1
    for e in ("ejecutada", "concluida", "retrasada"):
        for a in db.acciones(estado=e, agente=agente, limite=5000):
            out["ejecutado"] += float(a.get("importe_ejecutado") or (a.get("impacto") or {}).get("importe") or 0) if a["tipo"] in con_dinero else 0.0; out["n_ejecutado"] += 1
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}


def resumen() -> dict:
    return {"nivel": nivel(), "nivel_nombre": NIVELES[nivel()], "politicas": politicas(), "conteo": db.resumen_acciones()}
