"""Agente 1 · Control Inteligente de Consumo — orquestación."""
from __future__ import annotations

import json
import threading
import time

import pandas as pd

from .. import db
from ..config import settings
from ..llm import claude, prompts
from ..ml import anomalias as A
from ..odoo import queries, schema
from ..reports import builders
from . import autonomia, investigador
from .base import Cronometro, compacto, contexto_aprendizaje, df_registros, mxn

NOMBRE = "consumo"
_CANDADO = threading.Lock()   # una sola corrida a la vez (manual, programada o desde el copiloto)


REGLAS_SIN_FOLIO = {"R04_SIN_CIRUGIA", "R08_DUPLICADO"}   # requieren folio médico real; en respaldo son ruido


def modo_respaldo(df: pd.DataFrame | None = None) -> bool:
    """True si el consumo viene de movimientos de inventario (stock.move.line) y no del módulo de folios de CBH.
    Se decide por lo que realmente se leyó (columna ``origen_datos``) y, si no hay datos, por el mapeo vigente."""
    if df is not None and len(df) and "origen_datos" in df.columns:
        return bool((df["origen_datos"].astype(str) == "stock.move.line").all())
    return schema.en_respaldo()


def _config_desde_aprendizaje(respaldo: bool = False) -> A.ConfigAnomalias:
    cfg = A.ConfigAnomalias(umbral_z=settings.AGENT1_SEVERITY_THRESHOLD, min_muestras=settings.AGENT1_MIN_SAMPLES)
    ajustes = db.get_ajuste("agente1_config", {}) or {}
    for k, v in ajustes.items():
        if hasattr(cfg, k) and not isinstance(getattr(cfg, k), (dict, set)):
            setattr(cfg, k, type(getattr(cfg, k))(v))
    # reglas desactivadas por política (lista en agente1_config) y por modo de respaldo
    cfg.reglas_desactivadas = {str(r) for r in (ajustes.get("reglas_desactivadas") or [])}
    if respaldo:
        cfg.reglas_desactivadas |= REGLAS_SIN_FOLIO
    # densidad / contenido: primero datos maestros del producto en Odoo, luego ajustes manuales (ganan)
    try:
        cat = queries.catalogo_productos()
        for c_log, destino in (("densidad", cfg.densidad), ("capacidad", cfg.capacidad)):
            if c_log in cat.columns:
                for pid, v in zip(cat["producto_id"], cat[c_log]):
                    try:
                        if v not in (None, False) and float(v) > 0:
                            destino[int(pid)] = float(v)
                    except (TypeError, ValueError):
                        continue
    except Exception as e:  # noqa: BLE001
        db.log("warn", "agente1", "No se pudieron leer densidad/contenido del catálogo", str(e))
    cfg.densidad.update({int(k): float(v) for k, v in (db.get_ajuste("densidades", {}) or {}).items()})
    cfg.capacidad.update({int(k): float(v) for k, v in (db.get_ajuste("capacidades", {}) or {}).items()})
    # umbrales aprendidos por perfil
    for k, p in db.perfiles().items():
        if p.get("ajuste_umbral"):
            cfg.ajustes[(p["ambito"], p["clave"])] = float(p["ajuste_umbral"])
    # huellas ya justificadas/descartadas por humanos
    with db.conn() as con:
        cfg.huellas_justificadas = {r["huella"] for r in con.execute(
            "SELECT huella FROM anomalias WHERE estado IN ('justificada','descartada') AND huella IS NOT NULL")}
    return cfg


def ejecutar(dias: int | None = None, usuario: str = "", disparo: str = "manual", con_llm: bool = True,
             generar_excel: bool = True, proponer_acciones: bool = True, progreso=None) -> dict:
    if not _CANDADO.acquire(blocking=False):
        raise RuntimeError("El Agente 1 ya está en ejecución; espera a que termine.")
    if con_llm and settings.LLM_ENABLED and db.presupuesto_agotado():
        _CANDADO.release()
        raise RuntimeError(db.mensaje_presupuesto_agotado())
    try:
        return _ejecutar(dias, usuario, disparo, con_llm, generar_excel, proponer_acciones, progreso or (lambda *a: None))
    finally:
        _CANDADO.release()


def _ejecutar(dias, usuario, disparo, con_llm, generar_excel, proponer_acciones, progreso) -> dict:
    dias = dias or settings.AGENT1_LOOKBACK_DAYS
    corrida_id = db.iniciar_corrida(NOMBRE, {"dias": dias, "con_llm": con_llm}, disparo, usuario)
    t0 = time.time()
    progreso = Cronometro(progreso)          # mide cada paso: la bitácora dice en qué se fue el tiempo
    try:
        progreso("Conectando a Odoo y leyendo el consumo", f"últimos {dias} días")
        df = queries.consumo(dias=dias, progreso=progreso)
        progreso("Consumo leído", f"{len(df):,} líneas · {df['folio'].nunique() if len(df) else 0:,} folios")
        respaldo = modo_respaldo(df)
        if respaldo:
            progreso("Modo de respaldo", "sin módulo de folios: se analizan movimientos de inventario; R04/R08 y tickets de facturación desactivados")
            db.log("warn", "agente1", "Corrida en modo de respaldo (stock.move.line): no se detectó el modelo de folios de CBH",
                   "Se desactivan R04 (sin cirugía), R08 (duplicado) y los tickets de facturación. Revisa Configuración ▸ Re-descubrir modelos.",
                   usuario)
        cfg = _config_desde_aprendizaje(respaldo)
        progreso("Aplicando lo aprendido", f"{len(cfg.ajustes)} umbrales ajustados · {len(cfg.huellas_justificadas)} hallazgos ya justificados")
        progreso("Construyendo perfiles y detectando anomalías", "perfiles robustos · Isolation Forest · reglas · patrones")
        res = A.detectar(df, cfg)
        res["modo_respaldo"] = respaldo
        res["reglas_desactivadas"] = sorted(cfg.reglas_desactivadas)
        h, ag = res["hallazgos"], res["agregados"]
        progreso("Detección terminada", f"{len(h)} hallazgos · {len(ag)} patrones · {res['basculas'].get('discrepancias', 0)} discrepancias de báscula")

        # ── persistir perfiles aprendidos (baseline que se refina en cada corrida) ──
        for clave, p in res["perfiles"].items():
            if p["ambito"] in ("producto", "producto_hospital", "producto_auxiliar", "producto_medico", "producto_subalmacen"):
                db.guardar_perfil(p["ambito"], p["clave"], {k: v for k, v in p.items() if k not in ("ambito", "clave")})

        # ── persistir hallazgos nuevos (dedupe por huella) ──
        conocidas = db.huellas_conocidas()
        nuevos = h[~h["huella"].isin(conocidas)] if not h.empty else h
        db.guardar_anomalias(A.hallazgos_a_registros(nuevos, corrida_id))
        recurrentes = int(len(h) - len(nuevos))

        # ── investigación de casos (expedientes) ──
        progreso("Investigando los casos de mayor impacto", "folio · historial · báscula · lote · casos similares")
        casos = investigador.investigar_corrida(res, df, corrida_id, usuario=usuario, con_llm=con_llm, progreso=progreso,
                                                densidades=cfg.densidad)
        res["casos"] = casos

        # ── narrativa ──
        progreso("Redactando el informe", "con Claude" if (con_llm and claude.disponible()) else "informe determinista")
        informe = _informe(res, df, corrida_id, con_llm, usuario, nuevos_n=len(nuevos), recurrentes=recurrentes)

        # ── acciones propuestas ──
        progreso("Proponiendo acciones para aprobación", "")
        acciones = _proponer(res, corrida_id, usuario) if proponer_acciones else []

        # ── Excel ──
        progreso("Generando el Excel", "")
        reporte = builders.reporte_anomalias(res, informe, {"id": corrida_id}, usuario) if generar_excel else None

        tiempos = progreso.cerrar()
        db.log("info", "agente1", f"Tiempos de la corrida #{corrida_id} ({round(time.time() - t0)} s)", progreso.resumen(), usuario)
        kpis = _kpis(res, nuevos_n=len(nuevos), recurrentes=recurrentes)
        n_acc = len(acciones) + sum(len(c.get("acciones") or []) for c in casos)
        resumen = (f"{res['n_lineas']:,} líneas · {len(h)} hallazgos ({len(nuevos)} nuevos) · {len(ag)} patrones · "
                   f"{len(casos)} expedientes · en revisión {mxn(kpis['importe_riesgo'])} · {n_acc} acciones propuestas")
        db.cerrar_corrida(corrida_id, "ok", res["n_lineas"], int(len(h) + len(ag)), resumen)
        db.set_ajuste("agente1_ultimo", {"corrida_id": corrida_id, "kpis": kpis, "informe": informe, "casos": casos,
                                          "modo_respaldo": respaldo, "reglas_desactivadas": res["reglas_desactivadas"],
                                          "origen_datos": ("stock.move.line" if respaldo else
                                                           (str(df["origen_datos"].iloc[0]) if len(df) and "origen_datos" in df.columns else "")),
                                          "exposicion": db.exposicion_economica(),
                                          "reporte": reporte, "riesgos": res["riesgos"],
                                          "basculas": res["basculas"], "historico": _hist_compacto(res["historico"]),
                                          "agregados": df_registros(ag, 40),
                                          "top_hallazgos": df_registros(h, 40, [
                                              "severidad", "score", "fecha", "folio", "hospital", "subalmacen", "medico",
                                              "auxiliar", "producto", "cantidad", "unidad", "esperado", "desviacion",
                                              "importe_riesgo", "motivos", "huella"]),
                                          "segundos": round(time.time() - t0, 1), "tiempos": tiempos, "fecha": db.now()})
        return {"corrida_id": corrida_id, "kpis": kpis, "informe": informe, "reporte": reporte, "acciones": acciones,
                "resumen": resumen, "segundos": round(time.time() - t0, 1), "tiempos": tiempos}
    except Exception as e:  # noqa: BLE001
        db.cerrar_corrida(corrida_id, "error", error=str(e))
        db.log("error", "agente1", "Falla en la corrida", str(e), usuario)
        raise


def _kpis(res: dict, nuevos_n: int = 0, recurrentes: int = 0) -> dict:
    h, ag = res["hallazgos"], res["agregados"]
    sev = h["severidad"].value_counts().to_dict() if not h.empty else {}
    return {
        "lineas": int(res["n_lineas"]), "hallazgos": int(len(h)), "nuevos": int(nuevos_n), "recurrentes": int(recurrentes),
        "criticos": int(sev.get("critica", 0)), "altos": int(sev.get("alta", 0)), "medios": int(sev.get("media", 0)),
        "patrones": int(len(ag)),
        "importe_riesgo": round(float(h["importe_riesgo"].sum()) if not h.empty else 0.0, 2),
        "importe_patrones": round(float(ag["importe_riesgo"].sum()) if not ag.empty else 0.0, 2),
        "basculas": res.get("basculas", {}),
        "top_riesgo_auxiliar": (res["riesgos"].get("auxiliar") or [{}])[0].get("clave"),
        "top_riesgo_hospital": (res["riesgos"].get("hospital") or [{}])[0].get("clave"),
    }


def _hist_compacto(hist: dict) -> dict:
    return {"mensual": hist.get("mensual", [])[-13:], "tendencias_producto": hist.get("tendencias_producto", [])[:12],
            "comparativo_hospital": hist.get("comparativo_hospital", [])[:12],
            "estacionalidad_semana": hist.get("estacionalidad_semana", {}),
            "cambios_estructurales": hist.get("cambios_estructurales", []), "interanual": hist.get("interanual", [])}


def _informe(res: dict, df: pd.DataFrame, corrida_id: int, con_llm: bool, usuario: str,
             nuevos_n: int, recurrentes: int) -> str:
    h, ag = res["hallazgos"], res["agregados"]
    respaldo = _informe_deterministico(res, nuevos_n, recurrentes)
    if not con_llm or not claude.disponible():
        return respaldo
    contexto = {
        "corrida": corrida_id, "periodo": {"desde": str(df["fecha"].min().date()), "hasta": str(df["fecha"].max().date()),
                                           "lineas": int(len(df)), "folios": int(df["folio"].nunique()),
                                           "importe_total": round(float(df["importe"].sum()), 2)},
        "kpis": _kpis(res, nuevos_n, recurrentes),
        "hallazgos_top": df_registros(h, 30, ["severidad", "score", "fecha", "folio", "hospital", "subalmacen", "medico",
                                             "auxiliar", "producto", "lote", "cantidad", "unidad", "esperado", "desviacion",
                                             "importe_riesgo", "motivos", "duracion_min", "tasa"]),
        "conteo_por_metodo": _conteo_metodos(h),
        "patrones": df_registros(ag, 25),
        "riesgos": {k: v[:8] for k, v in res["riesgos"].items()},
        "basculas": res["basculas"], "historico": _hist_compacto(res["historico"]),
        "descripcion_reglas": A.DESCRIPCION_REGLAS,
        "expedientes_investigados": [{**c, "expediente": (db.caso(c["id"]) or {}).get("expediente")} for c in (res.get("casos") or [])[:8]],
        "modo_respaldo": bool(res.get("modo_respaldo")), "reglas_desactivadas": res.get("reglas_desactivadas", []),
    }
    nota_respaldo = ""
    if res.get("modo_respaldo"):
        nota_respaldo = ("\n\nIMPORTANTE: esta corrida se hizo en MODO DE RESPALDO sobre movimientos de inventario (stock.move.line), "
                         "no sobre folios de operación médica: no hay médico, auxiliar, paciente, báscula, duración ni importe, y el "
                         "«folio» es el nombre de la entrega de almacén. No hables de facturación al IMSS, de consumos sin cirugía ni "
                         "de duplicados; dilo explícitamente al inicio y limita el informe a lo que sí se puede afirmar con esos datos.")
    prompt = (f"Notas de aprendizaje del usuario (respétalas):\n{contexto_aprendizaje(['agente', 'producto', 'hospital', 'medico', 'auxiliar', 'unidad'])}\n\n"
              f"Resultados del motor y expedientes ya investigados por el Agente Investigador (JSON):\n{compacto(contexto)}\n\n"
              f"Redacta el informe apoyándote en los expedientes: cita sus hipótesis y conclusiones, no repitas sólo las cifras.{nota_respaldo}")
    return claude.completar(prompts.SISTEMA_AGENTE_CONSUMO, prompt, origen="agente1", usuario=usuario, respaldo=respaldo)


def _conteo_metodos(h: pd.DataFrame) -> dict:
    if h.empty:
        return {}
    c: dict[str, int] = {}
    for ms in h["metodos"]:
        for m in ms:
            c[m] = c.get(m, 0) + 1
    return dict(sorted(c.items(), key=lambda x: -x[1]))


def _informe_deterministico(res: dict, nuevos_n: int, recurrentes: int) -> str:
    h, ag, r = res["hallazgos"], res["agregados"], res["riesgos"]
    k = _kpis(res, nuevos_n, recurrentes)
    b = res["basculas"]
    L = [f"# Control Inteligente de Consumo — informe automático",
         ""]
    if res.get("modo_respaldo"):
        L += ["> **Modo de respaldo.** No se detectó el módulo de folios de operación médica de CBH, así que esta corrida "
              "analizó **movimientos de inventario** (`stock.move.line`): no hay médico, auxiliar, básculas, duración ni importe, "
              "y el «folio» es el nombre de la entrega de almacén. Las reglas *sin cirugía* (R04) y *duplicado* (R08) y los "
              "tickets de facturación quedaron desactivados. Configuración ▸ *Re-descubrir modelos* corrige esto.",
              ""]
    L += ["## Resumen ejecutivo",
         f"- Se analizaron **{k['lineas']:,}** líneas de consumo. Hallazgos: **{k['hallazgos']}** "
         f"({k['criticos']} críticos, {k['altos']} altos, {k['medios']} medios); {nuevos_n} nuevos y {recurrentes} ya conocidos.",
         f"- Patrones agregados detectados: **{k['patrones']}**. Importe en riesgo: **{mxn(k['importe_riesgo'])}** por línea "
         f"y **{mxn(k['importe_patrones'])}** por patrones.",
         f"- Básculas: {b.get('lineas_con_bascula', 0):,} líneas con pesaje, {b.get('imposibles', 0)} lecturas imposibles, "
         f"{b.get('discrepancias', 0)} discrepancias, {b.get('gramos_no_explicados', 0):,.1f} g no explicados.",
         ""]
    casos = res.get("casos") or []
    if casos:
        L.append("## Expedientes investigados")
        for c in casos[:8]:
            L.append(f"- **[{(c.get('severidad') or '').upper()}] {c['titulo']}** — hipótesis principal: {c.get('hipotesis_principal')}; "
                     f"confianza {c.get('confianza')}; impacto {mxn(c.get('impacto_mxn'))}. Acción: {c.get('accion_recomendada')} "
                     f"(responsable: {c.get('responsable')}).")
        L.append("")
    if not ag.empty:
        L.append("## Patrones que requieren verificación")
        for _, p in ag.head(10).iterrows():
            L.append(f"- **[{p['severidad'].upper()}] {A.DESCRIPCION_REGLAS.get(p['regla'], p['regla'])}** — {p['motivo']} "
                     f"Importe en riesgo: {mxn(p['importe_riesgo'])}.")
        L.append("")
    if not h.empty:
        L.append("## Hallazgos críticos y altos (top 12)")
        for _, x in h[h["severidad"].isin(["critica", "alta"])].head(12).iterrows():
            L.append(f"- **[{x['severidad'].upper()}]** {x['fecha']} · {x['folio']} · {x['hospital']} · {x['producto']} · "
                     f"{x['cantidad']:.1f} {x['unidad']}" + (f" (esperado {x['esperado']:.1f})" if x['esperado'] is not None else "")
                     + f" — {' | '.join(x['motivos'][:3])}")
        L.append("")
    for dim, titulo in (("auxiliar", "auxiliar"), ("hospital", "unidad médica"), ("subalmacen", "sub-almacén")):
        top = (r.get(dim) or [])[:5]
        if top:
            L.append(f"## Índice de riesgo por {titulo}")
            L.extend(f"- {t['clave']}: índice {t['indice']} · {t['hallazgos']} hallazgos · en riesgo {mxn(t['importe_riesgo'])}" for t in top)
            L.append("")
    hist = res["historico"]
    if hist.get("tendencias_producto"):
        L.append("## Tendencias históricas")
        for t in hist["tendencias_producto"][:6]:
            L.append(f"- {t['producto']}: {t['tendencia_pct_mes']:+.1f} % por mes"
                     + (f", últimos 3 meses {t['variacion_pct']:+.1f} % vs. previos" if t.get("variacion_pct") is not None else ""))
        L.append("")
    if res.get("modo_respaldo"):
        L += ["## Siguientes pasos",
              "1. Ingeniería Cóndor: Configuración ▸ Re-descubrir modelos y campos (o dar al usuario técnico acceso al módulo de folios).",
              "2. Inventarios: conciliar existencias físicas de los sub-almacenes con mayor índice de riesgo.",
              "3. Cadena de suministro: revisar lotes fuera de su unidad habitual y caducados.",
              "4. Registrar en el copiloto las justificaciones válidas para que el agente aprenda."]
    else:
        L += ["## Siguientes pasos",
              "1. Operaciones: verificar en sitio los patrones críticos (báscula, consumos sin cirugía) esta semana.",
              "2. Inventarios: conciliar existencias físicas de los sub-almacenes con mayor índice de riesgo.",
              "3. Cadena de suministro: revisar lotes fuera de su unidad habitual y caducados.",
              "4. Contabilidad: retener de la facturación IMSS los folios con duplicados o consumos sin cirugía hasta aclarar.",
              "5. Registrar en el copiloto las justificaciones válidas para que el agente aprenda."]
    return "\n".join(L)


def _proponer(res: dict, corrida_id: int, usuario: str, maximo: int = 12) -> list[dict]:
    """Acciones para la cola: alertas por patrón crítico y actividades en Odoo cuando hay folio."""
    if autonomia.nivel() == 0:
        return []
    out: list[dict] = []
    ag, h = res["agregados"], res["hallazgos"]
    for _, p in ag.iterrows():
        if len(out) >= maximo:
            break
        if p["severidad"] in ("critica", "alta"):
            out.append(autonomia.proponer(
                "consumo", "alerta", f"Verificar patrón: {A.DESCRIPCION_REGLAS.get(p['regla'], p['regla'])} · {p['clave']}",
                {"regla": p["regla"], "clave": p["clave"], "producto": p["producto"], "hospital": p["hospital"]},
                motivo=p["motivo"], impacto={"importe": float(p["importe_riesgo"])}, corrida_id=corrida_id, usuario=usuario))
    # las acciones concretas en Odoo (tickets, cuarentena, conteo) las propone el investigador a partir de cada expediente
    return out


# ── retroalimentación (aprendizaje) ─────────────────────────────────────────
def retroalimentar(anomalia_id: int, estado: str, nota: str, usuario: str) -> dict:
    """La retroalimentación humana mueve los umbrales de los perfiles implicados (siempre trazable):
    justificada/descartada → +0.15 al umbral (más tolerante); confirmada → −0.15 (más estricto).
    Repetir la misma clasificación sobre el mismo hallazgo NO vuelve a mover el umbral."""
    with db.conn() as con:
        a = con.execute("SELECT * FROM anomalias WHERE id=?", (anomalia_id,)).fetchone()
    if not a:
        raise ValueError("Hallazgo no encontrado.")
    a = dict(a)
    estado_previo = a.get("estado")
    db.clasificar_anomalia(anomalia_id, estado, nota, usuario)
    ajustado = []
    efecto_previo = {"justificada": 0.15, "descartada": 0.15, "confirmada": -0.15}.get(estado_previo, 0.0)
    efecto_nuevo = {"justificada": 0.15, "descartada": 0.15, "confirmada": -0.15}.get(estado, 0.0)
    delta = efecto_nuevo - efecto_previo          # deshace el efecto anterior y aplica el nuevo
    if delta and a.get("producto_id"):
        pid = a["producto_id"]
        for ambito, clave in (("producto", f"{pid}"), ("producto_hospital", f"{pid}|{a.get('hospital')}"),
                              ("producto_medico", f"{pid}|{a.get('medico')}")):
            for metrica in ("cantidad", "tasa"):
                db.ajustar_umbral_perfil(ambito, f"{clave}|{metrica}", delta, delta > 0)
                ajustado.append(f"{ambito}:{clave}|{metrica}")
        db.log("info", "aprendizaje", f"Umbral ajustado {delta:+.2f} por hallazgo #{anomalia_id} ({estado_previo} → {estado})",
               ", ".join(ajustado[:6]), usuario)
    if nota:
        db.agregar_aprendizaje("hospital" if a.get("hospital") else "global", a.get("hospital") or "",
                               f"Hallazgo #{anomalia_id} ({a.get('producto')}, {a.get('folio')}) marcado como {estado}: {nota}",
                               usuario=usuario)
    db.log("info", "agente1", f"Retroalimentación en hallazgo #{anomalia_id}: {estado}", nota, usuario)
    return {"id": anomalia_id, "estado": estado, "estado_previo": estado_previo, "delta_umbral": delta,
            "perfiles_ajustados": len(ajustado)}
