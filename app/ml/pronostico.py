"""Motor de pronóstico de demanda y resurtido (Agente 2).

Para cada producto × ubicación construye la serie diaria de consumo, prueba varios métodos
(media móvil, suavizado exponencial, Holt amortiguado, Holt-Winters semanal, estacional ingenuo,
Croston/SBA para demanda intermitente), los evalúa con *backtesting* de origen móvil (WAPE por
bloques semanales y sesgo) y se queda con el mejor. Sobre ese pronóstico calcula el inventario
proyectado usando la operación completa:

  existencia UTILIZABLE (sin reservas ni lotes caducados)
  + compras confirmadas y transferencias pendientes que llegan dentro del horizonte
  − salidas pendientes desde la ubicación
  − demanda pronosticada, con piso de la demanda COMPROMETIDA por folios programados

y de ahí stock de seguridad (z·σ·√LT con variabilidad del plazo), punto de reorden, cobertura,
cantidad sugerida en unidad base y en unidad de COMPRA (mL → frascos), criticidad y confianza.
Además clasifica ABC/XYZ, proyecta caducidades (FEFO), propone rebalanceos entre almacenes y
señala entregas retrasadas. Todo es determinista y auditable; el LLM sólo lo explica.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

Z_SERVICIO = {0.90: 1.2816, 0.95: 1.6449, 0.97: 1.8808, 0.98: 2.0537, 0.99: 2.3263}


@dataclass
class ConfigPronostico:
    horizonte_dias: int = 30
    historia_dias: int = 540
    nivel_servicio: float = 0.95
    lead_time_default: float = 7.0
    lead_time_interno: float = 2.0          # hospital ← CEDIS
    sigma_lt_pct: float = 0.20              # variabilidad del plazo del proveedor (fracción del LT)
    dimension: str = "subalmacen"           # subalmacen | almacen | hospital
    min_dias_historia: int = 28
    min_dias_con_demanda: int = 5
    folds_backtest: int = 3
    horizonte_backtest: int = 14
    # Backtesting completo (6 métodos × folds) sólo para las combinaciones producto × ubicación más activas; el resto
    # usa el método rápido con una sola validación. Acota el tiempo en redes grandes sin perder precisión donde importa.
    max_backtests: int = 3000
    multiplo_default: float = 1.0
    ciclo_revision_dias: int = 7            # cada cuántos días se resurte un sub-almacén
    dias_sin_movimiento: int = 90
    exceso_cobertura_dias: int = 120
    umbral_sin_captura: float = 0.10        # si ≤10 % de los días hábiles de la ubicación están vacíos, se tratan como falta de captura (0 = desactivar)
    usar_utilizable: bool = True            # existencia sin reservas ni caducados
    lead_times: dict[int, float] = field(default_factory=dict)
    multiplos: dict[int, float] = field(default_factory=dict)
    es_cedis: set[str] = field(default_factory=set)
    regiones: dict[str, str] = field(default_factory=dict)
    usar_statsmodels: bool = False
    hoy: pd.Timestamp | None = None         # ancla temporal (por defecto, ahora)
    precision: dict[str, int] = field(default_factory=dict)   # unidad → decimales (de uom.uom.rounding)
    reserva_cedis_dias: int = 7             # el CEDIS conserva la demanda de su región durante estos días (reserva operativa)

    @property
    def z(self) -> float:
        return Z_SERVICIO.get(round(self.nivel_servicio, 2), 1.6449)


# ── unidades: redondeo coherente en todo el flujo ───────────────────────────
_DECIMALES_POR_NOMBRE = {"ml": 1, "l": 2, "kg": 2, "g": 1, "mg": 1}


def decimales_de(unidad: str, precision: dict[str, int] | None = None) -> int:
    """Decimales admitidos por la unidad: los definidos en Odoo (uom.rounding) o, si no se conocen, por el nombre
    (mililitros/kilos con decimales; piezas, pares, frascos, cajas, ampolletas enteros)."""
    if precision and unidad in precision:
        return int(precision[unidad])
    return _DECIMALES_POR_NOMBRE.get(str(unidad or "").strip().lower(), 0)


def redondear(cantidad: float, unidad: str, precision: dict[str, int] | None = None, arriba: bool = False, abajo: bool = False) -> float:
    """Redondea a la precisión de la unidad: hacia arriba cuando es una cantidad a abastecer (`arriba`), hacia abajo
    cuando es un tope de disponibilidad que no debe excederse (`abajo`)."""
    dec = decimales_de(unidad, precision)
    m = 10 ** dec
    q = float(cantidad) * m
    q = math.ceil(q - 1e-9) if arriba else (math.floor(q + 1e-9) if abajo else round(q))
    return float(q) / m


# ── métodos de pronóstico (vector de h días) ────────────────────────────────
def _media_movil(y: np.ndarray, h: int, dow0: int = 0, ventana: int = 28) -> np.ndarray:
    v = y[-ventana:] if len(y) >= ventana else y
    return np.full(h, max(0.0, float(v.mean())))


def _ses(y: np.ndarray, h: int, dow0: int = 0) -> np.ndarray:
    mejor, mejor_err = y[0], np.inf
    for a in (0.05, 0.1, 0.2, 0.3, 0.5):
        l, err = y[0], 0.0
        for t in range(1, len(y)):
            err += (y[t] - l) ** 2
            l = a * y[t] + (1 - a) * l
        if err < mejor_err:
            mejor, mejor_err = l, err
    return np.full(h, max(0.0, float(mejor)))


def _holt(y: np.ndarray, h: int, dow0: int = 0, phi: float = 0.9) -> np.ndarray:
    mejor, mejor_err = None, np.inf
    for a in (0.1, 0.2, 0.4):
        for b in (0.01, 0.05, 0.1):
            l, t_, err = y[0], (y[min(7, len(y) - 1)] - y[0]) / max(1, min(7, len(y) - 1)), 0.0
            for t in range(1, len(y)):
                pred = l + phi * t_
                err += (y[t] - pred) ** 2
                l_new = a * y[t] + (1 - a) * (l + phi * t_)
                t_ = b * (l_new - l) + (1 - b) * phi * t_
                l = l_new
            if err < mejor_err:
                mejor, mejor_err = (l, t_), err
    l, t_ = mejor
    out, acc = [], 0.0
    for k in range(1, h + 1):
        acc += phi ** k
        out.append(max(0.0, l + acc * t_))
    return np.array(out)


def _indice_semanal(y: np.ndarray, dow0: int) -> np.ndarray:
    """Índice multiplicativo por día de la semana (posición 0 = lunes), alineado con el calendario real."""
    if len(y) < 14 or y.mean() <= 0:
        return np.ones(7)
    dows = (dow0 + np.arange(len(y))) % 7
    idx = np.ones(7)
    for d in range(7):
        v = y[dows == d]
        if len(v):
            idx[d] = v.mean()
    idx = idx / idx.mean() if idx.mean() > 0 else np.ones(7)
    return np.clip(idx, 0.2, 3.0)


def _holt_winters_semanal(y: np.ndarray, h: int, dow0: int = 0) -> np.ndarray:
    """Desestacionaliza por día de semana (calendario real), aplica Holt amortiguado y reestacionaliza."""
    idx = _indice_semanal(y, dow0)
    n = len(y)
    dows = (dow0 + np.arange(n)) % 7
    y_des = y / idx[dows]
    base = _holt(y_des, h)
    dows_f = (dow0 + np.arange(n, n + h)) % 7
    return np.maximum(0.0, base * idx[dows_f])


def _estacional_ingenuo(y: np.ndarray, h: int, dow0: int = 0) -> np.ndarray:
    if len(y) < 14:
        return _media_movil(y, h)
    base = (y[-7:] + y[-14:-7]) / 2
    return np.maximum(0.0, np.array([base[k % 7] for k in range(h)]))


def _croston_sba(y: np.ndarray, h: int, dow0: int = 0, alpha: float = 0.15) -> np.ndarray:
    nz = np.flatnonzero(y > 0)
    if len(nz) == 0:
        return np.zeros(h)
    z = y[nz[0]]
    p = float(nz[0] + 1)
    q = 1.0
    for t in range(nz[0] + 1, len(y)):
        if y[t] > 0:
            z = alpha * y[t] + (1 - alpha) * z
            p = alpha * q + (1 - alpha) * p
            q = 1.0
        else:
            q += 1.0
    return np.full(h, max(0.0, (1 - alpha / 2) * z / p))


METODOS = {
    "media_movil_28": _media_movil, "suavizado_exp": _ses, "holt_amortiguado": _holt,
    "holt_winters_semanal": _holt_winters_semanal, "estacional_ingenuo": _estacional_ingenuo, "croston_sba": _croston_sba,
}


# ── evaluación ──────────────────────────────────────────────────────────────
def _wape(y: np.ndarray, f: np.ndarray) -> float:
    den = np.abs(y).sum()
    return float(np.abs(y - f).sum() / den) if den > 0 else (0.0 if np.abs(f).sum() == 0 else 1.0)


def _bloques(x: np.ndarray, n: int = 7) -> np.ndarray:
    k = len(x) // n * n
    return x[:k].reshape(-1, n).sum(axis=1) if k >= n else np.array([x.sum()])


def backtest(y: np.ndarray, cfg: ConfigPronostico, intermitente: bool, dow0: int) -> tuple[str, dict[str, float], np.ndarray, float]:
    """Origen móvil. Devuelve (mejor_metodo, {metodo: wape}, residuales diarios del mejor, sesgo del mejor)."""
    h, k = cfg.horizonte_backtest, cfg.folds_backtest
    candidatos = ["media_movil_28", "suavizado_exp", "holt_amortiguado", "holt_winters_semanal", "estacional_ingenuo"]
    if intermitente:
        candidatos = ["croston_sba", "media_movil_28", "suavizado_exp"]
    if len(y) < h * (k + 1) + 7:
        k = max(1, (len(y) - 14) // h)
    resultados: dict[str, float] = {}
    residuales: dict[str, list[float]] = {}
    sesgos: dict[str, float] = {}
    for nombre in candidatos:
        fn = METODOS[nombre]
        errs, res, reales = [], [], []
        try:
            for i in range(k, 0, -1):
                corte = len(y) - i * h
                if corte < 14:
                    continue
                f = fn(y[:corte], h, (dow0 + corte) % 7)
                real = y[corte:corte + h]
                f = f[:len(real)]
                errs.append(_wape(_bloques(real), _bloques(f)))
                res.extend((real - f).tolist())
                reales.extend(real.tolist())
            if errs:
                resultados[nombre] = float(np.mean(errs))
                residuales[nombre] = res
                tot = float(np.sum(reales))
                sesgos[nombre] = float(np.sum(res) / tot) if tot >= 1.0 else 0.0   # + = subestima, − = sobreestima
        except Exception:  # noqa: BLE001
            continue
    if not resultados:
        return "media_movil_28", {"media_movil_28": 1.0}, np.zeros(1), 0.0
    mejor = min(resultados, key=resultados.get)
    return mejor, resultados, np.array(residuales[mejor] or [0.0]), sesgos.get(mejor, 0.0)


# ── serie diaria con manejo de días sin captura ─────────────────────────────
def dias_sin_captura_por_ubicacion(d: pd.DataFrame, dim: str, umbral: float) -> dict[str, set]:
    """Días hábiles en que la UBICACIÓN no registró ninguna línea mientras la red sí operó. Si son pocos
    (≤ umbral de sus días hábiles) se consideran falta de captura y se imputan; si son muchos, la ubicación
    simplemente no opera esos días y los ceros son reales."""
    dias_red = set(d["fecha"].dt.date.unique())
    habiles_red = {x for x in dias_red if x.weekday() < 5}
    out: dict[str, set] = {}
    for loc, g in d.groupby(dim):
        dias_loc = set(g["fecha"].dt.date.unique())
        ini, fin = min(dias_loc), max(dias_loc)
        candidatos = {x for x in habiles_red if ini <= x <= fin}
        vacios = candidatos - dias_loc
        if candidatos and 0 < len(vacios) / len(candidatos) <= umbral:
            out[str(loc)] = vacios
    return out


def serie_diaria(g: pd.DataFrame, fin: pd.Timestamp, dias: int, sin_captura: set | None = None,
                 umbral_sin_captura: float = 0.25) -> tuple[pd.Series, int]:
    """Serie diaria producto × ubicación. Los días marcados como *sin captura* para la ubicación se imputan con la
    mediana del mismo día de la semana (en vez de contarlos como demanda cero). Devuelve (serie, días_imputados)."""
    s = g.set_index("fecha")["cantidad"].resample("D").sum()
    idx = pd.date_range(end=fin.normalize(), periods=dias, freq="D")
    s = s.reindex(idx, fill_value=0.0).astype(float)
    imputados = 0
    if sin_captura:
        vacios = np.array([d.date() in sin_captura for d in s.index])
        if vacios.any():
            vals = s.values.copy()
            dows = np.array([d.weekday() for d in s.index])
            for dw in range(7):
                m = (dows == dw) & ~vacios
                med = float(np.median(vals[m])) if m.any() else 0.0
                sel = (dows == dw) & vacios
                vals[sel] = med
                imputados += int(sel.sum())
            s = pd.Series(vals, index=s.index)
    return s, imputados


# ── proyección de saldo día a día ───────────────────────────────────────────
# Convención temporal (única en toda la plataforma):
#   • t = 0 es HOY al cierre; t = 1 es mañana; forecast[t-1] es la demanda que se consume durante el día t.
#   • saldo[t] es el saldo al CIERRE del día t.
#   • Una entrada prevista el día t se considera disponible desde el INICIO de ese día (sirve la demanda del día t).
#   • Un día ≤ 0 (documento vencido pero confirmado) se aplica hoy; un evento posterior a la ventana NO se cuenta.
#   • Primer quiebre = primer día cuyo saldo al cierre es negativo; la fecha NECESARIA de abastecimiento es el día
#     anterior al quiebre (margen de recepción y acomodo), nunca antes de mañana.
def proyectar_saldo(stock: float, forecast: np.ndarray, entradas: list[tuple[int, float]] | None = None,
                    salidas: list[tuple[int, float]] | None = None, stock_seguridad: float = 0.0) -> dict:
    """Saldo proyectado por día respetando CUÁNDO llegan las entradas y cuándo salen las transferencias (ver convención
    arriba). Devuelve saldo, mínimo, día del primer quiebre, día bajo stock de seguridad, cobertura en días (fraccional,
    hasta agotar), faltante para no bajar del stock de seguridad dentro de la ventana, saldo final y día necesario."""
    forecast = np.asarray(forecast, dtype=float)
    h = len(forecast)
    saldo = np.empty(h + 1)
    saldo[0] = float(stock)
    ent = np.zeros(h + 1)
    sal = np.zeros(h + 1)
    fuera = 0
    for x in (entradas or []):
        d = int(x[0])
        if d > h:
            fuera += 1
            continue
        ent[max(d, 0)] += float(x[1])
    for x in (salidas or []):
        d = int(x[0])
        if d > h:
            fuera += 1
            continue
        sal[max(d, 0)] += float(x[1])
    saldo[0] += ent[0] - sal[0]
    for t in range(1, h + 1):
        saldo[t] = saldo[t - 1] + ent[t] - sal[t] - forecast[t - 1]
    quiebre = next((t for t in range(h + 1) if saldo[t] < -1e-9), None)
    bajo_ss = next((t for t in range(h + 1) if saldo[t] < stock_seguridad - 1e-9), None)
    faltante = float(max(0.0, stock_seguridad - saldo.min()))
    if forecast.sum() <= 0:
        cobertura = None
    elif quiebre is None:
        cobertura = float(h)
    elif quiebre == 0:
        cobertura = 0.0
    else:
        dem_q = forecast[quiebre - 1]
        cobertura = float(quiebre - 1) + (float(saldo[quiebre - 1]) / dem_q if dem_q > 0 else 0.0)
        cobertura = max(0.0, round(cobertura, 2))
    return {"saldo": saldo, "minimo": float(saldo.min()), "dia_quiebre": quiebre, "dia_bajo_seguridad": bajo_ss,
            "cobertura_dias": cobertura, "faltante_para_seguridad": faltante, "saldo_final": float(saldo[-1]),
            "dia_necesario": (max(1, quiebre - 1) if quiebre is not None else None), "eventos_fuera_de_ventana": fuera}


def criticidad_local(proy: dict, sug: float, dem_dia: float, lt: float, ciclo: int, stock: float, exceso_cob: int) -> str:
    """Misma regla para la corrida base y para los escenarios (así un escenario sin cambios no 'empeora' nada)."""
    q, b = proy.get("dia_quiebre"), proy.get("dia_bajo_seguridad")
    if dem_dia <= 0:
        return "ok"
    if q is not None and q <= 1:
        return "desabasto"
    if q is not None and q <= lt:
        return "critico"
    if sug > 0 and ((q is not None and q <= lt + ciclo) or (b is not None and b <= lt + ciclo)):
        return "reordenar"
    if q is None and stock / dem_dia > exceso_cob:
        return "exceso"
    return "ok"


# ── motor principal ─────────────────────────────────────────────────────────
def pronosticar(consumo: pd.DataFrame, existencias: pd.DataFrame, cfg: ConfigPronostico | None = None,
                pendientes: pd.DataFrame | None = None, folios_prog: pd.DataFrame | None = None,
                uom_compra: dict[int, dict] | None = None, compromisos: dict | None = None) -> dict[str, Any]:
    """`compromisos` = {"origen": {(pid, almacen): cantidad}, "destino": {...}} ya comprometida por propuestas activas
    de los agentes (pendientes, aprobadas o creadas en Odoo aún sin confirmar), para no asignar dos veces lo mismo."""
    cfg = cfg or ConfigPronostico()
    f_proy_map: dict[tuple[int, str], np.ndarray] = {}
    ss_map: dict[tuple[int, str], float] = {}
    vacio = {"pronosticos": pd.DataFrame(), "resurtido": pd.DataFrame(), "abc_xyz": pd.DataFrame(),
             "caducidades": pd.DataFrame(), "rebalanceo": pd.DataFrame(), "resumen": {}, "historico": {},
             "retrasadas": pd.DataFrame(), "dimension": cfg.dimension}
    if consumo.empty:
        return vacio
    uom_compra = uom_compra or {}
    d = consumo.copy()
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")
    d = d[d["fecha"].notna()]
    dim = cfg.dimension if cfg.dimension in d.columns and (d[cfg.dimension].astype(str).str.strip() != "").mean() > 0.5 \
        else ("almacen" if (d["almacen"].astype(str).str.strip() != "").mean() > 0.5 else "hospital")
    d[dim] = d[dim].astype(str).str.strip()
    d = d[d[dim] != ""]
    hoy = (cfg.hoy or pd.Timestamp.now()).normalize()
    ultimo_dato = d["fecha"].max().normalize()
    desfase = int((hoy - ultimo_dato).days)
    fin_serie = min(hoy, ultimo_dato)
    hist = max(min(cfg.historia_dias, (fin_serie - d["fecha"].min().normalize()).days + 1), cfg.min_dias_historia)
    h = cfg.horizonte_dias
    fechas_f = pd.date_range(start=hoy + pd.Timedelta(days=1), periods=h, freq="D")
    sin_captura = dias_sin_captura_por_ubicacion(d, dim, cfg.umbral_sin_captura)
    hosp_por_ubic = d.groupby(dim)["hospital"].agg(lambda x: x.mode().iloc[0] if len(x.mode()) else "").to_dict()

    # ── existencias por producto × ubicación (utilizable y total) ──
    stock_map: dict[tuple[int, str], dict] = {}
    if existencias is not None and not existencias.empty:
        ex = existencias.copy()
        col_dim = "ubicacion" if dim == "subalmacen" and "ubicacion" in ex.columns else "almacen"
        ex[col_dim] = ex[col_dim].astype(str).str.strip()
        for c in ("utilizable", "disponible", "reservado"):
            if c not in ex.columns:
                ex[c] = ex["cantidad"] if c != "reservado" else 0.0
        g_ex = ex.groupby(["producto_id", col_dim]).agg(cantidad=("cantidad", "sum"), utilizable=("utilizable", "sum"),
                                                       reservado=("reservado", "sum"))
        stock_map = {(int(k[0]), str(k[1])): {"total": float(v["cantidad"]), "utilizable": float(v["utilizable"]),
                                              "reservado": float(v["reservado"])} for k, v in g_ex.iterrows()}
    stock_total: dict[int, dict] = {}
    for (pid, _), v in stock_map.items():
        t = stock_total.setdefault(pid, {"total": 0.0, "utilizable": 0.0, "reservado": 0.0})
        for k2 in t:
            t[k2] += v[k2]

    # ── abastecimiento pendiente (entradas/salidas dentro del horizonte) ──
    entradas: dict[tuple[int, str], float] = {}
    salidas: dict[tuple[int, str], float] = {}
    entradas_red: dict[int, float] = {}
    entradas_det: dict[tuple[int, str], list] = {}      # (pid, loc) → [(día_rel, cantidad, ref, tipo, proveedor)]
    salidas_det: dict[tuple[int, str], list] = {}
    entradas_red_det: dict[int, list] = {}
    retrasadas = pd.DataFrame()
    if pendientes is not None and not pendientes.empty:
        pen = pendientes.copy()
        pen["fecha_prevista"] = pd.to_datetime(pen["fecha_prevista"], errors="coerce")
        en_horizonte = pen[pen["confirmada"] & ~pen["retrasada"]
                           & (pen["fecha_prevista"].isna() | (pen["fecha_prevista"] <= fechas_f[-1]))]
        for _, r_ in en_horizonte.iterrows():
            pid = int(r_["producto_id"]) if pd.notna(r_["producto_id"]) else None
            if not pid:
                continue
            dia_rel = int((r_["fecha_prevista"].normalize() - hoy).days) if pd.notna(r_["fecha_prevista"]) else 1
            det = (max(dia_rel, 0), float(r_["cantidad"]), str(r_["ref"]), str(r_["tipo"]), str(r_.get("proveedor") or ""))
            k_in = (pid, str(r_["ubicacion_destino"]))
            entradas[k_in] = entradas.get(k_in, 0.0) + float(r_["cantidad"])
            entradas_det.setdefault(k_in, []).append(det)
            if r_["tipo"] == "compra":
                entradas_red[pid] = entradas_red.get(pid, 0.0) + float(r_["cantidad"])
                entradas_red_det.setdefault(pid, []).append(det)
            if r_["tipo"] == "transferencia" and r_.get("ubicacion_origen"):
                k_out = (pid, str(r_["ubicacion_origen"]))
                salidas[k_out] = salidas.get(k_out, 0.0) + float(r_["cantidad"])
                salidas_det.setdefault(k_out, []).append(det)
        retrasadas = pen[pen["retrasada"]].copy()

    # ── agenda: folios programados por hospital y DÍA REAL (índice 0 = mañana) ──
    # Una sola lógica para combinar pronóstico histórico y agenda: la agenda es un PISO diario del pronóstico base
    # en la fecha real de cada procedimiento; el "ajuste atribuible a la agenda" es el exceso sobre el pronóstico base
    # en esa fecha. Nunca se suma ni se multiplica dos veces la misma necesidad; los escenarios hipotéticos se
    # aplican aparte (planificador) sobre esta demanda proyectada.
    agenda_hosp: dict[str, np.ndarray] = {}
    if folios_prog is not None and not folios_prog.empty:
        fp = folios_prog.copy()
        fp["dia"] = pd.to_datetime(fp["dia"], errors="coerce")
        fp = fp[fp["dia"].notna() & (fp["dia"] > hoy) & (fp["dia"] <= fechas_f[-1])]
        for hsp, g_ in fp.groupby("hospital"):
            v = np.zeros(h)
            for _, r_ in g_.iterrows():
                t = int((r_["dia"].normalize() - hoy).days) - 1
                if 0 <= t < h:
                    v[t] += float(r_["folios"])
            if v.sum() > 0:
                agenda_hosp[str(hsp)] = v
    r90 = d[d["fecha"] > fin_serie - pd.Timedelta(days=90)]
    folios_h = r90.groupby("hospital")["folio"].nunique() if "folio" in r90.columns else pd.Series(dtype=float)
    cant_ph = r90.groupby(["hospital", "producto_id"])["cantidad"].sum()
    share = r90.groupby(["hospital", "producto_id", dim])["cantidad"].sum()
    perfil_agenda: dict[tuple[int, str], float] = {}       # (pid, loc) → consumo por folio programado del hospital
    for (hsp, pid, loc), q in share.items():
        if str(hsp) not in agenda_hosp or folios_h.get(hsp, 0) == 0:
            continue
        por_folio = float(cant_ph.get((hsp, pid), 0.0)) / float(folios_h[hsp])
        part = float(q) / float(cant_ph.get((hsp, pid), 1.0)) if cant_ph.get((hsp, pid), 0) else 0.0
        perfil_agenda[(int(pid), str(loc))] = por_folio * part
    comprometida: dict[tuple[int, str], float] = {}
    ajuste_agenda: dict[tuple[int, str], float] = {}

    costo_unit = (d.groupby("producto_id").apply(lambda g: g["importe"].sum() / g["cantidad"].sum()
                                                 if g["cantidad"].sum() else 0.0, include_groups=False)).to_dict()
    nombres = d.drop_duplicates("producto_id").set_index("producto_id")["producto"].to_dict()
    unidades = d.drop_duplicates("producto_id").set_index("producto_id")["unidad"].to_dict()

    filas_p, filas_r = [], []
    grupos = list(d.groupby(["producto_id", dim]))
    # combinaciones con backtesting completo: las más activas (líneas registradas); las demás, validación rápida
    orden = sorted(range(len(grupos)), key=lambda i: -len(grupos[i][1]))
    con_backtest = set(orden[:max(1, int(cfg.max_backtests))])
    for idx, ((pid, dm), g) in enumerate(grupos):
        pid = int(pid)
        completo = idx in con_backtest
        serie, imputados = serie_diaria(g, fin_serie, hist, sin_captura.get(dm), cfg.umbral_sin_captura)
        y = serie.to_numpy()
        dias_con_demanda = int((y > 0).sum())
        primero = int(np.argmax(y > 0)) if (y > 0).any() else 0
        if len(y) - primero >= cfg.min_dias_historia:
            y = y[primero:]
            dow0 = int(serie.index[primero].weekday())
        else:
            dow0 = int(serie.index[0].weekday())
        insuficiente = len(y) < cfg.min_dias_historia or dias_con_demanda < cfg.min_dias_con_demanda or y.sum() <= 0
        frac_cero = float((y == 0).mean()) if len(y) else 1.0
        intermitente = frac_cero > 0.6
        if insuficiente:
            metodo, wape, sesgo = "insuficiente", None, None
            dem = float(y.mean()) if len(y) else 0.0
            f = np.full(h, dem)
            sigma = max(float(np.std(y)) if len(y) > 1 else dem, 0.5 * dem, 1e-9)
        elif not completo:
            # método rápido con una sola validación (última ventana): suficiente para combinaciones de baja actividad
            metodo = "croston_sba" if intermitente else "media_movil_28"
            corte = max(14, len(y) - cfg.horizonte_backtest)
            f_val = METODOS[metodo](y[:corte], cfg.horizonte_backtest, (dow0 + corte) % 7)[:len(y) - corte]
            real = y[corte:]
            res = real - f_val if len(real) else np.zeros(1)
            scores = {metodo: _wape(_bloques(real), _bloques(f_val)) if len(real) else 1.0}
            tot = float(np.sum(real)) if len(real) else 0.0
            sesgo = float(np.sum(res) / tot) if tot >= 1.0 else 0.0
            f = METODOS[metodo](y, h, (dow0 + len(y)) % 7)
            wape = scores.get(metodo, 1.0)
            sigma = float(np.std(res)) if len(res) > 3 else float(np.std(y[-56:]))
            sigma = max(sigma, 0.05 * max(f.mean(), 1e-9))
        else:
            metodo, scores, res, sesgo = backtest(y, cfg, intermitente, dow0)
            f = METODOS[metodo](y, h, (dow0 + len(y)) % 7)
            wape = scores.get(metodo, 1.0)
            sigma = float(np.std(res)) if len(res) > 3 else float(np.std(y[-56:]))
            sigma = max(sigma, 0.05 * max(f.mean(), 1e-9))
        lo = np.maximum(0.0, f - cfg.z * sigma)
        hi = f + cfg.z * sigma
        hsp = hosp_por_ubic.get(dm, "")
        agenda_vec = perfil_agenda.get((pid, dm), 0.0) * agenda_hosp.get(str(hsp), np.zeros(h))
        comp = float(agenda_vec.sum())
        f_proy = np.maximum(f, agenda_vec)                      # piso diario en la fecha real de cada folio
        ajuste = float(np.maximum(0.0, agenda_vec - f).sum())   # exceso atribuible a la agenda
        if comp > 0:
            comprometida[(pid, dm)] = comp
            ajuste_agenda[(pid, dm)] = ajuste
        for fe, fv, l, u, dp, ag in zip(fechas_f, f, lo, hi, f_proy, agenda_vec):
            filas_p.append({"producto_id": pid, "producto": g["producto"].iloc[0], "almacen": dm, "metodo": metodo,
                            "mape": None if wape is None else round(100 * wape, 1), "fecha": fe.date().isoformat(),
                            "pronostico": round(float(fv), 3), "inferior": round(float(l), 3), "superior": round(float(u), 3),
                            "demanda_proyectada": round(float(dp), 3), "demanda_agenda": round(float(ag), 3)})

        # ── inventario proyectado y resurtido ──
        dem_dia = float(f.mean())
        dem_h = float(f.sum())
        dem_h_ajust = float(f_proy.sum())
        es_cedis = dm in cfg.es_cedis or "cedis" in dm.lower()
        lt = cfg.lead_times.get(pid, cfg.lead_time_default) if es_cedis else cfg.lead_time_interno
        sigma_lt = cfg.sigma_lt_pct * lt if es_cedis else 0.0
        ss = cfg.z * math.sqrt(max(lt, 1) * sigma ** 2 + (dem_dia ** 2) * (sigma_lt ** 2))
        rop = dem_dia * lt + ss
        st = stock_map.get((pid, dm))
        stock = (st["utilizable"] if cfg.usar_utilizable else st["total"]) if st else np.nan
        en_camino = entradas.get((pid, dm), 0.0)
        por_salir = salidas.get((pid, dm), 0.0)
        f_proy_map[(pid, dm)], ss_map[(pid, dm)] = f_proy, ss
        proy = proyectar_saldo(0.0 if np.isnan(stock) else stock, f_proy, entradas_det.get((pid, dm)), salidas_det.get((pid, dm)), ss)
        proyectado = proy["saldo_final"] + float(f_proy.sum())   # existencia + entradas − salidas (sin demanda), para mostrar
        cob = proy["cobertura_dias"] if dem_dia > 0 else (np.inf if not np.isnan(stock) else np.nan)
        if cob is None:
            cob = np.inf
        ratio_uom = float(uom_compra.get(pid, {}).get("ratio") or 0.0)
        multiplo = cfg.multiplos.get(pid) or (ratio_uom if (es_cedis and ratio_uom > 1) else cfg.multiplo_default) or 1.0
        # sugerido = lo necesario para que el saldo nunca baje del stock de seguridad dentro de la ventana relevante
        ventana = h if es_cedis else int(min(h, cfg.ciclo_revision_dias + lt))
        proy_v = proyectar_saldo(0.0 if np.isnan(stock) else stock, f_proy[:ventana], entradas_det.get((pid, dm)), salidas_det.get((pid, dm)), ss)
        sug = proy_v["faltante_para_seguridad"]
        sug = math.ceil(sug / multiplo - 1e-9) * multiplo if sug > 0 else 0.0
        sug = redondear(sug, str(g["unidad"].iloc[0]), cfg.precision, arriba=True) if sug > 0 else 0.0
        dias_ult = int((fin_serie - g["fecha"].max().normalize()).days)
        fecha_quiebre = (hoy + pd.Timedelta(days=proy["dia_quiebre"])).date().isoformat() if proy["dia_quiebre"] is not None else None
        fecha_necesaria = (hoy + pd.Timedelta(days=proy["dia_necesario"])).date().isoformat() if proy["dia_necesario"] is not None else None
        if np.isnan(stock):
            crit = "sin_existencias_registradas"
        elif dem_dia > 0 and dias_ult > cfg.dias_sin_movimiento and stock > 0:
            crit = "sin_movimiento"
        else:
            crit = criticidad_local(proy, sug, dem_dia, lt, cfg.ciclo_revision_dias, stock, cfg.exceso_cobertura_dias)
        confianza = "baja" if insuficiente or (wape is not None and wape > 0.5) else ("media" if wape > 0.25 else "alta")
        cu = float(costo_unit.get(pid, 0.0))
        filas_r.append({
            "producto_id": pid, "producto": g["producto"].iloc[0], "almacen": dm, "hospital": hsp, "unidad": g["unidad"].iloc[0],
            "stock_actual": None if np.isnan(stock) else round(stock, 2),
            "stock_total": None if not st else round(st["total"], 2), "reservado": None if not st else round(st["reservado"], 2),
            "en_camino": round(en_camino, 2), "por_salir": round(por_salir, 2), "stock_proyectado": round(proyectado, 2),
            "saldo_minimo": round(proy["minimo"], 2), "fecha_quiebre": fecha_quiebre, "fecha_necesaria": fecha_necesaria,
            "dia_quiebre": proy["dia_quiebre"], "dia_necesario": proy["dia_necesario"], "saldo_final_horizonte": round(proy["saldo_final"], 2),
            "primera_entrada_dia": (min(x[0] for x in entradas_det.get((pid, dm), [])) if entradas_det.get((pid, dm)) else None),
            "demanda_diaria": round(dem_dia, 3), "sigma_diaria": round(sigma, 3), "lead_time_dias": lt,
            "stock_seguridad": round(ss, 2), "punto_reorden": round(rop, 2), "demanda_horizonte": round(dem_h_ajust, 2),
            "demanda_comprometida": round(comp, 2), "ajuste_agenda": round(ajuste, 2),
            "dias_con_agenda": int((agenda_vec > 0).sum()), "sugerido": round(sug, 2),
            "sugerido_compra": (round(math.ceil(sug / ratio_uom), 0) if (ratio_uom > 1 and sug > 0) else None),
            "unidad_compra": uom_compra.get(pid, {}).get("unidad_compra"),
            "dias_cobertura": None if (isinstance(cob, float) and (np.isnan(cob) or np.isinf(cob))) else round(float(cob), 1),
            "criticidad": crit, "metodo": metodo, "mape": None if wape is None else round(100 * wape, 1),
            "sesgo_pct": None if sesgo is None else round(100 * sesgo, 1), "confianza": confianza, "nivel": "local",
            "intermitente": intermitente, "frac_cero": round(frac_cero, 2), "es_cedis": es_cedis, "costo_unit": round(cu, 4),
            "importe_sugerido": round(sug * cu, 2), "valor_inventario": None if np.isnan(stock) else round(stock * cu, 2),
            "dias_desde_ultimo_consumo": dias_ult, "dias_imputados": imputados, "dias_historia": int(len(y)),
            "ultimos_28d": round(float(y[-28:].sum()), 2), "prev_28d": round(float(y[-56:-28].sum()), 2) if len(y) >= 56 else None,
        })

    # ── ubicaciones con existencia pero sin consumo: CEDIS (fuente) o stock muerto ──
    cubiertas = {(int(r_["producto_id"]), r_["almacen"]) for r_ in filas_r}
    for (pid, loc), st in stock_map.items():
        stock = st["utilizable"] if cfg.usar_utilizable else st["total"]
        if (pid, loc) in cubiertas or stock <= 0:
            continue
        es_cedis = loc in cfg.es_cedis or "cedis" in loc.lower()
        cu = float(costo_unit.get(pid, 0.0))
        en_camino = entradas.get((pid, loc), 0.0)
        por_salir = salidas.get((pid, loc), 0.0)
        filas_r.append({
            "producto_id": pid, "producto": nombres.get(pid, str(pid)), "almacen": loc, "hospital": hosp_por_ubic.get(loc, ""),
            "unidad": unidades.get(pid, ""), "stock_actual": round(stock, 2), "stock_total": round(st["total"], 2),
            "reservado": round(st["reservado"], 2), "en_camino": round(en_camino, 2), "por_salir": round(por_salir, 2),
            "stock_proyectado": round(stock + en_camino - por_salir, 2), "saldo_minimo": round(stock + en_camino - por_salir, 2),
            "fecha_quiebre": None, "fecha_necesaria": None, "dia_quiebre": None, "dia_necesario": None,
            "saldo_final_horizonte": round(stock + en_camino - por_salir, 2),
            "primera_entrada_dia": None, "demanda_diaria": 0.0, "sigma_diaria": 0.0,
            "lead_time_dias": cfg.lead_times.get(pid, cfg.lead_time_default) if es_cedis else cfg.lead_time_interno,
            "stock_seguridad": 0.0, "punto_reorden": 0.0, "demanda_horizonte": 0.0, "demanda_comprometida": 0.0,
            "ajuste_agenda": 0.0, "dias_con_agenda": 0, "sugerido": 0.0,
            "sugerido_compra": None, "unidad_compra": uom_compra.get(pid, {}).get("unidad_compra"), "dias_cobertura": None,
            "criticidad": "fuente" if es_cedis else "sin_movimiento", "metodo": "sin consumo local", "mape": None, "sesgo_pct": None,
            "confianza": "n/a", "nivel": "local", "intermitente": False, "frac_cero": 1.0, "es_cedis": es_cedis,
            "costo_unit": round(cu, 4), "importe_sugerido": 0.0, "valor_inventario": round(stock * cu, 2),
            "dias_desde_ultimo_consumo": 9999, "dias_imputados": 0, "dias_historia": 0, "ultimos_28d": 0.0, "prev_28d": None,
        })

    # ── nivel RED por producto: demanda de toda la red vs. existencia utilizable total + compras en camino ──
    red_rows, red_pron = [], []
    if filas_r:
        tmp = pd.DataFrame(filas_r)
        pron_tmp = pd.DataFrame(filas_p)
        for pid, g in tmp.groupby("producto_id"):
            pid = int(pid)
            locales = g[g["metodo"] != "sin consumo local"]
            dem_dia = float(locales["demanda_diaria"].sum())
            sigma = float(np.sqrt((locales["sigma_diaria"] ** 2).sum()))
            lt = cfg.lead_times.get(pid, cfg.lead_time_default)
            sigma_lt = cfg.sigma_lt_pct * lt
            ss = cfg.z * math.sqrt(max(lt, 1) * sigma ** 2 + (dem_dia ** 2) * (sigma_lt ** 2))
            stt = stock_total.get(pid, {"total": 0.0, "utilizable": 0.0, "reservado": 0.0})
            stock = stt["utilizable"] if cfg.usar_utilizable else stt["total"]
            en_camino = entradas_red.get(pid, 0.0)
            proyectado = stock + en_camino
            dem_h = float(locales["demanda_horizonte"].sum()) if len(locales) else dem_dia * h
            comp = float(locales["demanda_comprometida"].sum()) if len(locales) else 0.0
            rop = dem_dia * lt + ss
            f_red = pron_tmp[(pron_tmp["producto_id"] == pid) & (pron_tmp["almacen"] != "RED (todas las ubicaciones)")].groupby("fecha")["demanda_proyectada"].sum()
            f_red = f_red.reindex([fe.date().isoformat() for fe in fechas_f], fill_value=0.0).to_numpy() if len(f_red) else np.full(h, dem_dia)
            proy = proyectar_saldo(stock, f_red, entradas_red_det.get(pid), None, ss)
            cob = proy["cobertura_dias"] if dem_dia > 0 else np.inf
            if cob is None:
                cob = np.inf
            ratio_uom = float(uom_compra.get(pid, {}).get("ratio") or 0.0)
            multiplo = cfg.multiplos.get(pid) or (ratio_uom if ratio_uom > 1 else cfg.multiplo_default) or 1.0
            sug = proy["faltante_para_seguridad"]
            sug = math.ceil(sug / multiplo - 1e-9) * multiplo if sug > 0 else 0.0
            sug = redondear(sug, str(g["unidad"].iloc[0]), cfg.precision, arriba=True) if sug > 0 else 0.0
            # ¿la compra llega a tiempo? (lead time del proveedor vs. día del quiebre; holgura = margen en días)
            llega_a_tiempo = None if proy["dia_quiebre"] is None else bool(math.ceil(lt) <= proy["dia_quiebre"])
            holgura = None if proy["dia_quiebre"] is None else int(proy["dia_quiebre"] - math.ceil(lt))
            fecha_quiebre = (hoy + pd.Timedelta(days=proy["dia_quiebre"])).date().isoformat() if proy["dia_quiebre"] is not None else None
            fecha_necesaria = (hoy + pd.Timedelta(days=proy["dia_necesario"])).date().isoformat() if proy["dia_necesario"] is not None else None
            if dem_dia > 0 and proy["dia_quiebre"] is not None and proy["dia_quiebre"] <= 1:
                crit = "desabasto"
            elif dem_dia > 0 and proy["dia_quiebre"] is not None and proy["dia_quiebre"] <= lt:
                crit = "critico"
            elif dem_dia > 0 and proy["dia_bajo_seguridad"] is not None and proy["dia_bajo_seguridad"] <= lt + cfg.ciclo_revision_dias:
                crit = "reordenar"
            elif dem_dia > 0 and proy["dia_quiebre"] is None and (stock / dem_dia) > cfg.exceso_cobertura_dias:
                crit = "exceso"
            else:
                crit = "ok"
            cu = float(costo_unit.get(pid, 0.0))
            wapes = locales["mape"].dropna()
            pesos = locales.loc[wapes.index, "ultimos_28d"] if len(wapes) else pd.Series(dtype=float)
            wape_pond = float(np.average(wapes, weights=(pesos + 1e-9))) if len(wapes) else None
            red_rows.append({
                "producto_id": pid, "producto": g["producto"].iloc[0], "almacen": "RED (todas las ubicaciones)", "hospital": "",
                "unidad": g["unidad"].iloc[0], "stock_actual": round(stock, 2), "stock_total": round(stt["total"], 2),
                "reservado": round(stt["reservado"], 2), "en_camino": round(en_camino, 2), "por_salir": 0.0,
                "stock_proyectado": round(proyectado, 2), "saldo_minimo": round(proy["minimo"], 2), "fecha_quiebre": fecha_quiebre,
                "fecha_necesaria": fecha_necesaria, "dia_quiebre": proy["dia_quiebre"], "dia_necesario": proy["dia_necesario"],
                "saldo_final_horizonte": round(proy["saldo_final"], 2),
                "primera_entrada_dia": (min(x[0] for x in entradas_red_det.get(pid, [])) if entradas_red_det.get(pid) else None),
                "demanda_diaria": round(dem_dia, 3), "sigma_diaria": round(sigma, 3),
                "lead_time_dias": lt, "stock_seguridad": round(ss, 2), "punto_reorden": round(rop, 2),
                "demanda_horizonte": round(dem_h, 2), "demanda_comprometida": round(comp, 2),
                "ajuste_agenda": round(float(locales["ajuste_agenda"].sum()) if len(locales) else 0.0, 2),
                "dias_con_agenda": int(locales["dias_con_agenda"].max()) if len(locales) else 0, "sugerido": round(sug, 2),
                "sugerido_compra": (float(math.ceil(sug / ratio_uom - 1e-9)) if (ratio_uom > 1 and sug > 0) else None),
                "unidad_compra": uom_compra.get(pid, {}).get("unidad_compra"),
                "conversion_faltante": bool(uom_compra.get(pid, {}).get("unidad_compra") and not ratio_uom),
                "llega_a_tiempo": llega_a_tiempo, "holgura_dias": holgura, "fecha_llegada_estimada": (hoy + pd.Timedelta(days=int(math.ceil(lt)))).date().isoformat(),
                "dias_cobertura": None if np.isinf(cob) else round(float(cob), 1), "criticidad": crit,
                "metodo": "suma de ubicaciones", "mape": None if wape_pond is None else round(wape_pond, 1),
                "sesgo_pct": None, "confianza": "baja" if (wape_pond or 0) > 50 else ("media" if (wape_pond or 0) > 25 else "alta"),
                "nivel": "red", "intermitente": False, "frac_cero": 0.0, "es_cedis": True, "costo_unit": round(cu, 4),
                "importe_sugerido": round(sug * cu, 2), "valor_inventario": round(stock * cu, 2),
                "dias_desde_ultimo_consumo": int(g["dias_desde_ultimo_consumo"].min()), "dias_imputados": int(locales["dias_imputados"].sum()) if len(locales) else 0,
                "dias_historia": int(locales["dias_historia"].max()) if len(locales) else 0,
                "ultimos_28d": round(float(g["ultimos_28d"].sum()), 2),
                "prev_28d": round(float(g["prev_28d"].fillna(0).sum()), 2) if g["prev_28d"].notna().any() else None,
            })
            gp = pron_tmp[pron_tmp["producto_id"] == pid].groupby("fecha").agg(
                pronostico=("pronostico", "sum"), inferior=("inferior", "sum"), superior=("superior", "sum"),
                demanda_proyectada=("demanda_proyectada", "sum"), demanda_agenda=("demanda_agenda", "sum"))
            for fe, r_ in gp.iterrows():
                red_pron.append({"producto_id": pid, "producto": g["producto"].iloc[0], "almacen": "RED (todas las ubicaciones)",
                                 "metodo": "suma de ubicaciones", "mape": None if wape_pond is None else round(wape_pond, 1), "fecha": fe,
                                 "pronostico": round(float(r_["pronostico"]), 3), "inferior": round(float(r_["inferior"]), 3),
                                 "superior": round(float(r_["superior"]), 3), "demanda_proyectada": round(float(r_["demanda_proyectada"]), 3),
                                 "demanda_agenda": round(float(r_["demanda_agenda"]), 3)})
    filas_r.extend(red_rows)
    filas_p.extend(red_pron)

    pron = pd.DataFrame(filas_p)
    resur = pd.DataFrame(filas_r)
    orden = {"desabasto": 0, "critico": 1, "reordenar": 2, "sin_movimiento": 3, "exceso": 4, "ok": 5,
             "sin_existencias_registradas": 6, "fuente": 7}
    if not resur.empty:
        resur["orden"] = resur["criticidad"].map(orden)
        resur = resur.sort_values(["orden", "dias_cobertura"], na_position="last").drop(columns="orden").reset_index(drop=True)

    abc = clasificar_abc_xyz(d, fin_serie)
    local = resur[resur["nivel"] == "local"] if not resur.empty else resur
    cadu = proyectar_caducidades(existencias, local, dim, hoy)

    def _simular(pid_: int, loc_: str, extra_in: list, extra_out: list) -> dict:
        """Misma proyección día por día del plan, con entradas/salidas adicionales (para el 'después' de una propuesta)."""
        k_ = (int(pid_), str(loc_))
        f_ = f_proy_map.get(k_)
        st_ = stock_map.get(k_)
        stock_ = (st_["utilizable"] if cfg.usar_utilizable else st_["total"]) if st_ else 0.0
        if f_ is None:
            f_ = np.zeros(h)
        return proyectar_saldo(stock_, f_, list(entradas_det.get(k_, [])) + list(extra_in), list(salidas_det.get(k_, [])) + list(extra_out),
                               float(ss_map.get(k_, 0.0)))

    rebal = proponer_rebalanceo(local, cfg.regiones, compromisos=compromisos, cfg=cfg, simular=_simular, hoy=hoy)
    resumen = _resumen(resur, cadu, rebal, abc, dim, h, retrasadas, desfase, comprometida, folios_prog,
                       resur[resur["nivel"] == "local"].groupby("unidad")["demanda_comprometida"].sum().to_dict() if not resur.empty else {})
    resumen["ajuste_agenda_total"] = round(float(sum(ajuste_agenda.values())), 2)
    resumen["ajuste_agenda_por_unidad"] = ({str(k): round(float(v), 1) for k, v in
                                            resur[resur["nivel"] == "local"].groupby("unidad")["ajuste_agenda"].sum().items()} if not resur.empty else {})
    resumen["agenda_por_hospital"] = {k: {"folios": int(v.sum()), "dias": int((v > 0).sum()),
                                          "primer_dia": (hoy + pd.Timedelta(days=int(np.argmax(v > 0)) + 1)).date().isoformat()}
                                      for k, v in agenda_hosp.items()}
    historico = _historico_demanda(d, fin_serie)
    return {"pronosticos": pron, "resurtido": resur, "abc_xyz": abc, "caducidades": cadu, "rebalanceo": rebal,
            "resumen": resumen, "historico": historico, "dimension": dim, "retrasadas": retrasadas,
            "entradas_det": {f"{k[0]}|{k[1]}": v for k, v in entradas_det.items()},
            "salidas_det": {f"{k[0]}|{k[1]}": v for k, v in salidas_det.items()},
            "stock_seguridad_local": {f"{r_['producto_id']}|{r_['almacen']}": r_["stock_seguridad"] for r_ in filas_r if r_["nivel"] == "local"},
            "agenda_hospital": {k: v.tolist() for k, v in agenda_hosp.items()},
            "hoy": hoy.date().isoformat()}


# ── ABC / XYZ ───────────────────────────────────────────────────────────────
def clasificar_abc_xyz(d: pd.DataFrame, hoy: pd.Timestamp) -> pd.DataFrame:
    ult = d[d["fecha"] > hoy - pd.Timedelta(days=365)]
    if ult.empty:
        return pd.DataFrame()
    imp = ult.groupby(["producto_id", "producto"])["importe"].sum().sort_values(ascending=False).reset_index()
    imp["pct"] = imp["importe"] / imp["importe"].sum() if imp["importe"].sum() else 0
    imp["acum"] = imp["pct"].cumsum()
    imp["abc"] = np.where(imp["acum"] <= 0.80, "A", np.where(imp["acum"] <= 0.95, "B", "C"))
    sem = ult.set_index("fecha").groupby("producto_id")["cantidad"].resample("W").sum().reset_index()
    cv = sem.groupby("producto_id")["cantidad"].agg(lambda s: s.std() / s.mean() if s.mean() else np.inf)
    imp["cv_semanal"] = imp["producto_id"].map(cv).round(2)
    imp["xyz"] = np.where(imp["cv_semanal"] < 0.5, "X", np.where(imp["cv_semanal"] < 1.0, "Y", "Z"))
    imp["clase"] = imp["abc"] + imp["xyz"]
    imp["politica"] = imp["clase"].map({
        "AX": "Reposición automática, stock de seguridad bajo, revisión semanal",
        "AY": "Pronóstico estadístico + revisión semanal de compras", "AZ": "Revisión manual frecuente; comprar por pedido",
        "BX": "Regla min/max estable", "BY": "Regla min/max con revisión mensual", "BZ": "Stock mínimo + compra por pedido",
        "CX": "Min/max amplio, revisión trimestral", "CY": "Min/max, revisión trimestral", "CZ": "Comprar sólo por pedido"})
    return imp.round({"importe": 2, "pct": 4, "acum": 4})


# ── caducidades (FEFO) ──────────────────────────────────────────────────────
def proyectar_caducidades(existencias: pd.DataFrame | None, resur: pd.DataFrame, dim: str,
                          hoy: pd.Timestamp, horizonte_dias: int = 180) -> pd.DataFrame:
    if existencias is None or existencias.empty or resur.empty or "caducidad" not in existencias.columns:
        return pd.DataFrame()
    ex = existencias.copy()
    ex["caducidad"] = pd.to_datetime(ex["caducidad"].map(lambda v: None if v is False else v), errors="coerce")
    ex = ex[ex["caducidad"].notna() & (ex["cantidad"] > 0)]
    if ex.empty:
        return pd.DataFrame()
    col_dim = "ubicacion" if dim == "subalmacen" and "ubicacion" in ex.columns else "almacen"
    dem = resur.set_index(["producto_id", "almacen"])["demanda_diaria"].to_dict()
    costo = resur.drop_duplicates("producto_id").set_index("producto_id")["costo_unit"].to_dict()
    filas = []
    for (pid, loc), g in ex.groupby(["producto_id", col_dim]):
        pid = int(pid)
        dd = float(dem.get((pid, str(loc)), 0.0))
        acumulado = 0.0
        for _, lote in g.sort_values("caducidad").iterrows():
            dias_para_caducar = (lote["caducidad"] - hoy).days
            consumible = max(0.0, dd * max(dias_para_caducar, 0) - acumulado)
            en_riesgo = max(0.0, float(lote["cantidad"]) - consumible)
            acumulado += float(lote["cantidad"])
            if dias_para_caducar <= horizonte_dias and (en_riesgo > 0 or dias_para_caducar < 0):
                filas.append({"producto_id": pid, "producto": lote.get("producto", ""), "almacen": str(loc),
                              "lote": lote.get("lote", ""), "caducidad": lote["caducidad"].date().isoformat(),
                              "dias_para_caducar": int(dias_para_caducar), "cantidad": round(float(lote["cantidad"]), 2),
                              "demanda_diaria": round(dd, 3), "en_riesgo": round(en_riesgo, 2),
                              "importe_en_riesgo": round(en_riesgo * float(costo.get(pid, 0.0)), 2),
                              "estado": "caducado" if dias_para_caducar < 0 else ("critico" if dias_para_caducar <= 30 else "riesgo")})
    out = pd.DataFrame(filas)
    return out.sort_values(["dias_para_caducar", "importe_en_riesgo"], ascending=[True, False]).reset_index(drop=True) if not out.empty else out


# ── rebalanceo entre almacenes ──────────────────────────────────────────────
def _region(nombre: str, regiones: dict[str, str]) -> str:
    if nombre in regiones:
        return regiones[nombre]
    n = nombre.upper()
    for k, v in regiones.items():
        if k.upper() in n:
            return v
    tok = n.replace("CEDIS-", "").replace("CEDIS ", "").replace("CEDIS", "").split("/")[0].strip()
    m = re.match(r"[A-ZÁÉÍÓÚÑ]+", tok)
    return m.group(0) if m else tok[:3]


def reserva_operativa(local: pd.DataFrame, producto_id: int, almacen: str, cfg: ConfigPronostico, regiones: dict | None = None) -> float:
    """Lo que un origen debe CONSERVAR y no ceder a otras ubicaciones:
      • con consumo propio: punto de reorden × 1.5 + su demanda del horizonte;
      • si es CEDIS: además la demanda diaria de los hospitales de su región × `reserva_cedis_dias` (reserva operativa
        para las demás unidades que atiende). "Sin consumo propio" nunca significa "todo está libre"."""
    regiones = regiones if regiones is not None else cfg.regiones
    g = local[local["producto_id"] == int(producto_id)]
    f = g[g["almacen"] == str(almacen)]
    if f.empty:
        return 0.0
    f = f.iloc[0]
    dem_propia = float(f.get("demanda_diaria", 0) or 0)
    propio = float(f.get("punto_reorden", 0) or 0) * 1.5 + float(f.get("demanda_horizonte", 0) or 0) if dem_propia > 0 else 0.0
    reserva_red = 0.0
    if bool(f.get("es_cedis", False)):
        reg = _region(str(almacen), regiones)
        dem, dem_total, n_cedis = 0.0, 0.0, 0
        if "es_cedis" in g.columns and "demanda_diaria" in g.columns:
            for _, x in g.iterrows():
                if bool(x["es_cedis"]):
                    n_cedis += 1
                    continue
                if float(x["demanda_diaria"] or 0) <= 0:
                    continue
                dem_total += float(x["demanda_diaria"])
                if _region(x["almacen"], regiones) == reg:
                    dem += float(x["demanda_diaria"])
        if dem <= 0 and dem_total > 0:
            # sin regiones configuradas no se sabe qué hospitales atiende cada CEDIS: se reparte la demanda de la red
            # entre los CEDIS por igual (conservador; en Configuración ▸ Regiones se afina)
            dem = dem_total / max(1, n_cedis)
        reserva_red = dem * float(cfg.reserva_cedis_dias)
    return propio + reserva_red


def proponer_rebalanceo(resur: pd.DataFrame, regiones: dict[str, str] | None = None, compromisos: dict | None = None,
                        cfg: ConfigPronostico | None = None, simular=None, hoy: pd.Timestamp | None = None) -> pd.DataFrame:
    """Transferencias internas con ASIGNACIÓN CONJUNTA de existencias: cada origen reparte lo que realmente puede
    ceder entre todas las necesidades (las más urgentes primero), descontando lo que ya comprometieron otras
    propuestas activas (borradores del agente incluidos) y conservando una reserva operativa:
      • un CEDIS conserva la demanda diaria de los hospitales de su región × `reserva_cedis_dias` (no se interpreta
        "sin consumo propio" como "todo el inventario está libre");
      • un almacén con consumo propio conserva punto de reorden × 1.5 + su demanda del horizonte.
    Las cantidades se redondean a la precisión de la unidad y la cobertura "después" se recalcula con la misma
    proyección día por día del plan (cuando se recibe `simular`)."""
    if resur.empty:
        return pd.DataFrame()
    regiones = regiones or {}
    cfg = cfg or ConfigPronostico()
    comp_origen = dict((compromisos or {}).get("origen", {}))
    comp_destino = dict((compromisos or {}).get("destino", {}))
    hoy = (hoy or pd.Timestamp.now()).normalize()
    dia_llegada = max(1, int(round(cfg.lead_time_interno)))
    orden_crit = {"desabasto": 0, "critico": 1, "reordenar": 2}
    filas = []
    for pid, g in resur.groupby("producto_id"):
        pid = int(pid)
        unidad = str(g["unidad"].iloc[0])
        necesitan = g[g["criticidad"].isin(orden_crit) & (g["sugerido"] > 0)].copy()
        if necesitan.empty:
            continue
        necesitan["_orden"] = necesitan["criticidad"].map(orden_crit)
        necesitan = necesitan.sort_values(["dia_quiebre", "_orden", "sugerido"], ascending=[True, True, False], na_position="last")
        fuentes = g[g["criticidad"].isin(["exceso", "ok", "sin_movimiento", "fuente"])].copy()
        if fuentes.empty:
            continue
        piso, disponible = {}, {}
        for i, f in fuentes.iterrows():
            piso[i] = reserva_operativa(resur, pid, str(f["almacen"]), cfg, regiones)
            ya = float(comp_origen.get((pid, str(f["almacen"])), 0.0))
            disponible[i] = float(f["stock_proyectado"] or 0) - piso[i] - ya
        fuentes["prioridad"] = np.where(fuentes["es_cedis"], 0, np.where(fuentes["criticidad"] == "exceso", 1, 2))
        for _, n in necesitan.iterrows():
            ya_cubierto = float(comp_destino.get((pid, str(n["almacen"])), 0.0))
            faltante = redondear(max(0.0, float(n["sugerido"]) - ya_cubierto), unidad, cfg.precision, arriba=True)
            if faltante <= 0:
                continue
            reg_dest = _region(n["almacen"], regiones)
            fuentes["misma_region"] = fuentes["almacen"].map(lambda a: 0 if _region(a, regiones) == reg_dest else 1)
            for i, f in fuentes.sort_values(["misma_region", "prioridad", "stock_proyectado"], ascending=[True, True, False]).iterrows():
                if faltante <= 0:
                    break
                libre = redondear(max(0.0, disponible[i]), unidad, cfg.precision, abajo=True)
                if libre <= 0 or f["almacen"] == n["almacen"]:
                    continue
                q = redondear(min(faltante, libre), unidad, cfg.precision, abajo=True)
                if q <= 0:
                    continue
                cob_dest_despues = cob_orig_despues = None
                if simular is not None:
                    try:
                        cob_dest_despues = simular(pid, str(n["almacen"]), [(dia_llegada, q)], [])["cobertura_dias"]
                        if float(f["demanda_diaria"]) > 0:
                            cob_orig_despues = simular(pid, str(f["almacen"]), [], [(dia_llegada, q)])["cobertura_dias"]
                    except Exception:  # noqa: BLE001
                        pass
                if cob_dest_despues is None and n["demanda_diaria"]:
                    cob_dest_despues = round((float(n["stock_proyectado"]) + q) / float(n["demanda_diaria"]), 1)
                if cob_orig_despues is None and float(f["demanda_diaria"]) > 0:
                    cob_orig_despues = round((float(f["stock_proyectado"]) - q) / float(f["demanda_diaria"]), 1)
                # llega a tiempo si entra a más tardar el día del quiebre (una entrada del día t sirve la demanda del día t);
                # la fecha NECESARIA recomendada es un día antes (margen de recepción); holgura = días de margen
                dia_q = n.get("dia_quiebre")
                llega_a_tiempo = True if (dia_q is None or pd.isna(dia_q)) else dia_llegada <= int(dia_q)
                holgura = None if (dia_q is None or pd.isna(dia_q)) else int(dia_q) - dia_llegada
                filas.append({"producto_id": pid, "producto": n["producto"], "origen": f["almacen"], "destino": n["almacen"],
                              "cantidad": q, "unidad": unidad, "criticidad_destino": n["criticidad"],
                              "cobertura_destino_dias": n["dias_cobertura"], "cobertura_resultante_dias": cob_dest_despues,
                              "stock_origen": f["stock_actual"], "disponible_origen": round(libre, decimales_de(unidad, cfg.precision)),
                              "reserva_origen": round(piso[i], 2), "comprometido_origen_previo": round(float(comp_origen.get((pid, str(f["almacen"])), 0.0)), 2),
                              "cubierto_previo_destino": round(ya_cubierto, 2),
                              "cobertura_origen_resultante": cob_orig_despues, "fecha_quiebre_destino": n.get("fecha_quiebre"),
                              "fecha_necesaria": n.get("fecha_necesaria"),
                              "fecha_llegada_estimada": (hoy + pd.Timedelta(days=dia_llegada)).date().isoformat(),
                              "llega_a_tiempo": bool(llega_a_tiempo), "holgura_dias": holgura, "importe": round(q * float(n["costo_unit"]), 2),
                              "motivo": (f"{n['almacen']} está en «{n['criticidad']}» ({n['dias_cobertura']} días de cobertura hasta agotar"
                                         + (f", quiebre el {n['fecha_quiebre']}" if n.get("fecha_quiebre") else "") + "); "
                                         f"{f['almacen']} tiene {f['stock_actual']:.0f} {unidad} utilizables, conserva {piso[i]:.0f} de reserva operativa"
                                         + (f" y ya tiene {comp_origen.get((pid, str(f['almacen'])), 0.0):.0f} comprometidos en otras propuestas" if comp_origen.get((pid, str(f["almacen"]))) else "")
                                         + f"; puede ceder {libre:.0f}.")})
                disponible[i] -= q
                comp_origen[(pid, str(f["almacen"]))] = float(comp_origen.get((pid, str(f["almacen"])), 0.0)) + q
                faltante = redondear(faltante - q, unidad, cfg.precision)
    return pd.DataFrame(filas)


# ── resumen e histórico ─────────────────────────────────────────────────────
def _resumen(resur: pd.DataFrame, cadu: pd.DataFrame, rebal: pd.DataFrame, abc: pd.DataFrame, dim: str, h: int,
             retrasadas: pd.DataFrame, desfase: int, comprometida: dict, folios_prog: pd.DataFrame | None = None,
             comp_por_unidad: dict | None = None) -> dict:
    comp_por_unidad = comp_por_unidad or {}
    if resur.empty:
        return {}
    local = resur[resur["nivel"] == "local"]
    red = resur[resur["nivel"] == "red"]
    con_wape = local[local["mape"].notna()]
    wape_pond = float(np.average(con_wape["mape"], weights=con_wape["ultimos_28d"] + 1e-9)) if len(con_wape) else None
    return {
        "dimension": dim, "horizonte_dias": h, "combinaciones": int(len(resur)),
        "criticidad": local["criticidad"].value_counts().to_dict(),
        "importe_compra_sugerida": round(float(red["importe_sugerido"].sum()), 2),
        "importe_resurtido_interno": round(float(local["importe_sugerido"].sum()), 2),
        "valor_inventario_total": round(float(red["valor_inventario"].fillna(0).sum()), 2),
        "valor_inventario_exceso": round(float(local[local["criticidad"].isin(["exceso", "sin_movimiento"])]["valor_inventario"].fillna(0).sum()), 2),
        "productos_red_por_comprar": int((red["sugerido"] > 0).sum()),
        "importe_caducidad_en_riesgo": round(float(cadu["importe_en_riesgo"].sum()), 2) if not cadu.empty else 0.0,
        "lotes_en_riesgo": int(len(cadu)) if not cadu.empty else 0,
        "transferencias_propuestas": int(len(rebal)) if not rebal.empty else 0,
        "wape_promedio": round(float(con_wape["mape"].mean()), 1) if len(con_wape) else None,
        "wape_ponderado": round(wape_pond, 1) if wape_pond is not None else None,
        "sesgo_promedio": round(float(local["sesgo_pct"].dropna().mean()), 1) if local["sesgo_pct"].notna().any() else None,
        "confianza": local["confianza"].value_counts().to_dict(),
        "historico_insuficiente": int((local["metodo"] == "insuficiente").sum()),
        "dias_imputados": int(local["dias_imputados"].sum()),
        "captura_atrasada_dias": max(0, desfase),
        "entregas_retrasadas": int(len(retrasadas)) if retrasadas is not None else 0,
        "en_camino_unidades": round(float(local["en_camino"].sum()), 2),
        "demanda_comprometida_total": round(float(sum(comprometida.values())), 2),
        "folios_programados": int(folios_prog["folios"].sum()) if (folios_prog is not None and not folios_prog.empty) else 0,
        "hospitales_con_programacion": int(folios_prog["hospital"].nunique()) if (folios_prog is not None and not folios_prog.empty) else 0,
        "comprometida_por_unidad": {str(k): round(float(v), 1) for k, v in comp_por_unidad.items()},
        "productos_A": int((abc["abc"] == "A").sum()) if not abc.empty else 0,
        "metodos": local["metodo"].value_counts().to_dict(),
    }


def _historico_demanda(d: pd.DataFrame, hoy: pd.Timestamp) -> dict:
    d = d.copy()
    d["mes"] = d["fecha"].dt.to_period("M").astype(str)
    mensual = d.groupby(["mes", "unidad"]).agg(cantidad=("cantidad", "sum"), importe=("importe", "sum")).reset_index()
    mensual_imp = d.groupby("mes").agg(importe=("importe", "sum"), folios=("folio", "nunique")).reset_index()
    sem = d.set_index("fecha")["importe"].resample("W").sum()
    out = {"mensual": mensual_imp.round(2).to_dict("records"),
           "mensual_por_unidad": mensual.round(2).to_dict("records"),
           "semanal_ultimas_26": [{"semana": k.date().isoformat(), "importe": round(float(v), 2)} for k, v in sem.tail(26).items()]}
    r90 = d[d["fecha"] > hoy - pd.Timedelta(days=90)].groupby(["producto_id", "producto", "unidad"])["cantidad"].sum()
    p90 = d[(d["fecha"] <= hoy - pd.Timedelta(days=90)) & (d["fecha"] > hoy - pd.Timedelta(days=180))] \
        .groupby(["producto_id", "producto", "unidad"])["cantidad"].sum()
    mov = []
    for k, v in r90.items():
        prev = float(p90.get(k, 0.0))
        mov.append({"producto_id": int(k[0]), "producto": k[1], "unidad": k[2], "ult_90d": round(float(v), 2), "prev_90d": round(prev, 2),
                    "variacion_pct": round(100 * (v / prev - 1), 1) if prev else None})
    out["crecimiento_90d"] = sorted(mov, key=lambda x: -(abs(x["variacion_pct"]) if x["variacion_pct"] is not None else 0))
    return out
