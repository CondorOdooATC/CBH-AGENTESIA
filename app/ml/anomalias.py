"""Motor de detección de anomalías de consumo (Agente 1).

Combina cuatro familias de evidencia y las fusiona en un puntaje explicable:

  1. Perfiles robustos (mediana + MAD) por producto, producto×hospital,
     producto×médico y producto×auxiliar. Para anestésicos volátiles con
     duración de cirugía se modela la *tasa* (mL/min), que normaliza por la
     duración y es lo que un anestesiólogo reconocería como "normal".
  2. Isolation Forest sobre un vector de características por línea
     (cantidad normalizada, tasa, hora, día, sin médico, duración…).
  3. Reglas de negocio con semántica clínica/operativa (básculas, envases,
     lotes, horarios, duplicados, consumo sin cirugía…).
  4. Detección de patrones agregados: cambio de nivel por sub-almacén,
     actor con sobreconsumo sistemático vs. sus pares, frecuencia atípica.

Cada hallazgo lleva sus *motivos* en lenguaje claro, los *métodos* que lo
dispararon, el consumo *esperado*, la *desviación* y el *importe en riesgo*.
Los perfiles aprendidos y la retroalimentación humana (justificada/confirmada)
mueven los umbrales en corridas posteriores.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

try:  # scikit-learn es opcional: si falta, se omite el Isolation Forest
    from sklearn.ensemble import IsolationForest
    _SKLEARN = True
except Exception:  # noqa: BLE001
    _SKLEARN = False


# ── configuración ───────────────────────────────────────────────────────────
@dataclass
class ConfigAnomalias:
    umbral_z: float = 2.5                 # |z robusto| a partir del cual una línea es sospechosa
    min_muestras: int = 8                 # mínimo de observaciones para confiar en un perfil
    tolerancia_bascula_pct: float = 0.15  # discrepancia báscula vs captura tolerada
    tolerancia_bascula_g: float = 1.0     # precisión de la báscula (gramos)
    densidad_default: float = 1.5         # g/mL para volátiles sin densidad conocida
    tasa_max_ml_min: float = 0.60         # techo clínico para volátiles (mL/min)
    hora_noche_ini: int = 22
    hora_noche_fin: int = 6
    ventana_reciente_dias: int = 30       # para cambio de nivel
    ventana_base_dias: int = 120
    ratio_cambio_nivel: float = 1.5
    ratio_actor_vs_pares: float = 1.30
    contaminacion_if: float = 0.01
    pesos: dict = field(default_factory=lambda: {
        "z_producto": 1.0, "z_hospital": 1.0, "z_medico": 0.8, "z_auxiliar": 1.2,
        "isolation_forest": 1.0,
        "R01_EXCEDE_ENVASE": 5.0, "R02_BASCULA_IMPOSIBLE": 5.0, "R03_BASCULA_DISCREPANCIA": 3.0,
        "R04_SIN_CIRUGIA": 5.0, "R05_HORARIO_ATIPICO": 1.2, "R06_TASA_CLINICA": 3.0,
        "R07_LOTE_VIAJERO": 3.0, "R08_DUPLICADO": 2.0, "R09_LOTE_CADUCADO": 3.0,
    })
    # Contenido por envase y densidad por producto (se completan desde el catálogo o el nombre)
    capacidad: dict[int, float] = field(default_factory=dict)
    densidad: dict[int, float] = field(default_factory=dict)
    # Umbrales aprendidos: {(ambito, clave): ajuste}
    ajustes: dict[tuple[str, str], float] = field(default_factory=dict)
    huellas_justificadas: set[str] = field(default_factory=set)
    # Reglas que no se evalúan (modo de respaldo sobre movimientos de inventario: no hay folio médico, médico ni
    # duración, así que «sin cirugía» y «duplicado» serían ruido; también puede fijarse por política).
    reglas_desactivadas: set[str] = field(default_factory=set)


SEVERIDAD_PUNTOS = {"critica": 10, "alta": 5, "media": 2, "baja": 1}
REGLAS_CRITICAS = {"R01_EXCEDE_ENVASE", "R04_SIN_CIRUGIA"}

DESCRIPCION_REGLAS = {
    "R01_EXCEDE_ENVASE": "La cantidad registrada supera el contenido de un envase",
    "R02_BASCULA_IMPOSIBLE": "Lectura de báscula inconsistente (peso final mayor que el inicial): posible cambio de frasco, recarga o error de asociación",
    "R03_BASCULA_DISCREPANCIA": "Diferencia no conciliada entre lo que la báscula registró y lo capturado",
    "R04_SIN_CIRUGIA": "Consumo de anestésico sin cirugía, médico o paciente asociado (requiere conciliación)",
    "R05_HORARIO_ATIPICO": "Consumo en horario nocturno o fin de semana",
    "R06_TASA_CLINICA": "Tasa de consumo por minuto fuera del rango clínico",
    "R07_LOTE_VIAJERO": "Un lote se consumió en una unidad médica que no lo maneja habitualmente",
    "R08_DUPLICADO": "Línea duplicada (mismo folio, producto, cantidad y hora)",
    "R09_LOTE_CADUCADO": "Se consumió un lote ya caducado",
    "R10_CAMBIO_NIVEL": "El consumo del sub-almacén cambió de nivel de forma sostenida",
    "R11_ACTOR_DESVIADO": "Consumo sistemáticamente mayor que el de sus pares: diferencia no conciliada que requiere verificación",
    "R12_FRECUENCIA_ATIPICA": "Número de folios por día fuera de lo normal para la persona",
    "z_producto": "Cantidad atípica frente al histórico del producto",
    "z_hospital": "Cantidad atípica frente al histórico del producto en esa unidad médica",
    "z_medico": "Cantidad atípica frente al histórico del médico",
    "z_auxiliar": "Cantidad atípica frente al histórico del auxiliar",
    "isolation_forest": "Combinación inusual de características (modelo Isolation Forest)",
}


# ── utilidades ──────────────────────────────────────────────────────────────
def _mad(x: np.ndarray) -> float:
    med = np.median(x)
    return float(np.median(np.abs(x - med)))


def z_robusto(x: np.ndarray, mediana: float, mad: float, desv: float | None = None) -> np.ndarray:
    """Z-score robusto (0.6745·(x−mediana)/MAD). Si MAD≈0 cae a la desviación estándar."""
    escala = 1.4826 * mad if mad > 1e-9 else (desv or 0.0)
    if escala <= 1e-9:
        return np.zeros_like(x, dtype=float)
    return (x - mediana) / escala


def capacidad_desde_nombre(nombre: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|mL|ML|g|gr|kg|L)\b", nombre or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    u = m.group(2).lower()
    if u == "l":
        v *= 1000
    if u == "kg":
        v *= 1000
    return v


def huella(fila: pd.Series) -> str:
    base = f"{fila.get('folio')}|{fila.get('producto_id')}|{fila.get('cantidad')}|{fila.get('fecha')}|{fila.get('lote')}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def _es_volatil(df: pd.DataFrame) -> pd.Series:
    unidad = df["unidad"].str.lower().str.strip()
    vol = unidad.isin(["ml", "mililitro", "mililitros"]) | df["producto"].str.lower().str.contains(
        "sevo|desflu|isoflu|halot|anest", regex=True)
    if "pesable" in df.columns:      # en el CB Ticket el producto con «control por pesaje» es el anestésico volátil
        vol = vol | df["pesable"].fillna(False).astype(bool)
    return vol


# ── perfiles ────────────────────────────────────────────────────────────────
def construir_perfiles(df: pd.DataFrame, cfg: ConfigAnomalias) -> dict[str, dict]:
    """Perfiles robustos por ámbito. La métrica es la tasa (mL/min) cuando existe, si no la cantidad."""
    perfiles: dict[str, dict] = {}
    df = df.copy()
    ambitos = {
        "producto": ["producto_id"],
        "producto_hospital": ["producto_id", "hospital"],
        "producto_medico": ["producto_id", "medico"],
        "producto_auxiliar": ["producto_id", "auxiliar"],
        "producto_subalmacen": ["producto_id", "subalmacen"],
    }
    for ambito, cols in ambitos.items():
        sub = df[df[cols].notna().all(axis=1)]
        for c in cols[1:]:
            sub = sub[sub[c].astype(str).str.strip() != ""]
        if sub.empty:
            continue
        for clave_t, g in sub.groupby(cols, sort=False):
            clave = "|".join(str(k) for k in (clave_t if isinstance(clave_t, tuple) else (clave_t,)))
            for metrica in ("cantidad", "tasa"):
                vals = g[metrica].dropna().to_numpy(dtype=float)
                vals = vals[vals > 0]
                if len(vals) < 3:
                    continue
                perfiles[f"{ambito}|{clave}|{metrica}"] = {
                    "ambito": ambito, "clave": f"{clave}|{metrica}", "etiqueta": str(g["producto"].iloc[0]),
                    "n": int(len(vals)), "media": float(vals.mean()), "mediana": float(np.median(vals)),
                    "mad": _mad(vals), "desv": float(vals.std(ddof=1)) if len(vals) > 1 else 0.0,
                    "p05": float(np.percentile(vals, 5)), "p25": float(np.percentile(vals, 25)),
                    "p75": float(np.percentile(vals, 75)), "p95": float(np.percentile(vals, 95)),
                    "minimo": float(vals.min()), "maximo": float(vals.max()),
                    "unidad": str(g["unidad"].iloc[0]) if metrica == "cantidad" else "mL/min",
                }
    return perfiles


# ── detección principal ─────────────────────────────────────────────────────
def detectar(df: pd.DataFrame, cfg: ConfigAnomalias | None = None) -> dict[str, Any]:
    """Ejecuta el pipeline completo y devuelve hallazgos, perfiles, riesgos e histórico."""
    cfg = cfg or ConfigAnomalias()
    if df.empty:
        return {"hallazgos": pd.DataFrame(), "agregados": pd.DataFrame(), "perfiles": {},
                "riesgos": {}, "historico": {}, "basculas": {}, "n_lineas": 0}

    d = df.copy()
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")
    d = d[d["fecha"].notna()].reset_index(drop=True)
    if "dia" not in d.columns:
        d["dia"] = d["fecha"].dt.date
    d["hora"] = d["fecha"].dt.hour
    d["dow"] = d["fecha"].dt.dayofweek
    d["es_finde"] = d["dow"] >= 5
    d["es_noche"] = (d["hora"] >= cfg.hora_noche_ini) | (d["hora"] < cfg.hora_noche_fin)
    d["volatil"] = _es_volatil(d)
    d["duracion_min"] = pd.to_numeric(d["duracion_min"], errors="coerce")
    d["tasa"] = np.where((d["duracion_min"] > 0) & d["volatil"], d["cantidad"] / d["duracion_min"], np.nan)
    d["sin_medico"] = d["medico"].astype(str).str.strip() == ""
    d["costo_unit"] = np.where(d["cantidad"] > 0, d["importe"] / d["cantidad"], 0.0)
    d["huella"] = d.apply(huella, axis=1)

    for pid, nombre in d[["producto_id", "producto"]].drop_duplicates().itertuples(index=False):
        if pid not in cfg.capacidad:
            cap = capacidad_desde_nombre(nombre)
            if cap:
                cfg.capacidad[int(pid)] = cap

    perfiles = construir_perfiles(d, cfg)
    metodos: dict[int, list[str]] = {i: [] for i in d.index}
    motivos: dict[int, list[str]] = {i: [] for i in d.index}
    puntos = pd.Series(0.0, index=d.index)
    z_max = pd.Series(0.0, index=d.index)      # la familia z aporta una sola vez (su máximo)
    esperado = pd.Series(np.nan, index=d.index)

    def marcar(idx, metodo: str, motivo: str, peso: float | None = None, contrib: float = 1.0):
        if metodo in cfg.reglas_desactivadas:      # p. ej. en modo de respaldo (sin folios médicos) R04/R08 no aplican
            return
        metodos[idx].append(metodo)
        motivos[idx].append(motivo)
        aporte = (peso if peso is not None else cfg.pesos.get(metodo, 1.0)) * contrib
        if metodo.startswith("z_"):
            z_max[idx] = max(z_max[idx], aporte)
        else:
            puntos[idx] += aporte

    # 1 ▸ Perfiles robustos ------------------------------------------------
    for ambito, cols, metodo in (("producto", ["producto_id"], "z_producto"),
                                 ("producto_hospital", ["producto_id", "hospital"], "z_hospital"),
                                 ("producto_medico", ["producto_id", "medico"], "z_medico"),
                                 ("producto_auxiliar", ["producto_id", "auxiliar"], "z_auxiliar")):
        for clave_t, g in d.groupby(cols, sort=False):
            clave = "|".join(str(k) for k in (clave_t if isinstance(clave_t, tuple) else (clave_t,)))
            if any(str(k).strip() == "" for k in (clave_t if isinstance(clave_t, tuple) else (clave_t,))[1:]):
                continue
            # métrica: tasa si el grupo la tiene mayoritariamente, si no cantidad
            usa_tasa = g["tasa"].notna().mean() > 0.6
            metrica = "tasa" if usa_tasa else "cantidad"
            p = perfiles.get(f"{ambito}|{clave}|{metrica}")
            if not p or p["n"] < cfg.min_muestras:
                continue
            ajuste = cfg.ajustes.get((ambito, f"{clave}|{metrica}"), 0.0)
            umbral = max(1.5, cfg.umbral_z + ajuste)
            vals = g[metrica].to_numpy(dtype=float)
            # Escala robusta con piso: evita que cantidades discretas (1-3 pz) con MAD≈0 disparen todo
            discreto = metrica == "cantidad" and p["mediana"] < 10 and float(np.mean(np.mod(vals[~np.isnan(vals)], 1) == 0)) > 0.9
            piso = 1.0 if discreto else 0.05 * p["mediana"]
            escala = max(1.4826 * p["mad"], 0.5 * p["desv"], piso, 1e-9)
            z = (np.nan_to_num(vals, nan=p["mediana"]) - p["mediana"]) / escala
            minimo_abs = max(piso, 0.25 * p["mediana"])
            for idx, zi, vi in zip(g.index, z, vals):
                if np.isnan(vi):
                    continue
                if zi > umbral and vi > p["p95"] and (vi - p["mediana"]) >= minimo_abs:
                    # solo sobreconsumo; el subconsumo se reporta aparte en histórico
                    exp = p["mediana"] * (d.at[idx, "duracion_min"] if metrica == "tasa" else 1.0)
                    if np.isnan(esperado[idx]) or ambito != "producto":
                        esperado[idx] = exp
                    etiqueta = {"producto": "del producto", "producto_hospital": "de la unidad médica",
                                "producto_medico": "del médico", "producto_auxiliar": "del auxiliar"}[ambito]
                    marcar(idx, metodo,
                           f"{'Tasa' if metrica == 'tasa' else 'Cantidad'} {vi:.2f} vs. mediana {p['mediana']:.2f} "
                           f"{etiqueta} (z={zi:.1f}, n={p['n']})", contrib=min(zi / umbral, 3.0))

    # 2 ▸ Isolation Forest -------------------------------------------------
    if _SKLEARN and len(d) >= 200:
        feats = pd.DataFrame({
            "cant_log": np.log1p(d["cantidad"].clip(lower=0)),
            "tasa": d["tasa"].fillna(d["tasa"].median() if d["tasa"].notna().any() else 0),
            "hora": d["hora"], "dow": d["dow"], "noche": d["es_noche"].astype(int),
            "finde": d["es_finde"].astype(int), "sin_medico": d["sin_medico"].astype(int),
            "dur": d["duracion_min"].fillna(0), "volatil": d["volatil"].astype(int),
        })
        # normalizar cantidad dentro de cada producto para que el bosque compare peras con peras
        med = d.groupby("producto_id")["cantidad"].transform("median").replace(0, 1)
        feats["cant_rel"] = d["cantidad"] / med
        try:
            modelo = IsolationForest(n_estimators=200, contamination=cfg.contaminacion_if, random_state=7)
            modelo.fit(feats.to_numpy())
            sc = -modelo.score_samples(feats.to_numpy())  # mayor = más anómalo
            umbral_if = np.percentile(sc, 100 * (1 - cfg.contaminacion_if))
            for idx, s in zip(d.index, sc):
                if s >= umbral_if and s > 0.60:
                    marcar(idx, "isolation_forest",
                           f"Patrón inusual de características (score {s:.2f})", contrib=min((s - 0.55) * 4, 1.5))
        except Exception:  # noqa: BLE001
            pass

    # 3 ▸ Reglas de negocio ----------------------------------------------
    # R01 excede envase
    for idx, row in d.iterrows():
        cap = cfg.capacidad.get(int(row["producto_id"]) if pd.notna(row["producto_id"]) else -1)
        if cap and row["volatil"] and row["cantidad"] > cap * 1.02:
            esperado[idx] = np.nan if np.isnan(esperado[idx]) else esperado[idx]
            marcar(idx, "R01_EXCEDE_ENVASE", f"Cantidad {row['cantidad']:.1f} {row['unidad']} > envase de {cap:.0f}")

    # R02 / R03 básculas
    # un pesaje existe sólo si hay peso inicial > 0 (en el CB Ticket los campos valen 0.0 cuando no se pesó)
    con_peso = d[d["peso_inicial"].notna() & d["peso_final"].notna() & (d["peso_inicial"] > 0) & (d["cantidad"] > 0)]
    basculas = {"lineas_con_bascula": int(len(con_peso)), "imposibles": 0, "discrepancias": 0,
                "discrepancia_media_pct": None, "gramos_no_explicados": 0.0}
    if not con_peso.empty:
        dens = con_peso["producto_id"].map(lambda p: cfg.densidad.get(int(p), cfg.densidad_default))
        if "pesable" in con_peso.columns:
            # convención operativa de CBH para productos con control por pesaje: 1 g = 1 mL (salvo densidad explícita del producto)
            explicita = con_peso["producto_id"].map(lambda p: int(p) in cfg.densidad if pd.notna(p) else False)
            dens = dens.where(~con_peso["pesable"].fillna(False).astype(bool) | explicita, 1.0)
        consumo_bascula_ml = (con_peso["peso_inicial"] - con_peso["peso_final"]) / dens
        if "consumo_ml" in con_peso.columns:
            # si Odoo ya calculó el consumo de báscula en mL, ese dato manda
            calc = pd.to_numeric(con_peso["consumo_ml"], errors="coerce")
            consumo_bascula_ml = calc.where(calc.notna() & (calc > 0), consumo_bascula_ml)
        diff = consumo_bascula_ml - con_peso["cantidad"]
        pct = diff / con_peso["cantidad"].replace(0, np.nan)
        basculas["discrepancia_media_pct"] = round(float(pct.abs().median() * 100), 2)
        for idx, pi, pf, cb, df_, pc in zip(con_peso.index, con_peso["peso_inicial"], con_peso["peso_final"],
                                             consumo_bascula_ml, diff, pct):
            if pf > pi + cfg.tolerancia_bascula_g:
                basculas["imposibles"] += 1
                marcar(idx, "R02_BASCULA_IMPOSIBLE",
                       f"Peso final {pf:.1f} g > peso inicial {pi:.1f} g (¿cambio de frasco, recarga o lectura mal asociada?)",
                       contrib=1.2)
            elif abs(pc) > cfg.tolerancia_bascula_pct and abs(df_) * dens.get(idx, 1.5) > cfg.tolerancia_bascula_g * 2:
                basculas["discrepancias"] += 1
                sentido = "más" if df_ > 0 else "menos"
                marcar(idx, "R03_BASCULA_DISCREPANCIA",
                       f"Báscula: salieron {cb:.1f} mL del frasco vs. {d.at[idx, 'cantidad']:.1f} capturados "
                       f"({abs(pc) * 100:.0f}% {sentido})", contrib=min(abs(pc) / cfg.tolerancia_bascula_pct, 3.0))
                if df_ > 0:
                    basculas["gramos_no_explicados"] += float(df_ * dens.get(idx, 1.5))
                    if np.isnan(esperado[idx]):
                        esperado[idx] = float(d.at[idx, "cantidad"])
    basculas["gramos_no_explicados"] = round(basculas["gramos_no_explicados"], 1)

    # R04 sin cirugía (volátil sin médico o sin duración)
    m4 = d["volatil"] & (d["sin_medico"] | ~(d["duracion_min"] > 0))
    for idx in d[m4].index:
        marcar(idx, "R04_SIN_CIRUGIA", "Anestésico sin médico / duración de cirugía registrados")
        if np.isnan(esperado[idx]):
            esperado[idx] = 0.0

    # R05 horario atípico (solo volátiles; refuerza a otras reglas)
    m5 = d["volatil"] & (d["es_noche"] | d["es_finde"])
    for idx in d[m5].index:
        marcar(idx, "R05_HORARIO_ATIPICO",
               "Consumo " + ("nocturno" if d.at[idx, "es_noche"] else "en fin de semana"),
               contrib=1.0 if m4[idx] else 0.5)

    # R06 tasa clínica
    m6 = d["tasa"].notna() & (d["tasa"] > cfg.tasa_max_ml_min)
    for idx in d[m6].index:
        marcar(idx, "R06_TASA_CLINICA",
               f"{d.at[idx, 'tasa']:.2f} mL/min supera el techo clínico de {cfg.tasa_max_ml_min:.2f}",
               contrib=min(d.at[idx, "tasa"] / cfg.tasa_max_ml_min, 3.0))
        if np.isnan(esperado[idx]):
            p = perfiles.get(f"producto|{int(d.at[idx, 'producto_id'])}|tasa")
            if p:
                esperado[idx] = p["mediana"] * d.at[idx, "duracion_min"]

    # R07 lote viajero: un lote aparece en una unidad médica que históricamente no lo maneja
    con_lote = d[(d["lote"].astype(str).str.strip() != "") & (d["hospital"].astype(str).str.strip() != "")]
    if not con_lote.empty:
        hist = con_lote.groupby(["lote", "hospital"]).size().rename("n").reset_index()
        tot = hist.groupby("lote")["n"].sum().rename("total")
        hist = hist.merge(tot, on="lote")
        hist["pct"] = hist["n"] / hist["total"]
        # lotes con historial suficiente y unidades "ajenas" (< 10 % de su consumo y ≤ 5 líneas)
        ajenos = {(r.lote, r.hospital) for r in hist.itertuples()
                  if r.total >= 15 and r.pct < 0.10 and r.n <= 5}
        habitual = {r.lote: r.hospital for r in hist.sort_values("pct", ascending=False)
                    .drop_duplicates("lote").itertuples()}
        for idx, row in con_lote.iterrows():
            if (row["lote"], row["hospital"]) in ajenos:
                marcar(idx, "R07_LOTE_VIAJERO",
                       f"Lote {row['lote']} pertenece a {habitual.get(row['lote'], '?')} y se consumió en {row['hospital']}")

    # R08 duplicados
    dup_cols = ["folio", "producto_id", "cantidad", "fecha"]
    dups = d.duplicated(subset=dup_cols, keep="first")
    for idx in d[dups].index:
        marcar(idx, "R08_DUPLICADO", "Registro idéntico a otro del mismo folio")
        if np.isnan(esperado[idx]):
            esperado[idx] = 0.0

    # R09 lote caducado
    if "caducidad" in d.columns:
        cad = pd.to_datetime(d["caducidad"], errors="coerce")
        m9 = cad.notna() & (cad < d["fecha"])
        for idx in d[m9].index:
            marcar(idx, "R09_LOTE_CADUCADO", f"Lote {d.at[idx, 'lote']} caducó el {cad[idx].date()}")

    # 4 ▸ Fusión en hallazgos por línea ----------------------------------
    # Contexto humano: ¿ya lo justificaron?
    justificada = d["huella"].isin(cfg.huellas_justificadas)
    puntos = puntos + z_max
    d["puntos"] = puntos
    d["esperado"] = esperado
    idx_h = [i for i in d.index if metodos[i] and not justificada[i]
             and (puntos[i] >= 2.0 or any(m in REGLAS_CRITICAS for m in metodos[i]))]
    filas = []
    for i in idx_h:
        r = d.loc[i]
        ms = metodos[i]
        sev = _severidad(puntos[i], ms)
        exp = r["esperado"]
        desv = (r["cantidad"] - exp) if pd.notna(exp) else np.nan
        riesgo = max(0.0, float(desv)) * float(r["costo_unit"]) if pd.notna(desv) else float(r["importe"]) * 0.5
        filas.append({
            "tipo": "linea", "fecha": r["fecha"], "folio": r["folio"], "hospital": r["hospital"],
            "unidad_medica": r["hospital"], "medico": r["medico"], "auxiliar": r["auxiliar"],
            "subalmacen": r["subalmacen"], "almacen": r["almacen"],
            "producto_id": int(r["producto_id"]) if pd.notna(r["producto_id"]) else None,
            "producto": r["producto"], "lote": r["lote"], "cantidad": float(r["cantidad"]),
            "unidad": r["unidad"], "esperado": None if pd.isna(exp) else round(float(exp), 2),
            "desviacion": None if pd.isna(desv) else round(float(desv), 2),
            "importe_riesgo": round(riesgo, 2), "score": round(float(puntos[i]), 2), "severidad": sev,
            "metodos": ms, "motivos": motivos[i], "huella": r["huella"], "linea_id": r.get("id"),
            "duracion_min": r["duracion_min"], "tasa": r["tasa"],
            "peso_inicial": r["peso_inicial"], "peso_final": r["peso_final"],
        })
    hallazgos = pd.DataFrame(filas)
    if not hallazgos.empty:
        hallazgos = hallazgos.sort_values(["score", "importe_riesgo"], ascending=False).reset_index(drop=True)

    # 5 ▸ Patrones agregados (el importe se calcula sobre líneas NO señaladas individualmente) ----
    agregados = _patrones_agregados(d, cfg, ya_senaladas=set(idx_h))

    # 6 ▸ Riesgo por actor y por unidad ----------------------------------
    riesgos = _indices_riesgo(hallazgos, agregados, d)

    # 7 ▸ Histórico --------------------------------------------------------
    historico = analisis_historico(d)

    return {"hallazgos": hallazgos, "agregados": agregados, "perfiles": perfiles, "riesgos": riesgos,
            "historico": historico, "basculas": basculas, "n_lineas": int(len(d)),
            "config": {k: v for k, v in cfg.__dict__.items() if k not in ("ajustes", "huellas_justificadas")}}


def _severidad(p: float, metodos: list[str]) -> str:
    if any(m in REGLAS_CRITICAS for m in metodos) or p >= 6:
        return "critica"
    if p >= 3.5:
        return "alta"
    if p >= 2.0:
        return "media"
    return "baja"


# ── patrones agregados ──────────────────────────────────────────────────────
def _patrones_agregados(d: pd.DataFrame, cfg: ConfigAnomalias, ya_senaladas: set | None = None) -> pd.DataFrame:
    filas: list[dict] = []
    ya_senaladas = ya_senaladas or set()
    hoy = d["fecha"].max()
    reciente = d[d["fecha"] > hoy - pd.Timedelta(days=cfg.ventana_reciente_dias)]
    base = d[(d["fecha"] <= hoy - pd.Timedelta(days=cfg.ventana_reciente_dias))
             & (d["fecha"] > hoy - pd.Timedelta(days=cfg.ventana_reciente_dias + cfg.ventana_base_dias))]

    # R10 cambio de nivel por producto × sub-almacén (o hospital si no hay sub-almacén).
    # Se evalúan dos ventanas (30 y 60 días) y se conserva la más contundente por clave.
    dim = "subalmacen" if (d["subalmacen"].str.strip() != "").mean() > 0.5 else "hospital"
    mejores: dict[tuple, dict] = {}
    for ventana in (cfg.ventana_reciente_dias, cfg.ventana_reciente_dias * 2):
        rec_v = d[d["fecha"] > hoy - pd.Timedelta(days=ventana)]
        base_v = d[(d["fecha"] <= hoy - pd.Timedelta(days=ventana))
                   & (d["fecha"] > hoy - pd.Timedelta(days=ventana + cfg.ventana_base_dias))]
        for clave, g_rec in rec_v.groupby(["producto_id", dim]):
            g_base = base_v[(base_v["producto_id"] == clave[0]) & (base_v[dim] == clave[1])]
            usa_tasa = g_rec["tasa"].notna().sum() >= 6 and g_base["tasa"].notna().sum() >= 10
            vals_r = g_rec["tasa"].dropna() if usa_tasa else g_rec["cantidad"]
            vals_b = g_base["tasa"].dropna() if usa_tasa else g_base["cantidad"]
            if len(vals_r) < 6 or len(vals_b) < 10:
                continue
            if usa_tasa:
                mr, mb = float(vals_r.median()), float(vals_b.median())
                cambio_min = 0.0
            else:  # cantidades discretas: promedio y cambio absoluto mínimo de 1 unidad o 25 %
                mr, mb = float(vals_r.mean()), float(vals_b.mean())
                cambio_min = max(1.0, 0.25 * mb)
            if mb <= 0 or mr / mb < cfg.ratio_cambio_nivel or (mr - mb) < cambio_min:
                continue
            costo = float((g_rec["importe"].sum() / g_rec["cantidad"].sum()) if g_rec["cantidad"].sum() else 0)
            g_nuevas = g_rec[~g_rec.index.isin(ya_senaladas)]
            exceso_cant = float((g_nuevas["cantidad"] * (1 - mb / mr)).sum())
            cand = {
                "tipo": "agregado", "regla": "R10_CAMBIO_NIVEL", "severidad": "alta" if mr / mb < 2 else "critica",
                "producto_id": int(clave[0]), "producto": g_rec["producto"].iloc[0], "dimension": dim,
                "clave": clave[1], "hospital": g_rec["hospital"].iloc[0], "actor": "",
                "valor_reciente": round(mr, 3), "valor_base": round(mb, 3), "ratio": round(mr / mb, 2),
                "n_reciente": int(len(vals_r)), "n_base": int(len(vals_b)), "ventana_dias": ventana,
                "importe_riesgo": round(exceso_cant * costo, 2),
                "motivo": (f"La {'mediana de tasa (mL/min)' if usa_tasa else 'cantidad promedio por folio'} "
                           f"de {g_rec['producto'].iloc[0]} en {clave[1]} pasó de {mb:.3f} a {mr:.3f} "
                           f"(×{mr / mb:.2f}) en los últimos {ventana} días ({len(vals_r)} registros)."),
                "score": round(min(10.0, 3 * mr / mb), 2),
            }
            prev = mejores.get(clave)
            if prev is None or cand["score"] * math.sqrt(cand["n_reciente"]) > prev["score"] * math.sqrt(prev["n_reciente"]):
                mejores[clave] = cand
    filas.extend(mejores.values())

    # R11 actor desviado vs pares (auxiliar y médico) en la ventana reciente
    for actor_col in ("auxiliar", "medico"):
        sub = reciente[(reciente[actor_col].str.strip() != "") & reciente["tasa"].notna()]
        if sub.empty:
            continue
        for (pid, hosp), g in sub.groupby(["producto_id", "hospital"]):
            if g[actor_col].nunique() < 2:
                continue
            med_pares_total = g.groupby(actor_col)["tasa"].median()
            for actor, g_a in g.groupby(actor_col):
                if len(g_a) < 8:
                    continue
                pares = med_pares_total.drop(actor)
                if pares.empty:
                    continue
                m_actor, m_pares = float(g_a["tasa"].median()), float(pares.median())
                consistente = float(g_a["tasa"].quantile(0.25)) > m_pares  # 75 % de sus cirugías arriba de los pares
                if len(g_a) >= 10 and m_pares > 0 and m_actor / m_pares >= cfg.ratio_actor_vs_pares and consistente:
                    costo = float(g_a["importe"].sum() / g_a["cantidad"].sum()) if g_a["cantidad"].sum() else 0
                    g_nuevas = g_a[~g_a.index.isin(ya_senaladas)]
                    exceso = float((g_nuevas["cantidad"] * (1 - m_pares / m_actor)).sum())
                    ratio = m_actor / m_pares
                    filas.append({
                        "tipo": "agregado", "regla": "R11_ACTOR_DESVIADO",
                        "severidad": "critica" if ratio >= 1.4 else "alta",
                        "producto_id": int(pid), "producto": g_a["producto"].iloc[0], "dimension": actor_col,
                        "clave": actor, "hospital": hosp, "actor": actor,
                        "valor_reciente": round(m_actor, 3), "valor_base": round(m_pares, 3), "ratio": round(ratio, 2),
                        "n_reciente": int(len(g_a)), "n_base": int(len(pares)),
                        "importe_riesgo": round(exceso * costo, 2),
                        "motivo": (f"{actor} consume {m_actor:.3f} mL/min de {g_a['producto'].iloc[0]} vs. "
                                   f"{m_pares:.3f} de sus pares en {hosp} (×{ratio:.2f}, {len(g_a)} cirugías)."),
                        "score": round(min(10.0, 4 * ratio), 2),
                    })

    # R12 frecuencia atípica de folios por auxiliar/día
    sub = d[d["auxiliar"].str.strip() != ""]
    if not sub.empty:
        fol = sub.groupby(["auxiliar", "dia"])["folio"].nunique().reset_index(name="folios")
        p95 = fol.groupby("auxiliar")["folios"].quantile(0.95)
        for _, r in fol.iterrows():
            lim = max(3, p95.get(r["auxiliar"], 3) * 1.5)
            if r["folios"] > lim and pd.Timestamp(r["dia"]) > hoy - pd.Timedelta(days=cfg.ventana_reciente_dias):
                filas.append({
                    "tipo": "agregado", "regla": "R12_FRECUENCIA_ATIPICA", "severidad": "media",
                    "producto_id": None, "producto": "", "dimension": "auxiliar", "clave": r["auxiliar"],
                    "hospital": sub[sub["auxiliar"] == r["auxiliar"]]["hospital"].iloc[0], "actor": r["auxiliar"],
                    "valor_reciente": int(r["folios"]), "valor_base": round(float(p95.get(r["auxiliar"], 3)), 1),
                    "ratio": round(r["folios"] / max(1, p95.get(r["auxiliar"], 3)), 2), "n_reciente": 1, "n_base": 0,
                    "importe_riesgo": 0.0, "score": 2.0,
                    "motivo": f"{r['auxiliar']} registró {int(r['folios'])} folios el {r['dia']} (su p95 es {p95.get(r['auxiliar'], 3):.0f}).",
                })
    out = pd.DataFrame(filas)
    if not out.empty:
        out = out.sort_values(["score", "importe_riesgo"], ascending=False).reset_index(drop=True)
    return out


# ── índices de riesgo ───────────────────────────────────────────────────────
def _indices_riesgo(hallazgos: pd.DataFrame, agregados: pd.DataFrame, d: pd.DataFrame) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for dim in ("auxiliar", "medico", "hospital", "subalmacen", "producto"):
        tabla: dict[str, dict] = {}
        if not hallazgos.empty and dim in hallazgos.columns:
            for _, r in hallazgos.iterrows():
                k = str(r[dim]).strip()
                if not k:
                    continue
                t = tabla.setdefault(k, {"clave": k, "puntos": 0.0, "hallazgos": 0, "importe_riesgo": 0.0,
                                         "criticas": 0, "altas": 0})
                t["puntos"] += SEVERIDAD_PUNTOS.get(r["severidad"], 1)
                t["hallazgos"] += 1
                t["importe_riesgo"] += float(r["importe_riesgo"] or 0)
                t["criticas"] += r["severidad"] == "critica"
                t["altas"] += r["severidad"] == "alta"
        if not agregados.empty:
            col = "actor" if dim in ("auxiliar", "medico") else ("clave" if dim == "subalmacen" else dim)
            for _, r in agregados.iterrows():
                if dim in ("auxiliar", "medico", "subalmacen") and r["dimension"] != dim:
                    continue
                k = str(r.get(col, "")).strip()
                if not k:
                    continue
                t = tabla.setdefault(k, {"clave": k, "puntos": 0.0, "hallazgos": 0, "importe_riesgo": 0.0,
                                         "criticas": 0, "altas": 0})
                t["puntos"] += SEVERIDAD_PUNTOS.get(r["severidad"], 1) * 2  # los patrones pesan doble
                t["hallazgos"] += 1
                t["importe_riesgo"] += float(r["importe_riesgo"] or 0)
                t["criticas"] += r["severidad"] == "critica"
                if r["severidad"] == "critica":
                    t["patron_critico"] = True
        # volumen para contextualizar
        vol = d.groupby(dim)["importe"].sum() if dim in d.columns else pd.Series(dtype=float)
        lineas = d.groupby(dim).size() if dim in d.columns else pd.Series(dtype=int)
        filas = []
        for k, t in tabla.items():
            n_lin = max(int(lineas.get(k, 0)), 1)
            densidad = 100.0 * t["puntos"] / n_lin          # puntos por cada 100 líneas
            indice = 100 * (1 - math.exp(-densidad / 12)) if n_lin >= 20 else 100 * (1 - math.exp(-t["puntos"] / 25))
            if t.get("patron_critico"):
                indice = max(indice, 65.0)
            t["indice"] = round(indice, 1)
            t["importe_riesgo"] = round(t["importe_riesgo"], 2)
            t["lineas"] = int(lineas.get(k, 0))
            t["importe_total"] = round(float(vol.get(k, 0.0)), 2)
            t["pct_riesgo"] = round(100 * t["importe_riesgo"] / t["importe_total"], 2) if t["importe_total"] else 0.0
            filas.append(t)
        out[dim] = sorted(filas, key=lambda x: (-x["indice"], -x["importe_riesgo"]))[:25]
    return out


# ── análisis histórico ──────────────────────────────────────────────────────
def analisis_historico(d: pd.DataFrame) -> dict[str, Any]:
    """Tendencias, estacionalidad, comparativos y cambios estructurales."""
    if d.empty:
        return {}
    d = d.copy()
    d["mes"] = d["fecha"].dt.to_period("M").astype(str)
    hoy = d["fecha"].max()
    out: dict[str, Any] = {}

    # Serie mensual total y por producto
    vol_mask = _es_volatil(d)
    mensual = d.groupby("mes").agg(importe=("importe", "sum"), folios=("folio", "nunique"), lineas=("cantidad", "size")).reset_index()
    ml = d[vol_mask].groupby("mes")["cantidad"].sum().rename("ml_anestesicos_volatiles")
    mensual = mensual.merge(ml, on="mes", how="left").fillna({"ml_anestesicos_volatiles": 0.0})
    out["mensual"] = mensual.round(2).to_dict("records")

    def _pendiente(y: np.ndarray) -> float:
        if len(y) < 3 or np.nanmean(y) == 0:
            return 0.0
        x = np.arange(len(y))
        a, _ = np.polyfit(x, y, 1)
        return float(100 * a / np.nanmean(y))  # % del promedio por periodo

    tend = []
    for pid, g in d.groupby("producto_id"):
        serie = g.groupby("mes")["cantidad"].sum()
        serie = serie[serie.index < hoy.to_period("M").strftime("%Y-%m")]  # excluir mes incompleto
        if len(serie) < 4:
            continue
        pend = _pendiente(serie.to_numpy(dtype=float))
        ult3 = float(serie.tail(3).mean())
        prev3 = float(serie.iloc[-6:-3].mean()) if len(serie) >= 6 else np.nan
        tend.append({"producto_id": int(pid), "producto": g["producto"].iloc[0], "meses": int(len(serie)),
                     "tendencia_pct_mes": round(pend, 2), "prom_ult_3m": round(ult3, 2),
                     "prom_3m_previos": None if np.isnan(prev3) else round(prev3, 2),
                     "variacion_pct": None if (np.isnan(prev3) or prev3 == 0) else round(100 * (ult3 / prev3 - 1), 1),
                     "importe_ult_3m": round(float(g[g["mes"].isin(serie.tail(3).index)]["importe"].sum()), 2)})
    out["tendencias_producto"] = sorted(tend, key=lambda x: -abs(x["tendencia_pct_mes"]))

    # Comparativo por hospital: últimos 30 días vs 30 anteriores
    r30 = d[d["fecha"] > hoy - pd.Timedelta(days=30)]
    p30 = d[(d["fecha"] <= hoy - pd.Timedelta(days=30)) & (d["fecha"] > hoy - pd.Timedelta(days=60))]
    comp = []
    for h in sorted(set(d["hospital"]) - {""}):
        a = float(r30[r30["hospital"] == h]["importe"].sum())
        b = float(p30[p30["hospital"] == h]["importe"].sum())
        fa, fb = int(r30[r30["hospital"] == h]["folio"].nunique()), int(p30[p30["hospital"] == h]["folio"].nunique())
        comp.append({"hospital": h, "importe_30d": round(a, 2), "importe_30d_prev": round(b, 2),
                     "variacion_pct": round(100 * (a / b - 1), 1) if b else None,
                     "folios_30d": fa, "folios_30d_prev": fb,
                     "importe_por_folio": round(a / fa, 2) if fa else None,
                     "importe_por_folio_prev": round(b / fb, 2) if fb else None})
    out["comparativo_hospital"] = sorted(comp, key=lambda x: -(x["variacion_pct"] or 0))

    # Estacionalidad: índice por día de la semana y por mes calendario (sobre cantidad de volátiles)
    vol = d[_es_volatil(d)] if _es_volatil(d).any() else d
    dow = vol.groupby(vol["fecha"].dt.dayofweek)["cantidad"].sum()
    dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    if dow.sum():
        out["estacionalidad_semana"] = {dias[i]: round(float(v / dow.mean()), 2) for i, v in dow.items()}
    mes_cal = vol.groupby(vol["fecha"].dt.month)["cantidad"].sum()
    if mes_cal.sum():
        out["estacionalidad_mes"] = {int(m): round(float(v / mes_cal.mean()), 2) for m, v in mes_cal.items()}

    # Comparativo interanual si hay ≥ 13 meses
    if mensual["mes"].nunique() >= 13:
        m = mensual.set_index("mes")["importe"]
        yoy = []
        for mes in m.index[-6:]:
            y, mm = int(mes[:4]), mes[5:]
            prev = f"{y - 1}-{mm}"
            if prev in m.index and m[prev]:
                yoy.append({"mes": mes, "importe": round(float(m[mes]), 2), "importe_anio_prev": round(float(m[prev]), 2),
                            "variacion_pct": round(100 * (m[mes] / m[prev] - 1), 1)})
        out["interanual"] = yoy

    # Cambios estructurales (CUSUM simple) en la tasa semanal de volátiles por hospital
    cambios = []
    for h, g in vol.groupby("hospital"):
        sem = g.set_index("fecha")["cantidad"].resample("W").sum()
        sem = sem[sem > 0]
        if len(sem) < 12:
            continue
        x = sem.to_numpy(dtype=float)
        mu, sd = x[:max(6, len(x) // 2)].mean(), x[:max(6, len(x) // 2)].std() or 1.0
        s, s_max, punto = 0.0, 0.0, None
        for i, xi in enumerate(x):
            s = max(0.0, s + (xi - mu) / sd - 0.5)
            if s > s_max:
                s_max, punto = s, i
        if s_max > 5 and punto is not None:
            cambios.append({"hospital": h, "semana": str(sem.index[punto].date()), "cusum": round(s_max, 1),
                            "nivel_antes": round(float(mu), 1),
                            "nivel_despues": round(float(x[punto:].mean()), 1)})
    out["cambios_estructurales"] = cambios
    return out


# ── serialización ───────────────────────────────────────────────────────────
def hallazgos_a_registros(h: pd.DataFrame, corrida_id: int) -> list[dict]:
    regs = []
    for _, r in h.iterrows():
        regs.append({
            "corrida_id": corrida_id, "fecha": str(r["fecha"]), "folio": r["folio"], "hospital": r["hospital"],
            "unidad_medica": r["unidad_medica"], "medico": r["medico"], "almacen": r["almacen"],
            "producto_id": r["producto_id"], "producto": r["producto"], "lote": r["lote"],
            "cantidad": r["cantidad"], "unidad": r["unidad"], "esperado": r["esperado"],
            "desviacion": r["desviacion"], "importe_riesgo": r["importe_riesgo"], "score": r["score"], "severidad": r["severidad"],
            "metodos": json.dumps(list(r["metodos"]), ensure_ascii=False),
            "motivos": json.dumps(list(r["motivos"]), ensure_ascii=False),
            "explicacion": None, "huella": r["huella"], "estado": "nueva",
        })
    return regs
