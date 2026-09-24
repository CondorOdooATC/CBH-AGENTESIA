"""Agente 2 · Pronóstico de Demanda y Resurtido — orquestación."""
from __future__ import annotations

import math
import threading
import time

import pandas as pd

from .. import db
from ..config import settings
from ..llm import claude, prompts
from ..ml import pronostico as P
from ..odoo import queries
from ..reports import builders
from . import autonomia, planificador
from .base import Cronometro, compacto, contexto_aprendizaje, df_registros, mxn

NOMBRE = "demanda"
_CANDADO = threading.Lock()   # una sola corrida a la vez (manual, programada o desde el copiloto)


def _config() -> P.ConfigPronostico:
    cfg = P.ConfigPronostico(horizonte_dias=settings.AGENT2_HORIZON_DAYS, historia_dias=settings.AGENT2_HISTORY_DAYS,
                             nivel_servicio=settings.AGENT2_SERVICE_LEVEL, lead_time_default=settings.AGENT2_LEAD_TIME_DAYS)
    ajustes = db.get_ajuste("agente2_config", {}) or {}
    for k, v in ajustes.items():
        if hasattr(cfg, k) and not isinstance(getattr(cfg, k), (dict, set)):
            setattr(cfg, k, type(getattr(cfg, k))(v))
    cfg.regiones = db.get_ajuste("regiones", {}) or {}
    cfg.multiplos = {int(k): float(v) for k, v in (db.get_ajuste("multiplos", {}) or {}).items()}
    return cfg


def ejecutar(horizonte: int | None = None, usuario: str = "", disparo: str = "manual", con_llm: bool = True,
             generar_excel: bool = True, proponer_acciones: bool = True, progreso=None) -> dict:
    if not _CANDADO.acquire(blocking=False):
        raise RuntimeError("El Agente 2 ya está en ejecución; espera a que termine.")
    if con_llm and settings.LLM_ENABLED and db.presupuesto_agotado():
        _CANDADO.release()
        raise RuntimeError(db.mensaje_presupuesto_agotado())
    try:
        return _ejecutar(horizonte, usuario, disparo, con_llm, generar_excel, proponer_acciones, progreso or (lambda *a: None))
    finally:
        _CANDADO.release()


def _ejecutar(horizonte, usuario, disparo, con_llm, generar_excel, proponer_acciones, progreso) -> dict:
    cfg = _config()
    if horizonte:
        cfg.horizonte_dias = int(horizonte)
    corrida_id = db.iniciar_corrida(NOMBRE, {"horizonte": cfg.horizonte_dias, "historia": cfg.historia_dias,
                                             "nivel_servicio": cfg.nivel_servicio}, disparo, usuario)
    t0 = time.time()
    progreso = Cronometro(progreso)          # mide cada paso: la bitácora dice en qué se fue el tiempo
    try:
        progreso("Leyendo consumo histórico", f"{cfg.historia_dias} días")
        df = queries.consumo(dias=cfg.historia_dias, progreso=progreso)
        progreso("Autoevaluando el pronóstico anterior", "predicho vs. real")
        try:
            autoeval = planificador.autoevaluar(df)
        except Exception as e:  # noqa: BLE001
            db.log("warn", "agente2", "Autoevaluación falló", str(e)); autoeval = {}
        progreso("Leyendo existencias, reservas y lotes", "")
        ex = queries.existencias()
        progreso("Leyendo compras y transferencias en camino, folios programados y proveedores", "")
        cfg.lead_times = queries.lead_times()
        pendientes = queries.abastecimiento_pendiente()
        folios_prog = queries.folios_programados(cfg.horizonte_dias)
        uom_compra = queries.unidades_compra()
        cfg.precision = queries.precision_uom()
        if cfg.precision:
            db.set_ajuste("precision_uom", cfg.precision)
        # libro de compromisos: lo que ya apartaron propuestas activas (pendientes, aprobadas o en borrador en Odoo)
        refs_conf = set(pendientes[pendientes["confirmada"]]["ref"].astype(str)) if len(pendientes) else set()
        compromisos = autonomia.compromisos_activos(refs_confirmadas=refs_conf)
        progreso("Pronosticando (backtesting de métodos por producto × ubicación)",
                 f"{len(df):,} líneas · {len(pendientes)} documentos en camino · {int(folios_prog['folios'].sum()) if len(folios_prog) else 0} folios programados"
                 f" · {len(compromisos['detalle'])} propuestas activas consideradas")
        res = P.pronosticar(df, ex, cfg, pendientes=pendientes, folios_prog=folios_prog, uom_compra=uom_compra, compromisos=compromisos)
        res["compromisos"] = compromisos
        r, pron = res["resurtido"], res["pronosticos"]
        progreso("Pronóstico terminado", f"{len(r)} combinaciones · {res['resumen'].get('transferencias_propuestas', 0)} transferencias · "
                                          f"{res['resumen'].get('productos_red_por_comprar', 0)} compras")

        # ── persistencia ──
        if not pron.empty:
            db.guardar_pronosticos([{"corrida_id": corrida_id, **x} for x in df_registros(pron)])
        if not r.empty:
            cols = ["producto_id", "producto", "almacen", "unidad", "stock_actual", "demanda_diaria", "sigma_diaria",
                    "lead_time_dias", "stock_seguridad", "punto_reorden", "demanda_horizonte", "sugerido", "dias_cobertura",
                    "criticidad", "metodo", "mape"]
            db.guardar_resurtido([{"corrida_id": corrida_id, **x} for x in df_registros(r, cols=cols)])

        # ── acciones ──
        progreso("Proponiendo transferencias, compras y reglas para aprobación", "")
        acciones = _proponer(res, corrida_id, usuario) if proponer_acciones else []

        # ── razonamiento: anticipar, simular, ajustar ──
        plan = planificador.razonar(res, df, pendientes, folios_prog, acciones, corrida_id, cfg, con_llm=con_llm, usuario=usuario, progreso=progreso)
        res["plan_razonado"], res["autoevaluacion"] = plan, autoeval

        # ── narrativa y Excel ──
        progreso("Redactando el plan", "con Claude" if (con_llm and claude.disponible()) else "plan determinista")
        informe = _informe(res, df, corrida_id, con_llm, usuario, acciones)
        progreso("Generando el Excel", "")
        res["importes"] = autonomia.importes("demanda")
        reporte = builders.reporte_pronostico(res, informe, {"id": corrida_id}, usuario) if generar_excel else None

        tiempos = progreso.cerrar()
        db.log("info", "agente2", f"Tiempos de la corrida #{corrida_id} ({round(time.time() - t0)} s)", progreso.resumen(), usuario)
        kpis = _kpis(res)
        kpis["importes"] = res["importes"]
        kpis["propuestas_nuevas"] = sum(1 for a in acciones if a.get("id") and not a.get("reutilizada"))
        kpis["propuestas_vigentes"] = sum(1 for a in acciones if a.get("reutilizada"))
        kpis["propuestas_caducadas"] = int(res.get("caducadas", 0))
        resumen = (f"{kpis['combinaciones']} combinaciones · {kpis['desabasto'] + kpis['critico']} en riesgo de desabasto · "
                   f"compra sugerida {mxn(kpis['importe_compra'])} · {kpis['propuestas_nuevas']} propuestas nuevas, {kpis['propuestas_vigentes']} vigentes")
        db.cerrar_corrida(corrida_id, "ok", int(len(df)), int(kpis["desabasto"] + kpis["critico"] + kpis["reordenar"]), resumen)
        db.set_ajuste("agente2_ultimo", {
            "corrida_id": corrida_id, "kpis": kpis, "informe": informe, "reporte": reporte, "resumen_motor": res["resumen"],
            "plan_razonado": {k: v for k, v in plan.items() if k != "trazas"}, "autoevaluacion": autoeval,
            "dimension": res.get("dimension"),
            "alertas": df_registros(r[(r["nivel"] == "local") & r["criticidad"].isin(["desabasto", "critico", "reordenar"])], 60, [
                "criticidad", "producto", "almacen", "hospital", "stock_actual", "reservado", "en_camino", "por_salir", "stock_proyectado",
                "primera_entrada_dia", "saldo_minimo", "fecha_quiebre", "fecha_necesaria", "demanda_diaria", "demanda_comprometida", "ajuste_agenda",
                "dias_con_agenda", "dias_cobertura", "sugerido", "unidad", "importe_sugerido", "metodo", "mape", "sesgo_pct", "confianza"]) if not r.empty else [],
            "compras": df_registros(r[(r["nivel"] == "red") & (r["sugerido"] > 0)], 60, [
                "criticidad", "producto", "unidad", "stock_actual", "reservado", "en_camino", "primera_entrada_dia", "stock_proyectado",
                "saldo_minimo", "fecha_quiebre", "fecha_necesaria", "demanda_diaria", "demanda_comprometida", "dias_cobertura", "lead_time_dias", "sugerido",
                "sugerido_compra", "unidad_compra", "llega_a_tiempo", "fecha_llegada_estimada", "importe_sugerido", "mape", "confianza"]) if not r.empty else [],
            "retrasadas": df_registros(res.get("retrasadas", pd.DataFrame()), 40),
            "transferencias": df_registros(res["rebalanceo"], 60),
            "caducidades": df_registros(res["caducidades"], 40),
            "abc": df_registros(res["abc_xyz"], 40, ["clase", "producto", "importe", "pct", "cv_semanal", "politica"]) if not res["abc_xyz"].empty else [],
            "historico": {k: v for k, v in res["historico"].items() if k != "semanal_ultimas_26"},
            "semanal": res["historico"].get("semanal_ultimas_26", []),
            "segundos": round(time.time() - t0, 1), "tiempos": tiempos, "fecha": db.now()})
        return {"corrida_id": corrida_id, "kpis": kpis, "informe": informe, "reporte": reporte, "acciones": acciones,
                "resumen": resumen, "segundos": round(time.time() - t0, 1), "tiempos": tiempos}
    except Exception as e:  # noqa: BLE001
        db.cerrar_corrida(corrida_id, "error", error=str(e))
        db.log("error", "agente2", "Falla en la corrida", str(e), usuario)
        raise


def _kpis(res: dict) -> dict:
    s = res.get("resumen", {})
    c = s.get("criticidad", {})
    return {"combinaciones": int(s.get("combinaciones", 0)), "desabasto": int(c.get("desabasto", 0)),
            "wape_ponderado": s.get("wape_ponderado"), "sesgo": s.get("sesgo_promedio"), "confianza": s.get("confianza", {}),
            "historico_insuficiente": int(s.get("historico_insuficiente", 0)), "dias_imputados": int(s.get("dias_imputados", 0)),
            "captura_atrasada_dias": int(s.get("captura_atrasada_dias", 0)), "entregas_retrasadas": int(s.get("entregas_retrasadas", 0)),
            "en_camino": float(s.get("en_camino_unidades", 0)), "demanda_comprometida": float(s.get("demanda_comprometida_total", 0)),
            "critico": int(c.get("critico", 0)), "reordenar": int(c.get("reordenar", 0)), "exceso": int(c.get("exceso", 0)),
            "sin_movimiento": int(c.get("sin_movimiento", 0)), "importe_compra": float(s.get("importe_compra_sugerida", 0)),
            "importe_interno": float(s.get("importe_resurtido_interno", 0)),
            "valor_inventario": float(s.get("valor_inventario_total", 0)),
            "valor_exceso": float(s.get("valor_inventario_exceso", 0)),
            "caducidad_riesgo": float(s.get("importe_caducidad_en_riesgo", 0)), "lotes_riesgo": int(s.get("lotes_en_riesgo", 0)),
            "transferencias": int(s.get("transferencias_propuestas", 0)), "wape": float(s.get("wape_promedio") or 0),
            "productos_por_comprar": int(s.get("productos_red_por_comprar", 0))}


def _informe(res: dict, df: pd.DataFrame, corrida_id: int, con_llm: bool, usuario: str, acciones: list[dict]) -> str:
    respaldo = _informe_deterministico(res, acciones)
    if not con_llm or not claude.disponible():
        return respaldo
    r = res["resurtido"]
    contexto = {
        "corrida": corrida_id, "dimension": res.get("dimension"), "resumen": res["resumen"],
        "periodo": {"desde": str(df["fecha"].min().date()), "hasta": str(df["fecha"].max().date()), "lineas": int(len(df))},
        "alertas": df_registros(r[(r["nivel"] == "local") & r["criticidad"].isin(["desabasto", "critico", "reordenar"])], 40, [
            "criticidad", "producto", "almacen", "stock_actual", "demanda_diaria", "dias_cobertura", "lead_time_dias",
            "punto_reorden", "sugerido", "importe_sugerido", "metodo", "mape", "ultimos_28d", "prev_28d"]),
        "compras_red": df_registros(r[(r["nivel"] == "red")], 40, [
            "criticidad", "producto", "stock_actual", "demanda_diaria", "dias_cobertura", "lead_time_dias", "sugerido",
            "importe_sugerido", "valor_inventario", "mape"]),
        "exceso": df_registros(r[(r["nivel"] == "local") & r["criticidad"].isin(["exceso", "sin_movimiento"])], 25, [
            "criticidad", "producto", "almacen", "stock_actual", "demanda_diaria", "dias_cobertura", "valor_inventario"]),
        "transferencias": df_registros(res["rebalanceo"], 40), "caducidades": df_registros(res["caducidades"], 30),
        "abc_xyz": df_registros(res["abc_xyz"], 30, ["clase", "producto", "importe", "pct", "cv_semanal", "politica"]) if not res["abc_xyz"].empty else [],
        "historico": {k: (v[-13:] if isinstance(v, list) else v) for k, v in res["historico"].items() if k != "semanal_ultimas_26"},
        "acciones_propuestas": [{k: a.get(k) for k in ("id", "estado", "riesgo")} | {"titulo": t} for a, t in
                                zip(acciones, [x.get("titulo", "") for x in acciones])][:40],
        "plan_razonado_por_el_planificador": {k: v for k, v in (res.get("plan_razonado") or {}).items() if k not in ("trazas",)},
        "autoevaluacion_pronostico_anterior": res.get("autoevaluacion") or {},
    }
    prompt = (f"Notas de aprendizaje del usuario (respétalas):\n{contexto_aprendizaje(['agente', 'producto', 'hospital', 'unidad'])}\n\n"
              f"Resultados del motor, decisiones del Agente Planificador y autoevaluación (JSON):\n{compacto(contexto)}\n\n"
              f"Redacta el plan de abastecimiento integrando las decisiones y riesgos anticipados por el planificador (cítalos), "
              f"y di con franqueza dónde falló el pronóstico anterior.")
    return claude.completar(prompts.SISTEMA_AGENTE_DEMANDA, prompt, origen="agente2", usuario=usuario, respaldo=respaldo)


def _informe_deterministico(res: dict, acciones: list[dict]) -> str:
    r, s = res["resurtido"], res["resumen"]
    k = _kpis(res)
    L = ["# Pronóstico de Demanda y Resurtido — plan automático", "",
         "## Resumen ejecutivo",
         f"- {k['combinaciones']} combinaciones producto × ubicación analizadas (nivel {res.get('dimension')}); "
         f"**{k['desabasto']} en desabasto, {k['critico']} críticas, {k['reordenar']} por reordenar, {k['exceso']} en exceso**.",
         f"- Compra sugerida a proveedor: **{mxn(k['importe_compra'])}** ({k['productos_por_comprar']} productos). "
         f"Resurtido interno: {mxn(k['importe_interno'])}.",
         f"- Inventario en red: {mxn(k['valor_inventario'])}; capital inmovilizado en exceso/sin movimiento: **{mxn(k['valor_exceso'])}**.",
         f"- Caducidades en riesgo: **{mxn(k['caducidad_riesgo'])}** en {k['lotes_riesgo']} lotes. "
         f"Error del pronóstico (WAPE semanal ponderado por volumen): {k.get('wape_ponderado') or k['wape']:.1f} %; "
         f"sesgo {k.get('sesgo') or 0:+.1f} % (positivo = el modelo subestima).",
         f"- Operación considerada: {k['en_camino']:,.0f} unidades en camino (compras confirmadas y transferencias), "
         f"{k['entregas_retrasadas']} entregas retrasadas (no se cuentan como abastecimiento), "
         f"{k['demanda_comprometida']:,.0f} unidades comprometidas por folios programados"
         + (f", captura atrasada {k['captura_atrasada_dias']} días" if k['captura_atrasada_dias'] > 1 else "") + ".", ""]
    if not r.empty:
        al = r[(r["nivel"] == "local") & r["criticidad"].isin(["desabasto", "critico"])]
        if not al.empty:
            L.append("## Alertas de desabasto")
            for _, x in al.head(15).iterrows():
                L.append(f"- **[{x['criticidad'].upper()}]** {x['producto']} en {x['almacen']}: utilizable {x['stock_actual']}"
                         + (f" + {x['en_camino']:.0f} en camino" if x['en_camino'] else "")
                         + f" = {x['stock_proyectado']:.0f} proyectado; demanda {x['demanda_diaria']:.2f}/día, cobertura {x['dias_cobertura']} días "
                         f"→ resurtir {x['sugerido']:.0f} {x['unidad']} (confianza {x['confianza']}).")
            L.append("")
        cp = r[(r["nivel"] == "red") & (r["sugerido"] > 0)]
        if not cp.empty:
            L.append("## Compras sugeridas (nivel red)")
            for _, x in cp.head(15).iterrows():
                uc = f" = {x['sugerido_compra']:.0f} {x['unidad_compra']}" if x.get("sugerido_compra") and x.get("unidad_compra") else ""
                L.append(f"- {x['producto']}: comprar {x['sugerido']:.0f} {x['unidad']}{uc} ({mxn(x['importe_sugerido'])}), "
                         f"lead time {x['lead_time_dias']:.0f} días, cobertura proyectada {x['dias_cobertura']} días"
                         + (f", {x['en_camino']:.0f} ya en camino" if x['en_camino'] else "") + ".")
            L.append("")
    plan = res.get("plan_razonado") or {}
    if plan:
        L.append("## Anticipación del planificador")
        L.append(f"- {plan.get('resumen', '')}")
        for d_ in plan.get("decisiones", [])[:8]:
            L.append(f"- **Decisión:** {d_.get('decision')} — {d_.get('por_que')}" + (f" ({d_.get('impacto')})" if d_.get("impacto") else ""))
        for r_ in plan.get("riesgos", [])[:8]:
            L.append(f"- **Riesgo:** {r_.get('riesgo')}" + (f" · fecha estimada {r_.get('fecha_estimada')}" if r_.get("fecha_estimada") else "")
                     + (f" · {r_.get('mitigacion')}" if r_.get("mitigacion") else ""))
        for n_ in plan.get("lo_que_el_modelo_no_ve", [])[:6]:
            L.append(f"- **No lo ve el pronóstico:** {n_}")
        L.append("")
    ev = res.get("autoevaluacion") or {}
    if ev.get("wape_global") is not None:
        L.append("## Autoevaluación del pronóstico anterior")
        L.append(f"- Del {ev['desde']} al {ev['hasta']} ({ev['dias']} días): error real {ev['wape_global']} % (sesgo {ev['sesgo_global']:+.1f} %). "
                 f"Peores: " + ", ".join(f"{p['producto']} {p['wape']:.0f} %" for p in ev.get("peores", [])[:4]) + ".")
        L.append("")
    rb = res["rebalanceo"]
    if not rb.empty:
        L.append("## Transferencias internas propuestas")
        for _, x in rb.head(15).iterrows():
            L.append(f"- {x['producto']}: {x['cantidad']:.0f} {x['unidad']} de {x['origen']} → {x['destino']} ({x['criticidad_destino']}).")
        L.append("")
    cd = res["caducidades"]
    if not cd.empty:
        L.append("## Caducidades en riesgo")
        for _, x in cd.head(10).iterrows():
            L.append(f"- {x['producto']} lote {x['lote']} en {x['almacen']}: caduca en {x['dias_para_caducar']} días, "
                     f"{x['en_riesgo']:.0f} unidades en riesgo ({mxn(x['importe_en_riesgo'])}).")
        L.append("")
    if acciones:
        L.append("## Acciones en cola de aprobación")
        L.extend(f"- #{a.get('id')} · {a.get('titulo', '')} · riesgo {a.get('riesgo', '')}" for a in acciones[:20] if a.get("id"))
    return "\n".join(L)


def _proponer(res: dict, corrida_id: int, usuario: str) -> list[dict]:
    """Convierte el plan en propuestas COHERENTES con el libro de compromisos:
      • transferencias: las del rebalanceo (ya son residuales: descuentan lo que otras propuestas cubren y respetan la
        reserva operativa del origen);
      • compras: sólo lo que no cubren ya solicitudes de compra pendientes del producto;
      • una necesidad que ya tenía propuesta pendiente se ACTUALIZA (nueva versión si cambió) en vez de duplicarse;
      • las propuestas de corridas anteriores cuya necesidad desapareció se caducan.
    Título, cantidad aprobada y cantidad enviada a Odoo salen del mismo payload, redondeado a la unidad."""
    if autonomia.nivel() == 0:
        return []
    maximo = int(autonomia.politicas().get("max_acciones_por_corrida", 60))
    prec = autonomia.precision_unidades()
    out: list[dict] = []
    claves: set[str] = set()
    try:
        ubic = queries.ubicaciones()
        id_por_nombre = {str(n).strip(): int(i) for i, n in zip(ubic["id"], ubic["nombre"])} if not ubic.empty else {}
    except Exception:  # noqa: BLE001
        id_por_nombre = {}

    def _id(nombre: str) -> int | None:
        if nombre in id_por_nombre:
            return id_por_nombre[nombre]
        for k, v in id_por_nombre.items():
            if k.lower() == str(nombre).lower() or k.lower().endswith("/" + str(nombre).lower()):
                return v
        return None

    nuevas = [0]

    def _registrar(r: dict) -> None:
        if r.get("id"):
            out.append(r)
            if not r.get("reutilizada"):
                nuevas[0] += 1

    # propuestas pendientes de corridas anteriores (por clave): la cantidad nueva es lo pendiente + el residual
    autonomia.depurar_duplicadas("demanda")
    pend = {a["clave"]: a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, agente="demanda", limite=2000) if a.get("clave")}
    rr = res["resurtido"]
    local = rr[rr["nivel"] == "local"] if not rr.empty else rr
    en_necesidad = {(int(x["producto_id"]), str(x["almacen"])) for _, x in local.iterrows()
                    if x["criticidad"] in ("desabasto", "critico", "reordenar") and float(x["sugerido"] or 0) > 0} if len(local) else set()
    red_en_necesidad = {int(x["producto_id"]) for _, x in rr[(rr["nivel"] == "red") & (rr["sugerido"] > 0)].iterrows()} if not rr.empty else set()

    # 1) transferencias internas (rebalanceo con asignación conjunta; cantidades residuales)
    rb = res["rebalanceo"]
    for _, x in (rb.iterrows() if not rb.empty else []):
        if nuevas[0] >= maximo:
            break
        o, d = _id(x["origen"]), _id(x["destino"])
        if not o or not d or x["cantidad"] <= 0:
            continue
        payload = {"producto_id": int(x["producto_id"]), "producto": x["producto"], "cantidad": float(x["cantidad"]), "unidad": x["unidad"],
                   "origen_id": o, "destino_id": d, "origen": x["origen"], "destino": x["destino"]}
        prev = pend.get(autonomia.clave_de("transferencia_interna", payload))
        if prev:   # el residual se SUMA a lo ya propuesto por esta misma clave (el motor ya lo había descontado)
            payload["cantidad"] = float(x["cantidad"]) + float((prev.get("payload") or {}).get("cantidad") or 0)
        r = autonomia.proponer("demanda", "transferencia_interna", "", payload, motivo=x["motivo"],
                               impacto={"cantidad": payload["cantidad"], "importe": round(payload["cantidad"] * (float(x["importe"]) / float(x["cantidad"]) if x["cantidad"] else 0.0), 2),
                                        "costo_unit": (float(x["importe"]) / float(x["cantidad"]) if x["cantidad"] else 0.0),
                                        "criticidad": x["criticidad_destino"], "unidad": x["unidad"],
                                        "cobertura_destino_antes": x.get("cobertura_destino_dias"),
                                        "cobertura_destino_despues": x.get("cobertura_resultante_dias"),
                                        "stock_origen": x.get("stock_origen"), "disponible_origen": x.get("disponible_origen"),
                                        "reserva_origen": x.get("reserva_origen"), "cubierto_previo_destino": x.get("cubierto_previo_destino"),
                                        "cobertura_origen_despues": x.get("cobertura_origen_resultante"),
                                        "fecha_quiebre": x.get("fecha_quiebre_destino"), "fecha_necesaria": x.get("fecha_necesaria"),
                                        "fecha_llegada_estimada": x.get("fecha_llegada_estimada"), "llega_a_tiempo": x.get("llega_a_tiempo"),
                                        "holgura_dias": x.get("holgura_dias")},
                               corrida_id=corrida_id, usuario=usuario, fecha_requerida=x.get("fecha_necesaria"))
        claves.add(autonomia.clave_de("transferencia_interna", payload))
        _registrar(r)
    # 2) compras a proveedor (nivel red) — sólo el residual no cubierto por solicitudes pendientes
    comp_red = (res.get("compromisos") or {}).get("red", {})
    try:
        con_proveedor = set(queries.lead_times().keys())      # productos con proveedor configurado en Odoo
    except Exception:  # noqa: BLE001
        con_proveedor = None
    if not rr.empty:
        for _, x in rr[(rr["nivel"] == "red") & (rr["sugerido"] > 0)].iterrows():
            if nuevas[0] >= maximo:
                break
            pid = int(x["producto_id"])
            ya = float(comp_red.get(pid, 0.0))
            residual = P.redondear(max(0.0, float(x["sugerido"]) - ya), str(x["unidad"]), prec, arriba=True)
            prev = pend.get(autonomia.clave_de("solicitud_compra", {"producto_id": pid}))
            if residual <= 0:
                continue
            if prev:
                residual = residual + float((prev.get("payload") or {}).get("cantidad") or 0)
            ratio = None
            uc = x.get("unidad_compra")
            if x.get("sugerido_compra") and x["sugerido"]:
                ratio = float(x["sugerido"]) / float(x["sugerido_compra"]) if float(x["sugerido_compra"]) else None
            cant_compra = float(math.ceil(residual / ratio - 1e-9)) if (ratio and ratio > 1) else None
            sin_prov = bool(con_proveedor is not None and pid not in con_proveedor)
            payload = {"producto_id": pid, "producto": x["producto"], "cantidad": residual, "unidad": x["unidad"],
                       "cantidad_compra": cant_compra, "unidad_compra": uc, "conversion_faltante": bool(x.get("conversion_faltante")),
                       "sin_proveedor": sin_prov}
            llega = x.get("llega_a_tiempo")
            motivo = (f"Cobertura proyectada de red {x['dias_cobertura']} días vs. lead time {x['lead_time_dias']:.0f}; "
                      f"demanda {x['demanda_diaria']:.2f}/día; en camino {x['en_camino']:.0f}; punto de reorden {x['punto_reorden']:.0f}; "
                      f"confianza del pronóstico {x['confianza']}."
                      + (f" Ya hay {ya:g} {x['unidad']} en solicitudes pendientes; se pide sólo el resto." if ya else "")
                      + (f" ATENCIÓN: con el plazo del proveedor llegaría el {x.get('fecha_llegada_estimada')}, DESPUÉS del quiebre ({x.get('fecha_quiebre')}); "
                         "no evita el faltante por sí sola: hace falta transferencia interna o reclamar una entrega." if llega is False else "")
                      + (" Producto sin proveedor configurado en Odoo: la RFQ se creará con el proveedor en blanco (o «por definir») para que Compras lo asigne."
                         if sin_prov else ""))
            r = autonomia.proponer("demanda", "solicitud_compra", "", payload, motivo=motivo,
                                   impacto={"cantidad": residual, "importe": round(residual * float(x["costo_unit"]), 2), "costo_unit": float(x["costo_unit"]),
                                            "criticidad": x["criticidad"], "unidad": x["unidad"],
                                            "cobertura_red_antes": x.get("dias_cobertura"),
                                            "cobertura_red_despues": (round((float(x["stock_proyectado"]) + residual + ya) / float(x["demanda_diaria"]), 1) if x["demanda_diaria"] else None),
                                            "lead_time": x.get("lead_time_dias"), "en_camino": x.get("en_camino"), "cubierto_previo": ya,
                                            "unidad_compra": uc, "sugerido_compra": cant_compra, "confianza": x.get("confianza"),
                                            "llega_a_tiempo": llega, "holgura_dias": x.get("holgura_dias"), "fecha_llegada_estimada": x.get("fecha_llegada_estimada"),
                                            "fecha_quiebre": x.get("fecha_quiebre"), "fecha_necesaria": x.get("fecha_necesaria")},
                                   corrida_id=corrida_id, usuario=usuario, fecha_requerida=x.get("fecha_necesaria"))
            claves.add(autonomia.clave_de("solicitud_compra", payload))
            _registrar(r)
        # 3) reglas min/max para ubicaciones con demanda estable que hoy están en riesgo (clase X/Y)
        abc = res["abc_xyz"]
        estables = set(abc[abc["xyz"].isin(["X", "Y"])]["producto_id"]) if not abc.empty else set()
        cand = rr[(rr["nivel"] == "local") & rr["criticidad"].isin(["critico", "reordenar", "desabasto"])
                  & rr["producto_id"].isin(estables)]
        for _, x in cand.iterrows():
            if nuevas[0] >= maximo:
                break
            u = _id(x["almacen"])
            if not u:
                continue
            minimo = P.redondear(float(x["punto_reorden"]), str(x["unidad"]), prec, arriba=True)
            maximo_ = P.redondear(float(x["punto_reorden"] + x["demanda_diaria"] * 14), str(x["unidad"]), prec, arriba=True)
            payload = {"producto_id": int(x["producto_id"]), "producto": x["producto"], "ubicacion_id": u, "minimo": minimo,
                       "maximo": maximo_, "ubicacion": x["almacen"], "unidad": x["unidad"]}
            r = autonomia.proponer("demanda", "regla_reabastecimiento",
                                   f"Regla min/max {x['producto']} en {x['almacen']}: {autonomia.formato_cantidad(minimo, x['unidad'])} / {autonomia.formato_cantidad(maximo_, x['unidad'])}",
                                   payload, motivo=f"Demanda estable ({x['metodo']}, WAPE {x['mape']} %); hoy en «{x['criticidad']}».",
                                   impacto={"cantidad": maximo_, "importe": round(float(maximo_ * x["costo_unit"]), 2), "minimo": minimo,
                                            "maximo": maximo_, "criticidad": x["criticidad"], "confianza": x.get("confianza"), "unidad": x["unidad"]},
                                   corrida_id=corrida_id, usuario=usuario)
            claves.add(autonomia.clave_de("regla_reabastecimiento", payload))
            _registrar(r)
    # 4) propuestas pendientes anteriores que siguen vigentes (la necesidad existe aunque el residual sea 0) vs. caducadas
    for clave, a in pend.items():
        if clave in claves:
            continue
        p = a.get("payload") or {}
        pid = int(p.get("producto_id") or 0)
        vigente = ((a["tipo"] == "transferencia_interna" and (pid, str(p.get("destino"))) in en_necesidad)
                   or (a["tipo"] == "solicitud_compra" and pid in red_en_necesidad)
                   or (a["tipo"] == "regla_reabastecimiento" and (pid, str(p.get("ubicacion"))) in en_necesidad))
        if vigente:
            db.actualizar_accion(a["id"], ultima_corrida_id=corrida_id)
            claves.add(clave)
            out.append({"id": a["id"], "estado": "vigente", "riesgo": a.get("riesgo"), "titulo": a.get("titulo"), "reutilizada": True,
                        "version": a.get("version") or 1})
    res["caducadas"] = autonomia.caducar_no_vigentes("demanda", corrida_id, claves)
    return out
