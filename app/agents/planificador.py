"""Agente planificador de abasto: razona sobre el plan, anticipa y ajusta.

El motor estadístico entrega el plan base (pronóstico, inventario proyectado, propuestas). Este agente
lo cuestiona con herramientas: revisa lo que el pronóstico NO ve (folios programados, jornadas
extraordinarias, entregas retrasadas, pronósticos de baja confianza, su propia precisión pasada),
simula escenarios ("¿y si el proveedor X se retrasa 5 días?", "¿y si el hospital Y opera al 150 %?"), compara con
la corrida anterior y, cuando hace falta, AJUSTA o AGREGA propuestas (siempre a la cola de aprobación)
explicando qué cambió y por qué. Sin Claude, aplica reglas de anticipación deterministas.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import numpy as np
import pandas as pd

from .. import db
from ..config import settings
from ..llm import claude, prompts
from . import autonomia
from .base import compacto, contexto_aprendizaje, df_registros, mxn

SISTEMA_PLANIFICADOR = f"""
Eres el Agente Planificador de Abasto de CBH · Agentes de IA (Ingeniería Cóndor). Recibes el plan base del
motor estadístico y tu trabajo es ANTICIPARTE: encontrar lo que el pronóstico no ve y corregir el plan antes
de que ocurra el desabasto o la compra innecesaria.

{prompts.CONTEXTO_CBH}

Cómo se combinan demanda y agenda (NO lo dupliques): la demanda proyectada del plan base YA incluye los folios
programados en su fecha real (la agenda es un piso diario del pronóstico; `ajuste_agenda` es el exceso atribuible a
ella). Las propuestas base ya cubren esa demanda y ya descuentan lo que otras propuestas activas apartaron. Por eso:
  • nunca vuelvas a multiplicar la demanda por la jornada extraordinaria: eso ya está dentro;
  • un escenario es HIPOTÉTICO (p. ej. "la agenda crece 20 % más", "este proveedor se retrasa 5 días") y se simula con
    `simular_escenario`, que incluye las propuestas activas como entradas; su `faltante_residual` es lo que aún faltaría
    DESPUÉS del plan, y sólo eso puede justificar ajustar o agregar una propuesta.

Método obligatorio (usa las herramientas; respeta los límites de llamadas y escenarios que se indican al final):
1. `precision_pasada`: ¿dónde me equivoqué la última vez? Ajusta tu confianza por producto.
2. `ver_folios_programados` y `ver_pendientes`: agenda por fecha, entregas retrasadas, transferencias en camino.
3. `comparar_con_corrida_anterior`: qué empeoró, qué se resolvió, qué es nuevo.
4. `simular_escenario` con los escenarios más plausibles, dentro del límite: (a) retraso de las entregas pendientes concretas (por referencia o proveedor);
   (b) un crecimiento hipotético de la agenda sólo en sus fechas (`dias_desde`/`dias_hasta`) o en pronósticos de baja confianza.
5. Si un escenario plausible deja un faltante RESIDUAL, AJUSTA la propuesta existente (`ajustar_propuesta`, que recalcula
   cobertura, disponibilidad del origen, importe y riesgo) o AGREGA una (`agregar_propuesta`). No dupliques propuestas y
   no pidas más de lo que el origen puede ceder.
6. Señala las compras que NO llegan antes del quiebre (`llega_a_tiempo` = false): no resuelven el faltante por sí solas.
7. Termina SIEMPRE con `concluir_plan`: decisiones (qué, por qué, impacto), riesgos con fecha estimada, escenarios
   evaluados, "lo que el modelo no ve", y un resumen ejecutivo de 5 líneas.
Regla de oro: cada ajuste debe citar números (cobertura antes/después, fecha estimada de quiebre, importe).
""".strip()

HERRAMIENTAS_PLAN: list[dict] = [
    {"name": "ver_plan", "description": "Filas del plan base (nivel local o red) filtradas por criticidad, producto o ubicación.",
     "input_schema": {"type": "object", "properties": {"criticidad": {"type": "string"}, "producto": {"type": "string"},
                                                       "ubicacion": {"type": "string"}, "nivel": {"type": "string", "enum": ["local", "red"]},
                                                       "limite": {"type": "integer"}}, "required": []}},
    {"name": "simular_escenario", "description": "Re-proyecta el saldo DÍA A DÍA de cada ubicación bajo supuestos HIPOTÉTICOS sobre la demanda "
                                                 "proyectada (que ya incluye la agenda): factores de demanda por hospital/producto/ubicación, aplicados "
                                                 "sólo entre dias_desde y dias_hasta (relativos, 1 = mañana; por omisión todo el horizonte); retraso (días) "
                                                 "de entregas específicas por referencia, proveedor o producto (o de todas con retraso_entregas_dias); "
                                                 "excluir lo en camino. Incluye las propuestas activas como entradas (incluir_propuestas=true). Devuelve qué "
                                                 "ubicaciones empeoran, su fecha de quiebre y el faltante RESIDUAL tras el plan.",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string"},
         "factor_demanda": {"type": "object", "description": "{nombre EXACTO de hospital, producto o ubicación tal como aparece en el plan: factor}, p.ej. {\"<hospital del plan>\": 1.2}"},
         "dias_desde": {"type": "integer"}, "dias_hasta": {"type": "integer"},
         "retrasar": {"type": "object", "description": "días de retraso por referencia, proveedor o producto EXACTOS de ver_pendientes, p.ej. {\"<referencia de compra>\": 5}"},
         "retraso_entregas_dias": {"type": "integer", "description": "retraso aplicado a TODAS las entregas pendientes"},
         "excluir_en_camino": {"type": "boolean"}, "incluir_propuestas": {"type": "boolean"}}, "required": ["nombre"]}},
    {"name": "ver_folios_programados", "description": "Folios programados por hospital y día dentro del horizonte, comparados con lo típico.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ver_pendientes", "description": "Compras y transferencias en camino, y las retrasadas.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "historial_producto", "description": "Consumo semanal de un producto (opcionalmente en una ubicación) en las últimas 12 semanas.",
     "input_schema": {"type": "object", "properties": {"producto": {"type": "string"}, "ubicacion": {"type": "string"}}, "required": ["producto"]}},
    {"name": "comparar_con_corrida_anterior", "description": "Diferencias frente a la corrida previa del Agente 2: KPIs y cambios de criticidad.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "precision_pasada", "description": "Qué tan bien acertó el pronóstico anterior contra el consumo real (por producto).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ajustar_propuesta", "description": "Cambia la cantidad de una propuesta pendiente de esta corrida (id) con un motivo. Recalcula riesgo y aprobaciones; una transferencia se topa a lo utilizable en el origen y devuelve faltante_origen.",
     "input_schema": {"type": "object", "properties": {"accion_id": {"type": "integer"}, "cantidad": {"type": "number"}, "motivo": {"type": "string"}},
                      "required": ["accion_id", "cantidad", "motivo"]}},
    {"name": "agregar_propuesta", "description": "Agrega una propuesta nueva a la cola (una transferencia nunca excede lo utilizable en el origen): transferencia_interna (producto, cantidad, origen, destino) "
                                                 "o solicitud_compra (producto, cantidad).",
     "input_schema": {"type": "object", "properties": {"tipo": {"type": "string", "enum": ["transferencia_interna", "solicitud_compra"]},
                                                       "producto": {"type": "string"}, "cantidad": {"type": "number"}, "origen": {"type": "string"},
                                                       "destino": {"type": "string"}, "motivo": {"type": "string"}, "fecha_requerida": {"type": "string"}},
                      "required": ["tipo", "producto", "cantidad", "motivo"]}},
    {"name": "concluir_plan", "description": "Registra las decisiones finales. Llámala exactamente una vez al terminar.",
     "input_schema": {"type": "object", "properties": {
         "resumen": {"type": "string"},
         "decisiones": {"type": "array", "items": {"type": "object", "properties": {
             "decision": {"type": "string"}, "por_que": {"type": "string"}, "impacto": {"type": "string"}, "accion_id": {"type": "integer"}},
             "required": ["decision", "por_que"]}},
         "riesgos": {"type": "array", "items": {"type": "object", "properties": {
             "riesgo": {"type": "string"}, "fecha_estimada": {"type": "string"}, "probabilidad": {"type": "string"}, "mitigacion": {"type": "string"}},
             "required": ["riesgo"]}},
         "escenarios": {"type": "array", "items": {"type": "object", "properties": {"nombre": {"type": "string"}, "resultado": {"type": "string"}}}},
         "lo_que_el_modelo_no_ve": {"type": "array", "items": {"type": "string"}},
         "confianza_global": {"type": "string", "enum": ["alta", "media", "baja"]}},
         "required": ["resumen", "decisiones", "riesgos"]}},
]


class ContextoPlan:
    def __init__(self, res: dict, df: pd.DataFrame, pendientes: pd.DataFrame, folios_prog: pd.DataFrame,
                 acciones: list[dict], corrida_id: int, cfg) -> None:
        self.res, self.df, self.pendientes, self.folios_prog = res, df, pendientes, folios_prog
        self.acciones, self.corrida_id, self.cfg = acciones, corrida_id, cfg
        self.r: pd.DataFrame = res["resurtido"]
        if self.r is None or self.r.empty or "nivel" not in getattr(self.r, "columns", []):
            self.r = pd.DataFrame(columns=["nivel", "almacen", "producto_id", "producto", "hospital", "unidad", "stock_actual", "demanda_diaria",
                                           "stock_seguridad", "punto_reorden", "demanda_horizonte", "es_cedis", "criticidad", "costo_unit",
                                           "stock_proyectado", "sugerido", "dias_cobertura", "confianza", "mape", "dia_quiebre", "lead_time_dias"])
        self.trazas: list[dict] = []
        self.cambios: list[dict] = []

    # ── herramientas ────────────────────────────────────────────────────────
    def ver_plan(self, criticidad=None, producto=None, ubicacion=None, nivel="local", limite=25):
        r = self.r[self.r["nivel"] == nivel]
        if criticidad:
            r = r[r["criticidad"] == criticidad]
        if producto:
            r = r[r["producto"].str.contains(producto, case=False, regex=False)]
        if ubicacion:
            r = r[r["almacen"].str.contains(ubicacion, case=False, regex=False)]
        cols = ["producto", "almacen", "hospital", "criticidad", "confianza", "stock_actual", "en_camino", "primera_entrada_dia", "saldo_minimo",
                "fecha_quiebre", "fecha_necesaria", "demanda_diaria", "demanda_comprometida", "ajuste_agenda", "dias_con_agenda", "dias_cobertura",
                "lead_time_dias", "stock_seguridad", "sugerido", "sugerido_compra", "unidad_compra", "llega_a_tiempo", "fecha_llegada_estimada",
                "unidad", "mape", "sesgo_pct"]
        return {"filas": df_registros(r[[c for c in cols if c in r.columns]], int(limite or 25)), "total": int(len(r))}

    def _entradas_propuestas(self, excluir_id: int | None = None) -> tuple[dict, dict]:
        """Entradas y salidas que aportarían las propuestas ACTIVAS (pendientes, aprobadas o en borrador en Odoo),
        con la fecha de llegada estimada (lead time interno / del proveedor). Así un escenario mide el faltante
        RESIDUAL después del plan y no vuelve a pedir lo que ya está propuesto."""
        ent: dict[str, list] = {}
        sal: dict[str, list] = {}
        comp = autonomia.compromisos_activos(excluir_id=excluir_id)
        lt_int = max(1, int(round(self.cfg.lead_time_interno)))
        for d_ in comp.get("detalle", []):
            pid, q = d_["producto_id"], float(d_["cantidad"])
            if d_["tipo"] == "transferencia_interna" and d_.get("destino"):
                ent.setdefault(f"{pid}|{d_['destino']}", []).append((lt_int, q))
            if d_["tipo"] in ("transferencia_interna", "cuarentena_lote") and d_.get("origen"):
                sal.setdefault(f"{pid}|{d_['origen']}", []).append((lt_int, q))
        return ent, sal

    def simular_escenario(self, nombre, factor_demanda=None, retrasar=None, retraso_entregas_dias=0, excluir_en_camino=False,
                          lead_time_extra_dias=0, dias_desde=None, dias_hasta=None, incluir_propuestas=True, excluir_propuesta_id=None):
        """Proyección diaria bajo supuestos HIPOTÉTICOS sobre la demanda proyectada (que ya incluye la agenda en sus fechas),
        con las entradas fechadas reales y, por omisión, las propuestas activas del plan. El factor de demanda se aplica
        sólo a los días [dias_desde, dias_hasta] (1 = mañana)."""
        from ..ml.pronostico import proyectar_saldo, criticidad_local
        factor_demanda = factor_demanda or {}
        retrasar = {str(k).lower(): int(v) for k, v in (retrasar or {}).items()}
        pron = self.res.get("pronosticos")
        ent_det, sal_det = self.res.get("entradas_det", {}), self.res.get("salidas_det", {})
        ss_map = self.res.get("stock_seguridad_local", {})
        ent_prop, sal_prop = self._entradas_propuestas(excluir_propuesta_id) if incluir_propuestas else ({}, {})
        r = self.r[(self.r["nivel"] == "local") & (self.r["demanda_diaria"] > 0)]
        detalle, empeoran = [], 0
        hoy = pd.Timestamp(self.res.get("hoy") or pd.Timestamp.now().date())
        for _, x in r.iterrows():
            key = f"{int(x['producto_id'])}|{x['almacen']}"
            col_f = "demanda_proyectada" if (pron is not None and "demanda_proyectada" in pron.columns) else "pronostico"
            f = pron[(pron["producto_id"] == x["producto_id"]) & (pron["almacen"] == x["almacen"])].sort_values("fecha")[col_f].to_numpy() \
                if pron is not None and not pron.empty else np.full(int(self.cfg.horizonte_dias), float(x["demanda_diaria"]))
            fac = 1.0
            for k, v in factor_demanda.items():
                k_ = str(k).lower()
                if k_ in str(x["hospital"]).lower() or k_ in str(x["producto"]).lower() or k_ in str(x["almacen"]).lower():
                    fac *= float(v)
            f2 = f.astype(float).copy()
            if fac != 1.0:
                a = max(0, int(dias_desde or 1) - 1)
                b = min(len(f2), int(dias_hasta or len(f2)))
                f2[a:b] = f2[a:b] * fac
            entradas = []
            for e in ent_det.get(key, []):
                dia, q, ref, tipo, prov = e[0], e[1], str(e[2]), str(e[3]), str(e[4]) if len(e) > 4 else ""
                if excluir_en_camino:
                    continue
                d = int(dia) + int(retraso_entregas_dias or 0)
                for k_, v in retrasar.items():
                    if k_ in ref.lower() or (prov and k_ in prov.lower()) or k_ in str(x["producto"]).lower():
                        d += v
                entradas.append((d, q))
            entradas += list(ent_prop.get(key, []))
            salidas = [(e[0], e[1]) for e in sal_det.get(key, [])] + list(sal_prop.get(key, []))
            ss = float(ss_map.get(key, x["stock_seguridad"]))
            stock0 = float(x["stock_actual"] or 0)
            proy = proyectar_saldo(stock0, f2, entradas, salidas, ss)
            lt = float(x["lead_time_dias"]) + float(lead_time_extra_dias or 0)
            ciclo = int(self.cfg.ciclo_revision_dias)
            ventana = int(min(len(f2), ciclo + lt)) if not x["es_cedis"] else len(f2)
            sug2 = proyectar_saldo(stock0, f2[:ventana], entradas, salidas, ss)["faltante_para_seguridad"]
            crit2 = criticidad_local(proy, sug2, float(f2.mean()), lt, ciclo, stock0, int(self.cfg.exceso_cobertura_dias))
            q = proy["dia_quiebre"]
            orden = {"desabasto": 0, "critico": 1, "reordenar": 2, "ok": 3, "exceso": 3, "fuente": 3, "sin_movimiento": 3}
            # base comparable: la misma proyección SIN el supuesto hipotético pero con las mismas propuestas
            base = proyectar_saldo(stock0, f.astype(float), [(e[0], e[1]) for e in ent_det.get(key, [])] + list(ent_prop.get(key, [])), salidas, ss)
            crit_base = criticidad_local(base, proyectar_saldo(stock0, f[:ventana].astype(float), [(e[0], e[1]) for e in ent_det.get(key, [])] + list(ent_prop.get(key, [])), salidas, ss)["faltante_para_seguridad"],
                                         float(f.mean()), lt, ciclo, stock0, int(self.cfg.exceso_cobertura_dias))
            if orden.get(crit2, 3) < orden.get(crit_base, 3) or (q is not None and (base["dia_quiebre"] is None or q < base["dia_quiebre"])):
                empeoran += 1
                detalle.append({"producto": x["producto"], "ubicacion": x["almacen"], "hospital": x["hospital"], "antes": crit_base, "despues": crit2,
                                "quiebre_antes": (hoy + pd.Timedelta(days=int(base["dia_quiebre"]))).date().isoformat() if base["dia_quiebre"] is not None else None,
                                "fecha_quiebre_estimada": (hoy + pd.Timedelta(days=int(q))).date().isoformat() if q is not None else None,
                                "cobertura_despues": proy["cobertura_dias"],
                                "faltante_residual": round(max(0.0, proy["faltante_para_seguridad"] - base["faltante_para_seguridad"]), 1),
                                "faltante_estimado": round(proy["faltante_para_seguridad"], 1), "unidad": x["unidad"]})
        detalle.sort(key=lambda d_: (d_["fecha_quiebre_estimada"] or "9999", -d_["faltante_residual"]))
        self.trazas.append({"herramienta": "simular_escenario", "argumentos": {"nombre": nombre, "factor_demanda": factor_demanda, "dias": [dias_desde, dias_hasta],
                            "retrasar": retrasar, "retraso_entregas_dias": retraso_entregas_dias, "excluir_en_camino": excluir_en_camino,
                            "incluir_propuestas": incluir_propuestas}, "resumen": f"{empeoran} empeoran"})
        return {"escenario": nombre, "empeoran": int(empeoran), "detalle": detalle[:25],
                "nota": "faltante_residual = lo que aún faltaría DESPUÉS de las propuestas activas; sólo eso justifica ajustar o agregar."}

    def ver_folios_programados(self):
        fp = self.folios_prog
        if fp is None or fp.empty:
            return {"programados": [], "nota": "No hay folios programados o el modelo de folio no está mapeado."}
        hoy = self.df["fecha"].max()
        tip = self.df[self.df["fecha"] > hoy - pd.Timedelta(days=90)].groupby(["hospital", self.df["fecha"].dt.date])["folio"].nunique()
        tipico = tip.groupby(level=0).median().to_dict()
        g = fp.groupby("hospital").agg(folios=("folios", "sum"), dias=("dia", "nunique")).reset_index()
        out = []
        for _, x in g.iterrows():
            t = float(tipico.get(x["hospital"], 0) or 0)
            por_dia = x["folios"] / max(x["dias"], 1)
            out.append({"hospital": x["hospital"], "folios_programados": int(x["folios"]), "dias": int(x["dias"]), "por_dia": round(por_dia, 1),
                        "tipico_por_dia": round(t, 1), "ratio_vs_tipico": round(por_dia / t, 2) if t else None})
        return {"programados": sorted(out, key=lambda o: -(o["ratio_vs_tipico"] or 0)), "por_dia": df_registros(fp.sort_values("dia"), 60),
                "nota": "La demanda proyectada del plan YA incluye estos folios en su fecha real (ajuste_agenda por ubicación en ver_plan)."}

    def ver_pendientes(self):
        p = self.pendientes
        if p is None or p.empty:
            return {"en_camino": [], "retrasadas": []}
        return {"en_camino": df_registros(p[~p["retrasada"]], 40), "retrasadas": df_registros(p[p["retrasada"]], 40)}

    def historial_producto(self, producto, ubicacion=None):
        d = self.df[self.df["producto"].str.contains(producto, case=False, regex=False)]
        if ubicacion:
            d = d[d["subalmacen"].str.contains(ubicacion, case=False, regex=False) | d["almacen"].str.contains(ubicacion, case=False, regex=False)]
        if d.empty:
            return {"semanas": []}
        s = d.set_index("fecha")["cantidad"].resample("W").sum().tail(12)
        return {"producto": producto, "ubicacion": ubicacion, "unidad": d["unidad"].iloc[0],
                "semanas": [{"semana": k.date().isoformat(), "cantidad": round(float(v), 1)} for k, v in s.items()],
                "promedio_semanal": round(float(s.mean()), 1), "tendencia": "sube" if len(s) > 4 and s.tail(4).mean() > s.head(4).mean() * 1.15
                else ("baja" if len(s) > 4 and s.tail(4).mean() < s.head(4).mean() * 0.85 else "estable")}

    def comparar_con_corrida_anterior(self):
        prev = db.get_ajuste("agente2_ultimo", {}) or {}
        if not prev or prev.get("corrida_id") == self.corrida_id:
            return {"nota": "No hay corrida anterior."}
        k0, k1 = prev.get("kpis", {}), self._kpis_actuales()
        prev_alertas = {(a["producto"], a["almacen"]): a["criticidad"] for a in prev.get("alertas", [])}
        r = self.r[self.r["nivel"] == "local"]
        act = {(x["producto"], x["almacen"]): x["criticidad"] for _, x in r.iterrows()}
        nuevas = [f"{k[0]} en {k[1]} → {v}" for k, v in act.items() if v in ("desabasto", "critico", "reordenar") and k not in prev_alertas]
        resueltas = [f"{k[0]} en {k[1]} (era {v})" for k, v in prev_alertas.items() if act.get(k) in ("ok", "exceso", None)]
        return {"corrida_anterior": prev.get("corrida_id"), "fecha_anterior": prev.get("fecha"), "kpis_anterior": k0, "kpis_actual": k1,
                "alertas_nuevas": nuevas[:20], "alertas_resueltas": resueltas[:20]}

    def _kpis_actuales(self):
        s = self.res.get("resumen", {})
        c = s.get("criticidad", {})
        return {"desabasto": c.get("desabasto", 0), "critico": c.get("critico", 0), "reordenar": c.get("reordenar", 0),
                "importe_compra": s.get("importe_compra_sugerida"), "caducidad_riesgo": s.get("importe_caducidad_en_riesgo")}

    def precision_pasada(self):
        ev = db.get_ajuste("agente2_autoevaluacion", {}) or {}
        return ev or {"nota": "Aún no hay evaluación: se calcula al cerrar cada periodo pronosticado."}

    def _proyectar_local(self, producto_id: int, almacen: str, extra_in=None, extra_out=None) -> dict | None:
        """La misma proyección día por día del plan (demanda proyectada con agenda, entradas y salidas fechadas,
        stock de seguridad) con movimientos adicionales: es el 'después' de una propuesta."""
        from ..ml.pronostico import proyectar_saldo
        pron = self.res.get("pronosticos")
        f = self.r[(self.r["nivel"] == "local") & (self.r["almacen"] == str(almacen)) & (self.r["producto_id"] == int(producto_id))]
        if f.empty:
            return None
        x = f.iloc[0]
        key = f"{int(producto_id)}|{almacen}"
        dem = float(x.get("demanda_diaria", 0) or 0)
        col_f = "demanda_proyectada" if (pron is not None and not pron.empty and "demanda_proyectada" in pron.columns) else "pronostico"
        vec = pron[(pron["producto_id"] == int(producto_id)) & (pron["almacen"] == str(almacen))].sort_values("fecha")[col_f].to_numpy() \
            if pron is not None and not pron.empty and "producto_id" in pron.columns else np.full(int(self.cfg.horizonte_dias), dem)
        if len(vec) == 0:
            vec = np.full(int(self.cfg.horizonte_dias), dem)
        ent = [(e[0], e[1]) for e in self.res.get("entradas_det", {}).get(key, [])] + list(extra_in or [])
        sal = [(e[0], e[1]) for e in self.res.get("salidas_det", {}).get(key, [])] + list(extra_out or [])
        return proyectar_saldo(float(x.get("stock_actual", 0) or 0), np.asarray(vec, dtype=float), ent, sal,
                               float(self.res.get("stock_seguridad_local", {}).get(key, x.get("stock_seguridad", 0) or 0)))

    def _disponible_en(self, almacen, producto_id, excluir_id=None) -> tuple[float | None, dict]:
        """Lo que el origen puede ceder HOY: utilizable − reserva operativa − compromisos de OTRAS propuestas activas."""
        from ..ml.pronostico import reserva_operativa
        if self.r is None or self.r.empty or not almacen or producto_id is None:
            return None, {}
        local = self.r[self.r["nivel"] == "local"]
        f = local[(local["almacen"] == almacen) & (local["producto_id"] == int(producto_id))]
        if f.empty or f["stock_actual"].isna().all():
            return None, {}
        stock = float(f["stock_actual"].iloc[0])
        reserva = reserva_operativa(local, int(producto_id), str(almacen), self.cfg)
        comp = float(autonomia.compromisos_activos(excluir_id=excluir_id)["origen"].get((int(producto_id), str(almacen)), 0.0))
        return max(0.0, stock - reserva - comp), {"stock": stock, "reserva": reserva, "comprometido": comp}

    def _utilizable_en(self, almacen, producto_id):
        return self._disponible_en(almacen, producto_id)[0]

    def ajustar_propuesta(self, accion_id, cantidad, motivo):
        """Cambia la cantidad de una propuesta pendiente y RECALCULA todo su impacto con la misma demanda, agenda,
        fechas de entrega y compromisos que justificaron la propuesta: cobertura del destino, disponibilidad del origen,
        importe, riesgo, faltante residual y aprobaciones necesarias (nueva versión)."""
        from ..ml.pronostico import redondear
        a = db.accion(int(accion_id))
        if not a or a["estado"] not in autonomia.ESTADOS_PENDIENTES or a.get("corrida_id") not in (self.corrida_id, None) and a.get("ultima_corrida_id") != self.corrida_id:
            return {"error": "Sólo se ajustan propuestas pendientes de esta corrida."}
        if a["tipo"] not in ("transferencia_interna", "solicitud_compra"):
            return {"error": "Sólo se ajusta la cantidad de transferencias o solicitudes de compra."}
        payload = dict(a["payload"]); anterior = float(payload.get("cantidad") or 0)
        unidad = str(payload.get("unidad") or "")
        prec = autonomia.precision_unidades()
        cantidad = redondear(float(cantidad), unidad, prec, arriba=True)
        imp = dict(a["impacto"] or {})
        faltante_origen = 0.0
        pid = int(payload["producto_id"])
        if a["tipo"] == "transferencia_interna":
            disponible, det = self._disponible_en(payload.get("origen"), pid, excluir_id=int(accion_id))
            if disponible is None and imp.get("stock_origen") is not None:
                disponible = float(imp["stock_origen"])
            if disponible is not None and cantidad > disponible:
                faltante_origen = redondear(cantidad - disponible, unidad, prec, arriba=True)
                cantidad = redondear(max(0.0, disponible), unidad, prec, abajo=True)
                motivo = (f"{motivo}; topado a lo que {payload.get('origen')} puede ceder ({disponible:g} {unidad}: existencia {det.get('stock', 0):g}"
                          f" − reserva operativa {det.get('reserva', 0):g} − comprometido en otras propuestas {det.get('comprometido', 0):g}), faltan {faltante_origen:g}")
        if cantidad <= 0 or abs(cantidad - anterior) < 10 ** -max(0, 2):
            return {"ok": False, "accion_id": int(accion_id), "antes": anterior, "despues": anterior, "faltante_origen": faltante_origen,
                    "nota": "Sin cambio: el origen no puede ceder más (o la cantidad es la misma)."}
        payload["cantidad"] = cantidad
        imp["cantidad"] = cantidad
        cu = float(imp.get("costo_unit") or (float(imp.get("importe") or 0) / anterior if anterior else 0) or 0)
        imp["costo_unit"], imp["importe"] = cu, round(cantidad * cu, 2)
        lt_int = max(1, int(round(self.cfg.lead_time_interno)))
        if a["tipo"] == "transferencia_interna":
            disponible, det = self._disponible_en(payload.get("origen"), pid, excluir_id=int(accion_id))
            imp.update({"stock_origen": det.get("stock", imp.get("stock_origen")), "reserva_origen": det.get("reserva"),
                        "disponible_origen": disponible, "comprometido_origen_previo": det.get("comprometido")})
            pd_ = self._proyectar_local(pid, payload.get("destino"), extra_in=[(lt_int, cantidad)])
            if pd_:
                imp["cobertura_destino_despues"] = pd_["cobertura_dias"]
                imp["faltante_residual_destino"] = round(pd_["faltante_para_seguridad"], 1)
                imp["llega_a_tiempo"] = True if pd_["dia_quiebre"] is None else lt_int <= int(pd_["dia_quiebre"])
                imp["holgura_dias"] = None if pd_["dia_quiebre"] is None else int(pd_["dia_quiebre"]) - lt_int
            po = self._proyectar_local(pid, payload.get("origen"), extra_out=[(lt_int, cantidad)])
            imp["cobertura_origen_despues"] = po["cobertura_dias"] if po else None
        else:
            ratio = None
            if imp.get("sugerido_compra") and anterior:
                ratio = anterior / float(imp["sugerido_compra"]) if float(imp["sugerido_compra"]) else None
            if ratio and ratio > 1:
                import math
                payload["cantidad_compra"] = float(math.ceil(cantidad / ratio - 1e-9)); imp["sugerido_compra"] = payload["cantidad_compra"]
            red = self.r[(self.r["nivel"] == "red") & (self.r["producto_id"] == pid)]
            if len(red) and float(red.iloc[0]["demanda_diaria"] or 0) > 0:
                imp["cobertura_red_despues"] = round((float(red.iloc[0]["stock_proyectado"]) + cantidad + float(imp.get("cubierto_previo") or 0)) / float(red.iloc[0]["demanda_diaria"]), 1)
        # toda modificación vuelve a pasar por la política: riesgo, topes y aprobaciones necesarias se recalculan
        ev = autonomia.evaluar(a["tipo"], payload, imp)
        campos = dict(payload=json.dumps(db._limpio(payload), ensure_ascii=False), impacto=json.dumps(db._limpio(imp), ensure_ascii=False),
                      motivo=f"{a.get('motivo', '')} | Ajuste del planificador: {motivo} (antes {anterior:g} {unidad})",
                      efecto=autonomia.efecto(a["tipo"], payload), titulo=autonomia.titulo_para(a["tipo"], payload) or a["titulo"],
                      riesgo=ev["riesgo"], ultima_corrida_id=self.corrida_id)
        version = db.modificar_propuesta(int(accion_id), **campos)
        if not ev["permitido"]:
            db.transicion_accion(int(accion_id), ("propuesta", "requiere_revision"), "bloqueada", error=" ".join(ev["motivos"]))
        self.cambios.append({"accion_id": int(accion_id), "antes": anterior, "despues": cantidad, "motivo": motivo,
                             "riesgo": ev["riesgo"], "version": version, "bloqueada": not ev["permitido"]})
        db.log("info", "planificador", f"Propuesta #{accion_id} ajustada {anterior:g} → {cantidad:g} {unidad} (v{version}, riesgo {ev['riesgo']})", motivo)
        return {"ok": True, "accion_id": int(accion_id), "antes": anterior, "despues": cantidad, "riesgo": ev["riesgo"],
                "version": version, "bloqueada": not ev["permitido"], "motivos_politica": ev["motivos"], "faltante_origen": faltante_origen,
                "impacto": {k: imp.get(k) for k in ("importe", "cobertura_destino_despues", "cobertura_origen_despues", "disponible_origen", "faltante_residual_destino", "llega_a_tiempo", "cobertura_red_despues")}}

    def agregar_propuesta(self, tipo, producto, cantidad, motivo, origen=None, destino=None, fecha_requerida=None):
        from ..odoo import acciones as OA
        pr = OA.producto_por_nombre(producto)
        if not pr:
            return {"error": f"No encontré el producto «{producto}»."}
        fila = self.r[self.r["producto_id"] == pr["id"]]
        unidad = str(fila["unidad"].iloc[0]) if len(fila) else str((pr.get("uom_id") or [None, ""])[1])
        payload: dict[str, Any] = {"producto_id": pr["id"], "producto": pr["display_name"], "cantidad": float(cantidad), "unidad": unidad}
        titulo = ""
        impacto: dict[str, Any] = {"cantidad": float(cantidad), "unidad": unidad}
        lt_int = max(1, int(round(self.cfg.lead_time_interno)))
        if tipo == "transferencia_interna":
            o, d_ = OA.ubicacion_por_nombre(origen or ""), OA.ubicacion_por_nombre(destino or "")
            if not o or not d_:
                return {"error": "Origen o destino no encontrados."}
            payload.update({"origen_id": o["id"], "destino_id": d_["id"], "origen": o["complete_name"], "destino": d_["complete_name"]})
            disponible_origen, det = self._disponible_en(o["complete_name"], pr["id"])
            if disponible_origen is not None and float(cantidad) > disponible_origen:
                if disponible_origen <= 0:
                    return {"error": f"{o['complete_name']} no puede ceder {pr['display_name']} (existencia {det.get('stock', 0):g}, reserva operativa "
                                     f"{det.get('reserva', 0):g}, comprometido {det.get('comprometido', 0):g}); propone una compra o busca otro origen."}
                motivo = f"{motivo}; topado a lo que {o['complete_name']} puede ceder ({disponible_origen:g} {unidad})"
                cantidad = disponible_origen
                payload["cantidad"] = float(cantidad)
                impacto["cantidad"] = float(cantidad)
            impacto.update({"stock_origen": det.get("stock"), "reserva_origen": det.get("reserva"), "disponible_origen": disponible_origen,
                            "comprometido_origen_previo": det.get("comprometido")})
            antes = self._proyectar_local(pr["id"], d_["complete_name"])
            despues = self._proyectar_local(pr["id"], d_["complete_name"], extra_in=[(lt_int, float(cantidad))])
            if antes and despues:
                impacto.update({"cobertura_destino_antes": antes["cobertura_dias"], "cobertura_destino_despues": despues["cobertura_dias"],
                                "faltante_residual_destino": round(despues["faltante_para_seguridad"], 1),
                                "llega_a_tiempo": True if antes["dia_quiebre"] is None else lt_int <= int(antes["dia_quiebre"]),
                                "holgura_dias": None if antes["dia_quiebre"] is None else int(antes["dia_quiebre"]) - lt_int})
            po = self._proyectar_local(pr["id"], o["complete_name"], extra_out=[(lt_int, float(cantidad))])
            impacto["cobertura_origen_despues"] = po["cobertura_dias"] if po else None
        elif tipo == "solicitud_compra":
            red = self.r[(self.r["nivel"] == "red") & (self.r["producto_id"] == pr["id"])]
            if len(red):
                x = red.iloc[0]
                payload.update({"unidad_compra": x.get("unidad_compra"), "conversion_faltante": bool(x.get("conversion_faltante"))})
                if x.get("sugerido_compra") and x.get("sugerido"):
                    ratio = float(x["sugerido"]) / float(x["sugerido_compra"])
                    if ratio > 1:
                        import math
                        payload["cantidad_compra"] = float(math.ceil(float(cantidad) / ratio - 1e-9))
                impacto.update({"lead_time": x.get("lead_time_dias"), "llega_a_tiempo": x.get("llega_a_tiempo"),
                                "fecha_llegada_estimada": x.get("fecha_llegada_estimada"), "cobertura_red_antes": x.get("dias_cobertura")})
        cu = float(fila["costo_unit"].max() or 0) if len(fila) else 0.0
        impacto.update({"costo_unit": cu, "importe": round(float(cantidad) * cu, 2)})
        r = autonomia.proponer("demanda", tipo, titulo, payload, motivo=f"Planificador: {motivo}", impacto=impacto,
                               corrida_id=self.corrida_id, usuario="planificador", fecha_requerida=fecha_requerida)
        self.cambios.append({"accion_id": r.get("id"), "nueva": not r.get("reutilizada"), "titulo": r.get("titulo", titulo), "motivo": motivo})
        return r

    def despachar(self, nombre, args, salida):
        if nombre == "concluir_plan":
            salida["plan"] = args
            return {"ok": True, "mensaje": "Plan registrado. Responde únicamente: listo."}
        fn = getattr(self, nombre, None)
        if not fn:
            raise ValueError(f"Herramienta desconocida: {nombre}")
        # tope de escenarios por corrida (Configuración ▸ Agente · Abasto): cada simulación es una llamada a Claude + una
        # re-proyección día a día de toda la red; más de unos pocos no aportan y alargan la corrida
        if nombre == "simular_escenario":
            self.n_escenarios = getattr(self, "n_escenarios", 0) + 1
            tope = int(getattr(self, "max_escenarios", 5) or 5)
            if self.n_escenarios > tope:
                return {"ok": False, "mensaje": f"Límite de {tope} escenarios por corrida alcanzado: no simules más; ajusta o agrega lo que "
                                                "ya justifican los escenarios previos y llama concluir_plan."}
        res = fn(**args)
        if nombre != "simular_escenario":
            self.trazas.append({"herramienta": nombre, "argumentos": args, "resumen": json.dumps(res, ensure_ascii=False, default=str)[:160]})
        return res


# ── razonamiento del plan ───────────────────────────────────────────────────
def razonar(res: dict, df: pd.DataFrame, pendientes: pd.DataFrame, folios_prog: pd.DataFrame, acciones: list[dict],
            corrida_id: int, cfg, con_llm: bool = True, usuario: str = "", progreso: Callable | None = None) -> dict:
    progreso = progreso or (lambda *a: None)
    ctx = ContextoPlan(res, df, pendientes, folios_prog, acciones, corrida_id, cfg)
    cfg2 = db.get_ajuste("agente2_config", {}) or {}
    modelo = cfg2.get("modelo_planificacion") or settings.ANTHROPIC_MODEL
    max_escenarios = max(1, int(cfg2.get("max_escenarios") or 5))
    max_llamadas = max(3, int(cfg2.get("max_llamadas_planificador") or 12))
    ctx.max_escenarios = max_escenarios
    if con_llm and claude.disponible():
        salida: dict = {}
        resumen = res.get("resumen", {})
        prompt = (f"Notas de aprendizaje vigentes:\n{contexto_aprendizaje(['agente', 'producto', 'hospital', 'unidad'], 30)}\n\n"
                  f"RESUMEN DEL PLAN BASE (JSON):\n{compacto(resumen, 4000)}\n\n"
                  f"ALERTAS LOCALES:\n{compacto(ctx.ver_plan(limite=40)['filas'], 12000)}\n\n"
                  f"COMPRAS DE RED:\n{compacto(ctx.ver_plan(nivel='red', limite=30)['filas'], 8000)}\n\n"
                  f"PROPUESTAS EN COLA (id, título, riesgo):\n{compacto([{k: a.get(k) for k in ('id', 'titulo', 'riesgo', 'estado')} for a in acciones], 6000)}\n\n"
                  "Razona, simula y concluye.")
        try:
            progreso("El planificador razona sobre el plan", "precisión pasada · folios programados · escenarios · ajustes")
            sistema = (SISTEMA_PLANIFICADOR
                       + f"\n\nLÍMITES DE ESTA CORRIDA: como máximo {max_llamadas} llamadas a herramientas en total y "
                         f"{max_escenarios} llamadas a simular_escenario (elige los escenarios más plausibles: primero el retraso de las "
                         f"entregas pendientes concretas, luego el crecimiento de la agenda en sus fechas). Cuando se acerque el límite, "
                         f"llama concluir_plan sin falta.")
            r = claude.bucle_herramientas(sistema, [{"role": "user", "content": prompt}], HERRAMIENTAS_PLAN,
                                          lambda n, a: ctx.despachar(n, a, salida), origen="planificador", usuario=usuario,
                                          max_iteraciones=max_llamadas + 1, modelo=modelo)
            if salida.get("plan"):
                plan = salida["plan"]
                plan.update({"modo": f"claude:{modelo}", "cambios": ctx.cambios, "trazas": ctx.trazas})
                db.log("info", "planificador", f"Plan razonado: {len(plan.get('decisiones', []))} decisiones, {len(ctx.cambios)} ajustes", usuario=usuario)
                return plan
            db.log("warn", "planificador", "Claude no llamó a concluir_plan; uso razonamiento determinista")
        except claude.LLMError as e:
            db.log("error", "planificador", "Falla de Claude en planificación", str(e))
    progreso("Anticipación determinista", "folios programados · retrasos · baja confianza")
    return plan_determinista(ctx)


def plan_determinista(ctx: ContextoPlan) -> dict:
    """Reglas de anticipación sin LLM. Separa con claridad:
      • situación actual y plan base (la agenda YA está dentro de la demanda proyectada, en sus fechas);
      • escenarios HIPOTÉTICOS (agenda +20 % sólo en sus fechas; retraso de 5 días de las entregas concretas), simulados
        CON las propuestas activas: sólo su faltante RESIDUAL puede justificar un ajuste;
      • compras que no llegan antes del quiebre → transferencia preventiva si algún origen puede ceder."""
    decisiones, riesgos, no_ve, escenarios = [], [], [], []
    hoy = pd.Timestamp(ctx.res.get("hoy") or pd.Timestamp.now().date())
    local = ctx.r[ctx.r["nivel"] == "local"] if not ctx.r.empty else ctx.r
    agenda = (ctx.res.get("resumen") or {}).get("agenda_por_hospital", {}) or {}
    fp = ctx.ver_folios_programados()
    # ── 1) agenda: informar lo que el plan ya incorpora, por fecha, sin volver a sumarlo ──
    for p in fp.get("programados", []):
        if p.get("ratio_vs_tipico") and p["ratio_vs_tipico"] >= 1.3:
            info = agenda.get(p["hospital"], {})
            filas = local[(local["hospital"] == p["hospital"]) & (local.get("ajuste_agenda", 0) > 0)] if "ajuste_agenda" in local.columns else local.iloc[0:0]
            extra = "; ".join(f"{x['producto']} +{x['ajuste_agenda']:,.0f} {x['unidad']}" for _, x in filas.sort_values("ajuste_agenda", ascending=False).head(4).iterrows())
            no_ve.append(f"{p['hospital']}: {p['folios_programados']} folios programados en {p['dias']} días "
                         f"({p['por_dia']}/día vs. {p['tipico_por_dia']} típico, ×{p['ratio_vs_tipico']})"
                         + (f", desde el {info.get('primer_dia')}" if info.get("primer_dia") else "")
                         + f". El plan ya lo incorpora en esas fechas" + (f": {extra}." if extra else "."))
            # escenario hipotético: la agenda crece 20 % más, sólo en sus fechas
            dias = ctx.res.get("agenda_hospital", {}).get(p["hospital"])
            if dias:
                idx = [i + 1 for i, v in enumerate(dias) if v > 0]
                esc = ctx.simular_escenario(f"Agenda de {p['hospital']} 20 % mayor a lo programado (días {idx[0]}–{idx[-1]})",
                                            {p["hospital"]: 1.2}, dias_desde=idx[0], dias_hasta=idx[-1])
                escenarios.append({"nombre": esc["escenario"], "resultado": f"{esc['empeoran']} ubicaciones empeorarían; faltante residual tras el plan: "
                                   + (", ".join(f"{d_['producto']} {d_['faltante_residual']:,.0f} {d_['unidad']}" for d_ in esc["detalle"][:3]) or "ninguno")})
                for d_ in esc["detalle"][:3]:
                    if d_["faltante_residual"] > 0:
                        riesgos.append({"riesgo": f"Si la agenda de {p['hospital']} creciera 20 %, {d_['producto']} en {d_['ubicacion']} pasaría a {d_['despues']}",
                                        "fecha_estimada": d_["fecha_quiebre_estimada"], "probabilidad": "hipotético",
                                        "mitigacion": f"Faltarían ~{d_['faltante_residual']:,.0f} {d_['unidad']} adicionales al plan; vigilar la agenda real, no comprar por adelantado."})
    # ── 2) entregas retrasadas (hechos) ──
    pen = ctx.ver_pendientes()
    for r_ in pen.get("retrasadas", []):
        riesgos.append({"riesgo": f"Entrega retrasada {r_['ref']} ({r_['producto']}, {float(r_['cantidad_doc'] or r_['cantidad']):g} {r_.get('unidad_doc') or ''}) prevista {str(r_['fecha_prevista'])[:10]}",
                        "fecha_estimada": str(r_["fecha_prevista"])[:10], "probabilidad": "ocurriendo",
                        "mitigacion": "Reclamar al proveedor / cubrir con transferencia interna; no se contó como abastecimiento."})
    # ── 3) escenario hipotético: las entregas pendientes concretas se retrasan 5 días (con las propuestas activas) ──
    refs = sorted({str(e["ref"]) for e in pen.get("en_camino", []) if e.get("ref")})
    if refs:
        esc = ctx.simular_escenario(f"Retraso de 5 días en {len(refs)} entregas pendientes ({', '.join(refs[:4])}{'…' if len(refs) > 4 else ''})",
                                    retrasar={r_: 5 for r_ in refs})
        escenarios.append({"nombre": esc["escenario"], "resultado": f"{esc['empeoran']} ubicaciones empeorarían"})
        for d_ in esc["detalle"][:6]:
            cambio = (f"pasaría a {d_['despues']}" if d_["antes"] != d_["despues"] else f"adelantaría su quiebre al {d_['fecha_quiebre_estimada']}")
            riesgos.append({"riesgo": f"Si esas entregas se retrasan 5 días, {d_['producto']} en {d_['ubicacion']} {cambio}",
                            "fecha_estimada": d_["fecha_quiebre_estimada"], "probabilidad": "media",
                            "mitigacion": (f"Faltarían ~{d_['faltante_residual']:,.0f} {d_['unidad']} más allá del plan: reclamar la entrega o preparar transferencia" if d_["faltante_residual"] > 0
                                           else "El plan actual lo absorbe; sólo vigilar la entrega")})
    else:
        escenarios.append({"nombre": "Sin entregas pendientes que retrasar", "resultado": "n/a"})
    # ── 4) compras que NO llegan antes del quiebre → transferencia preventiva desde un origen que pueda ceder ──
    red = ctx.r[(ctx.r["nivel"] == "red") & (ctx.r["sugerido"] > 0)] if not ctx.r.empty else ctx.r
    for _, x in (red[red.get("llega_a_tiempo", True) == False].iterrows() if ("llega_a_tiempo" in red.columns and len(red)) else []):  # noqa: E712
        pid = int(x["producto_id"])
        riesgos.append({"riesgo": f"La compra de {x['producto']} llegaría el {x.get('fecha_llegada_estimada')} y el quiebre de red es el {x.get('fecha_quiebre')}: no evita el faltante",
                        "fecha_estimada": x.get("fecha_quiebre"), "probabilidad": "alta", "mitigacion": "Transferencia interna desde un origen con excedente o reclamar/adelantar una entrega."})
        destinos = local[(local["producto_id"] == pid) & local["criticidad"].isin(["desabasto", "critico"])].sort_values("dia_quiebre")
        for _, d_ in destinos.head(2).iterrows():
            faltante = float(d_.get("sugerido") or 0)
            if faltante <= 0:
                continue
            fuentes = local[(local["producto_id"] == pid) & (local["almacen"] != d_["almacen"]) & local["criticidad"].isin(["exceso", "ok", "fuente", "sin_movimiento"])]
            mejor, mejor_disp = None, 0.0
            for _, f0 in fuentes.iterrows():
                disp, _det = ctx._disponible_en(f0["almacen"], pid)
                if disp and disp > mejor_disp:
                    mejor, mejor_disp = f0, disp
            if mejor is not None:
                rr = ctx.agregar_propuesta("transferencia_interna", d_["producto"], min(faltante, mejor_disp),
                                           f"la compra de red no llega antes del quiebre ({x.get('fecha_quiebre')}); {mejor['almacen']} puede ceder {mejor_disp:,.0f} {d_['unidad']}",
                                           origen=mejor["almacen"], destino=d_["almacen"], fecha_requerida=d_.get("fecha_necesaria"))
                if rr.get("id"):
                    decisiones.append({"decision": f"{'Mantener' if rr.get('reutilizada') else 'Transferencia preventiva'}: {rr.get('titulo')}",
                                       "por_que": f"La compra llegaría después del quiebre de {d_['almacen']} ({d_.get('fecha_quiebre')})",
                                       "impacto": "Cubre el hueco hasta que llegue la compra", "accion_id": rr["id"]})
    # ── 5) baja confianza y precisión pasada ──
    baja = local[(local["confianza"] == "baja") & local["criticidad"].isin(["critico", "reordenar", "desabasto"])] if len(local) else local
    for _, x in baja.head(6).iterrows():
        no_ve.append(f"Pronóstico de baja confianza para {x['producto']} en {x['almacen']} (WAPE {x['mape']} %): revisar manualmente antes de aprobar.")
    ev = ctx.precision_pasada()
    if ev.get("peores"):
        no_ve.append("Productos donde la corrida anterior falló más: " + ", ".join(f"{p['producto']} ({p['wape']} %)" for p in ev["peores"][:4]))
    n_prop = sum(1 for a in ctx.acciones if a.get("id"))
    def _n(n, s1, s2):
        return f"{n} {s1 if n == 1 else s2}"
    resumen = (f"Plan base con {_n(n_prop, 'propuesta', 'propuestas')} (la agenda ya está incorporada en sus fechas). "
               f"{_n(len(decisiones), 'ajuste adicional', 'ajustes adicionales')}, {_n(len(riesgos), 'riesgo anticipado', 'riesgos anticipados')}, "
               f"{_n(len(escenarios), 'escenario hipotético evaluado', 'escenarios hipotéticos evaluados')} con las propuestas activas, "
               f"{_n(len(no_ve), 'señal', 'señales')} que el pronóstico no ve. Modo determinista (sin razonamiento del modelo de lenguaje).")
    return {"resumen": resumen, "decisiones": decisiones, "riesgos": riesgos, "escenarios": escenarios,
            "lo_que_el_modelo_no_ve": no_ve, "confianza_global": "media", "modo": "determinista", "cambios": ctx.cambios, "trazas": ctx.trazas}


# ── autoevaluación: lo que predije vs. lo que pasó ──────────────────────────
def autoevaluar(df: pd.DataFrame) -> dict:
    """Compara el pronóstico guardado en la última corrida (fechas ya transcurridas) contra el consumo real."""
    u = db.ultima_corrida("demanda")
    if not u:
        return {}
    pron = pd.DataFrame(db.pronosticos(corrida_id=u["id"], limite=200_000))
    if pron.empty:
        return {}
    pron = pron[pron["almacen"] != "RED (todas las ubicaciones)"]
    pron["fecha"] = pd.to_datetime(pron["fecha"], errors="coerce")
    hoy = pd.Timestamp.now().normalize()
    pron = pron[pron["fecha"] < hoy]
    if pron.empty:
        return {"nota": "El periodo pronosticado aún no transcurre.", "corrida": u["id"]}
    d = df.copy()
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce").dt.normalize()
    dim = "subalmacen" if "subalmacen" in d.columns and (d["subalmacen"].astype(str).str.strip() != "").mean() > 0.5 else "almacen"
    real = d[(d["fecha"] >= pron["fecha"].min()) & (d["fecha"] <= pron["fecha"].max())].groupby(["producto_id", dim])["cantidad"].sum()
    pp = pron.groupby(["producto_id", "almacen"]).agg(pronostico=("pronostico", "sum"), producto=("producto", "first")).reset_index()
    filas = []
    for _, x in pp.iterrows():
        r = float(real.get((x["producto_id"], x["almacen"]), 0.0))
        filas.append({"producto": x["producto"], "ubicacion": x["almacen"], "pronosticado": round(float(x["pronostico"]), 1), "real": round(r, 1),
                      "error": round(float(x["pronostico"]) - r, 1), "wape": round(100 * abs(float(x["pronostico"]) - r) / r, 1) if r else None})
    ev = pd.DataFrame(filas)
    con = ev[ev["wape"].notna()]
    por_prod = con.groupby("producto").apply(lambda g: pd.Series({"wape": 100 * g["error"].abs().sum() / max(g["real"].sum(), 1e-9),
                                                                   "sesgo": 100 * g["error"].sum() / max(g["real"].sum(), 1e-9)}), include_groups=False).reset_index()
    out = {"corrida": u["id"], "desde": str(pron["fecha"].min().date()), "hasta": str(pron["fecha"].max().date()), "dias": int(pron["fecha"].nunique()),
           "wape_global": round(100 * float(con["error"].abs().sum() / max(con["real"].sum(), 1e-9)), 1) if len(con) else None,
           "sesgo_global": round(100 * float(con["error"].sum() / max(con["real"].sum(), 1e-9)), 1) if len(con) else None,
           "peores": por_prod.sort_values("wape", ascending=False).head(8).round(1).to_dict("records"),
           "mejores": por_prod.sort_values("wape").head(5).round(1).to_dict("records"), "combinaciones": int(len(con))}
    db.set_ajuste("agente2_autoevaluacion", out)
    db.log("info", "planificador", f"Autoevaluación: WAPE real {out['wape_global']} % en {out['dias']} días", f"sesgo {out['sesgo_global']} %")
    return out
