"""Vigilancia continua entre corridas: chequeos ligeros cada pocas horas que producen alertas accionables.

  1. Consumos de anestésico sin cirugía/médico registrados en las últimas 24 h.
  2. Ubicaciones cuya existencia utilizable ya cayó por debajo del punto de reorden calculado en la última corrida.
  3. Entregas que se volvieron tardías desde la última vigilancia.
  4. Folios programados para los próximos 3 días muy por encima de lo típico.
  5. Propuestas pendientes de aprobación con más de N días.
Cada alerta se deduplica por huella y entra a la cola como «alerta» (no toca Odoo).
"""
from __future__ import annotations

import hashlib
import time

import pandas as pd

from .. import db
from ..llm import claude, prompts
from ..odoo import queries
from . import autonomia
from .base import compacto

CLAVE_HUELLAS = "vigilancia_alertadas"


def _huella(*partes) -> str:
    return hashlib.sha1("|".join(str(p) for p in partes).encode()).hexdigest()[:14]


def ejecutar(usuario: str = "vigilancia") -> dict:
    t0 = time.time()
    corrida_id = db.iniciar_corrida("vigilancia", {}, "programado", usuario)
    huellas = set(db.get_ajuste(CLAVE_HUELLAS, []) or [])
    nuevas: list[dict] = []
    hallazgos: dict = {"sin_cirugia_24h": [], "bajo_reorden": [], "entregas_tardias": [], "jornadas_extraordinarias": [], "aprobaciones_vencidas": []}

    def alerta(tipo: str, titulo: str, motivo: str, huella: str, impacto: dict | None = None):
        if huella in huellas:
            return
        r = autonomia.proponer("vigilancia", "alerta", titulo, {"tipo_vigilancia": tipo, "huella": huella}, motivo=motivo,
                               impacto=impacto or {}, corrida_id=corrida_id, usuario=usuario)
        huellas.add(huella)
        nuevas.append({"tipo": tipo, "titulo": titulo, "accion_id": r.get("id")})

    try:
        # 1 · consumos sin cirugía en 24 h
        df = queries.consumo(dias=2, tope=20_000)
        if not df.empty:
            ayer = pd.Timestamp.now() - pd.Timedelta(hours=24)
            unidad = df["unidad"].astype(str).str.lower()
            vol = unidad.isin(["ml", "mililitro", "mililitros"]) | df["producto"].str.lower().str.contains("sevo|desflu|isoflu", regex=True)
            sc = df[(df["fecha"] >= ayer) & vol & ((df["medico"].astype(str).str.strip() == "") | ~(df["duracion_min"] > 0))]
            for _, x in sc.head(10).iterrows():
                hallazgos["sin_cirugia_24h"].append({"folio": x["folio"], "hospital": x["hospital"], "producto": x["producto"], "cantidad": float(x["cantidad"])})
                alerta("sin_cirugia", f"Consumo sin cirugía: {x['producto']} {x['cantidad']:g} {x['unidad']} · {x['folio']} · {x['hospital']}",
                       "Anestésico registrado en las últimas 24 h sin médico/duración de cirugía. Requiere conciliación hoy.",
                       _huella("sc", x["folio"], x["producto_id"], x["cantidad"]), {"importe": float(x["importe"] or 0)})
        # 2 · bajo punto de reorden con existencias actuales
        u = db.ultima_corrida("demanda")
        if u:
            plan = pd.DataFrame(db.resurtido(corrida_id=u["id"], limite=5000))
            ex = queries.existencias()
            if not plan.empty and not ex.empty:
                col = "ubicacion" if "ubicacion" in ex.columns else "almacen"
                st = ex.groupby(["producto_id", col])["utilizable"].sum()
                for _, p in plan[plan["punto_reorden"] > 0].iterrows():
                    s_ = float(st.get((p["producto_id"], p["almacen"]), float("nan")))
                    if s_ == s_ and s_ < float(p["punto_reorden"]) and p["criticidad"] in ("ok", "exceso"):   # sólo lo que la corrida daba por sano
                        hallazgos["bajo_reorden"].append({"producto": p["producto"], "ubicacion": p["almacen"], "utilizable": s_, "punto_reorden": p["punto_reorden"]})
                        alerta("bajo_reorden", f"Bajo punto de reorden: {p['producto']} en {p['almacen']} ({s_:g} < {p['punto_reorden']:g})",
                               f"La existencia utilizable cayó por debajo del punto de reorden desde la última corrida (demanda {p['demanda_diaria']:.2f}/día).",
                               _huella("ro", p["producto_id"], p["almacen"], int(s_ < p["punto_reorden"] * 0.5)))
        # 3 · entregas tardías
        pen = queries.abastecimiento_pendiente()
        if not pen.empty:
            for _, x in pen[pen["retrasada"]].iterrows():
                hallazgos["entregas_tardias"].append({"ref": x["ref"], "producto": x["producto"], "cantidad": float(x["cantidad"]), "fecha": str(x["fecha_prevista"])[:10]})
                h_et = _huella("et", x["ref"], x["producto_id"])
                if h_et not in huellas and x["tipo"] == "compra":
                    r = autonomia.proponer("vigilancia", "recordatorio_proveedor", f"Recordar entrega {x['ref']} a {x.get('proveedor') or 'proveedor'} · {x['producto']}",
                                           {"orden": x["ref"], "cuerpo": (f"Recordatorio automático: la línea de {x['producto']} ({x['cantidad_doc']:g} {x['unidad_doc']}) "
                                                                          f"estaba prevista el {str(x['fecha_prevista'])[:10]} y no se ha recibido. Favor de confirmar nueva fecha.")},
                                           motivo="Entrega vencida; el pronóstico ya no la cuenta como abastecimiento.", corrida_id=corrida_id, usuario=usuario)
                    nuevas.append({"tipo": "recordatorio_proveedor", "titulo": f"Recordatorio {x['ref']}", "accion_id": r.get("id")})
                alerta("entrega_tardia", f"Entrega retrasada: {x['ref']} · {x['producto']} · {x['cantidad']:g}",
                       f"Prevista el {str(x['fecha_prevista'])[:10]} y aún no recibida. Reclamar al proveedor o cubrir con transferencia.",
                       h_et, {"cantidad": float(x["cantidad"])})
        # 4 · jornadas extraordinarias próximos 3 días
        fp = queries.folios_programados(3)
        if not fp.empty:
            hist = queries.consumo(dias=60, tope=60_000)
            if not hist.empty:
                tip = hist.groupby(["hospital", hist["fecha"].dt.date])["folio"].nunique().groupby(level=0).median()
                for h, g in fp.groupby("hospital"):
                    por_dia = g["folios"].sum() / max(g["dia"].nunique(), 1)
                    t = float(tip.get(h, 0) or 0)
                    if t and por_dia / t >= 1.5:
                        hallazgos["jornadas_extraordinarias"].append({"hospital": h, "por_dia": por_dia, "tipico": t})
                        alerta("jornada", f"Jornada extraordinaria en {h}: {por_dia:.0f} folios/día vs. {t:.0f} típico",
                               "Los próximos 3 días superan la carga habitual; verificar existencias de anestésicos e insumos críticos.",
                               _huella("jx", h, str(g["dia"].min())))
        # 5 · aprobaciones vencidas
        vig = int(autonomia.politicas().get("vigencia_propuesta_dias", 7))
        pendientes = [a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, limite=300)]
        viejas = [a for a in pendientes if (pd.Timestamp.now(tz="UTC") - pd.Timestamp(a["creado_en"])).days >= max(2, vig - 2)]
        if viejas:
            hallazgos["aprobaciones_vencidas"] = [{"id": a["id"], "titulo": a["titulo"]} for a in viejas[:10]]
            alerta("aprobaciones", f"{len(viejas)} propuestas llevan ≥{max(2, vig - 2)} días sin decisión",
                   "Las propuestas caducan a los %d días; decide o recházalas con motivo." % vig, _huella("ap", len(viejas), str(pd.Timestamp.now().date())))
    except Exception as e:  # noqa: BLE001
        db.cerrar_corrida(corrida_id, "error", error=str(e))
        db.log("error", "vigilancia", "Falló la vigilancia", str(e))
        raise
    db.set_ajuste(CLAVE_HUELLAS, sorted(huellas)[-2000:])
    resumen = {k: len(v) for k, v in hallazgos.items()}
    out = {"fecha": db.now(), "nuevas_alertas": nuevas, "hallazgos": hallazgos, "resumen": resumen, "segundos": round(time.time() - t0, 1)}
    # lectura con IA: qué importa primero y por qué (los chequeos son deterministas; la priorización y la explicación no)
    if nuevas and claude.disponible():
        out["lectura"] = claude.completar(
            prompts.SISTEMA_VIGILANCIA,
            f"Hallazgos de la vigilancia de hoy (JSON):\n{compacto(hallazgos, 9000)}\n\nAlertas nuevas: {compacto(nuevas, 3000)}\n\n"
            "Escribe la lectura: qué atender primero, por qué, y qué decisión conviene tomar en cada caso.",
            origen="vigilancia", usuario=usuario, respaldo="")
    db.set_ajuste("ultima_vigilancia", out)
    db.cerrar_corrida(corrida_id, "ok", hallazgos=sum(resumen.values()), resumen=f"{len(nuevas)} alertas nuevas · " + ", ".join(f"{k} {v}" for k, v in resumen.items() if v))
    db.log("info", "vigilancia", f"Vigilancia: {len(nuevas)} alertas nuevas", str(resumen))
    return out
