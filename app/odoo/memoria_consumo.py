"""Memoria local del consumo: la primera lectura baja todo de Odoo; las siguientes sólo lo nuevo o lo que cambió.

Qué se guarda en el disco persistente (DATA_DIR/cache/consumo_<modelo>.pkl):
  • las líneas de consumo tal como Odoo las devolvió (con su write_date),
  • las cabeceras de folio (con su write_date),
  • los lotes por transferencia de consumo (con el write_date de la transferencia).
Cómo se decide qué releer en cada corrida (nada se supone: todo se le pregunta a Odoo, pero con búsquedas baratas):
  1. `search` de los ids que HOY cumplen el dominio (ventana de fechas, folios no cancelados). Lo que ya no cumple,
     desaparece del resultado; lo que no está en memoria, se lee.
  2. `search` de las líneas con write_date posterior a la última sincronización → se releen.
  3. `search` de los folios con write_date posterior → sus líneas y su cabecera se releen (un cambio de técnico, fecha
     o estado vive en la cabecera).
  4. Para lotes, se compara el write_date de cada transferencia con el guardado.
El resultado es el mismo que una lectura completa; sólo cambia cuánto se baja de Odoo. Un cambio en el mapeo de campos
invalida la memoria (se vuelve a leer todo). «Releer consumo completo» en Configuración la borra.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .. import db
from ..config import settings

_LOCK = threading.Lock()
MARGEN_SEG = 120          # se releen también los registros escritos en los 2 min previos a la última sincronización
FMT = "%Y-%m-%d %H:%M:%S"


def activa() -> bool:
    return bool(getattr(settings, "CONSUMO_INCREMENTAL", True))


def _ruta(modelo: str):
    return settings.DATA_DIR / "cache" / f"consumo_{modelo.replace('.', '_')}.pkl"


def nueva(modelo: str, cols: list[str], modelo_folio: str, cols_folio: list[str]) -> dict:
    return {"version": 1, "modelo": modelo, "cols": list(cols), "lineas": pd.DataFrame(), "visto": {},
            "modelo_folio": modelo_folio, "cols_folio": list(cols_folio), "folios": pd.DataFrame(),
            "lotes": {}, "pickings_wd": {}, "sync_lineas": "", "sync_folios": "", "actualizado": ""}


def cargar(modelo: str, cols: list[str], modelo_folio: str, cols_folio: list[str]) -> dict | None:
    """Devuelve la memoria guardada si corresponde al mismo modelo y a los mismos campos; si no, None (lectura completa)."""
    if not activa():
        return None
    ruta = _ruta(modelo)
    if not ruta.exists():
        return None
    try:
        with _LOCK:
            m = pd.read_pickle(ruta)
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "Memoria de consumo ilegible; se vuelve a leer todo", str(e))
        return None
    if not isinstance(m, dict) or m.get("version") != 1 or m.get("modelo") != modelo or list(m.get("cols") or []) != list(cols) \
            or m.get("modelo_folio") != modelo_folio or list(m.get("cols_folio") or []) != list(cols_folio):
        db.log("info", "odoo", "El mapeo de consumo cambió: se vuelve a leer todo de Odoo", f"{modelo}")
        return None
    return m


def guardar(memoria: dict) -> None:
    if not activa():
        return
    ruta = _ruta(memoria["modelo"])
    ruta.parent.mkdir(parents=True, exist_ok=True)
    memoria["actualizado"] = datetime.now().strftime(FMT)
    tmp = ruta.with_suffix(".tmp")
    with _LOCK:
        pd.to_pickle(memoria, tmp)
        os.replace(tmp, ruta)


def borrar() -> int:
    """Borra todas las memorias de consumo (Configuración ▸ Releer consumo completo)."""
    carpeta = settings.DATA_DIR / "cache"
    n = 0
    if carpeta.exists():
        for f in carpeta.glob("consumo_*.pkl"):
            f.unlink(missing_ok=True)
            n += 1
    return n


def estado() -> dict:
    """Para la pantalla de Configuración."""
    carpeta = settings.DATA_DIR / "cache"
    out = {"activa": activa(), "memorias": []}
    if carpeta.exists():
        for f in sorted(carpeta.glob("consumo_*.pkl")):
            try:
                m = pd.read_pickle(f)
                out["memorias"].append({"modelo": m.get("modelo"), "lineas": int(len(m.get("lineas", []))),
                                        "folios": int(len(m.get("folios", []))), "actualizado": m.get("actualizado", ""),
                                        "mb": round(f.stat().st_size / 1e6, 1)})
            except Exception:  # noqa: BLE001
                out["memorias"].append({"modelo": f.stem, "error": "ilegible"})
    return out


# ── utilidades ───────────────────────────────────────────────────────────────
def _menos_margen(ts: str) -> str:
    try:
        return (datetime.strptime(ts, FMT) - timedelta(seconds=MARGEN_SEG)).strftime(FMT)
    except (TypeError, ValueError):
        return ts


def max_write_date(filas: list[dict], actual: str = "") -> str:
    m = actual or ""
    for r in filas:
        wd = r.get("write_date")
        if isinstance(wd, str) and wd > m:
            m = wd
    return m


def _leer_bloques(cli, modelo: str, ids: list[int], cols: list[str]) -> list[dict]:
    """Lectura por ids con las conexiones en paralelo del cliente (o en serie si el cliente no las tiene)."""
    if not ids:
        return []
    if hasattr(cli, "search_read_por_ids"):
        return cli.search_read_por_ids(modelo, ids, cols)
    out = []
    for i in range(0, len(ids), 1000):
        out.extend(cli.search_read(modelo, [["id", "in", ids[i:i + 1000]]], cols, limite=0))
    return out


def _df_desde(filas: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(filas)
    if df.empty:
        return df
    df["id"] = df["id"].astype(int)
    return df.set_index("id", drop=False)


def _upsert(base: pd.DataFrame, nuevas: pd.DataFrame) -> pd.DataFrame:
    if nuevas is None or nuevas.empty:
        return base
    if base is None or base.empty:
        return nuevas
    base = base.drop(index=[i for i in nuevas.index if i in base.index], errors="ignore")
    return pd.concat([base, nuevas], axis=0)


# ── líneas de consumo ────────────────────────────────────────────────────────
def leer_lineas(cli, memoria: dict, modelo: str, dominio: list, cols: list[str], tope: int,
                modelo_folio: str, campo_folio: str, por_folio: bool) -> tuple[list[dict], dict]:
    """Devuelve las filas que hoy cumplen el dominio, bajando de Odoo sólo lo que falta o cambió.
    Devuelve (filas, detalle) donde detalle dice cuántas se releyeron y por qué."""
    t0 = time.time()
    ids_act = [int(i) for i in (cli.execute(modelo, "search", [dominio], order="id", limit=tope) or [])]
    detalle: dict[str, Any] = {"ids": len(ids_act), "nuevas": 0, "cambiadas": 0, "por_folio": 0, "seg_busqueda": 0.0}
    lineas: pd.DataFrame = memoria.get("lineas")
    en_memoria = set(lineas.index.tolist()) if lineas is not None and not lineas.empty else set()
    releer = {i for i in ids_act if i not in en_memoria}
    detalle["nuevas"] = len(releer)
    conjunto_act = set(ids_act)
    if memoria.get("sync_lineas") and en_memoria:
        # candidatas: escritas desde la última sincronización (con margen). Se comparan con el write_date guardado
        # para releer únicamente las que de verdad cambiaron.
        desde = _menos_margen(memoria["sync_lineas"])
        candidatas = cli.search_read(modelo, dominio + [["write_date", ">=", desde]], ["write_date"], limite=tope)
        wd_mem = lineas["write_date"].to_dict() if "write_date" in lineas.columns else {}
        cambiadas = [int(c["id"]) for c in candidatas
                     if int(c["id"]) in conjunto_act and int(c["id"]) not in releer and wd_mem.get(int(c["id"])) != c.get("write_date")]
        detalle["cambiadas"] = len(cambiadas)
        releer.update(cambiadas)
    # Un cambio en la cabecera del folio (técnico, fecha, estado) se recoge al unir cabeceras (leer_folios); la ventana y
    # los folios cancelados se re-evalúan con la búsqueda de ids. Las líneas no necesitan releerse por eso.
    detalle["seg_busqueda"] = round(time.time() - t0, 1)
    frescas = _leer_bloques(cli, modelo, sorted(releer), cols) if releer else []
    nuevas_df = _df_desde(frescas)
    lineas = _upsert(lineas if lineas is not None else pd.DataFrame(), nuevas_df)
    memoria["lineas"] = lineas
    memoria["sync_lineas"] = max_write_date(frescas, memoria.get("sync_lineas", ""))
    hoy = datetime.now().strftime("%Y-%m-%d")
    visto = memoria.setdefault("visto", {})
    for i in ids_act:
        visto[i] = hoy
    # resultado: exactamente los ids vigentes, en orden de id, con las filas más recientes
    presentes = [i for i in ids_act if i in lineas.index]
    filas = lineas.loc[presentes].to_dict("records") if presentes else []
    detalle["releidas"] = len(frescas)
    return filas, detalle


def podar(memoria: dict, dias: int | None = None) -> None:
    """Olvida líneas y folios que ninguna corrida ha necesitado en `dias` (ventanas más largas que ninguna corrida usa)."""
    dias = dias or int(getattr(settings, "CONSUMO_MEMORIA_DIAS", 120) or 120)
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    visto = memoria.get("visto") or {}
    viejos = [i for i, d in visto.items() if d < limite]
    if not viejos:
        return
    for i in viejos:
        visto.pop(i, None)
    lineas = memoria.get("lineas")
    if lineas is not None and not lineas.empty:
        memoria["lineas"] = lineas.drop(index=[i for i in viejos if i in lineas.index], errors="ignore")


# ── cabeceras de folio ───────────────────────────────────────────────────────
def leer_folios(cli, memoria: dict, modelo_folio: str, ids: list[int], cols: list[str]) -> tuple[list[dict], dict]:
    ids = sorted({int(i) for i in ids})
    folios: pd.DataFrame = memoria.get("folios")
    en_memoria = set(folios.index.tolist()) if folios is not None and not folios.empty else set()
    releer = {i for i in ids if i not in en_memoria}
    detalle = {"ids": len(ids), "nuevos": len(releer), "cambiados": 0}
    if memoria.get("sync_folios") and en_memoria:
        desde = _menos_margen(memoria["sync_folios"])
        candidatos = cli.search_read(modelo_folio, [["id", "in", ids], ["write_date", ">=", desde]], ["write_date"], limite=0)
        wd_mem = folios["write_date"].to_dict() if "write_date" in folios.columns else {}
        cambiados = [int(c["id"]) for c in candidatos if int(c["id"]) not in releer and wd_mem.get(int(c["id"])) != c.get("write_date")]
        detalle["cambiados"] = len(cambiados)
        releer.update(cambiados)
    frescas = _leer_bloques(cli, modelo_folio, sorted(releer), cols) if releer else []
    folios = _upsert(folios if folios is not None else pd.DataFrame(), _df_desde(frescas))
    memoria["folios"] = folios
    memoria["sync_folios"] = max_write_date(frescas, memoria.get("sync_folios", ""))
    presentes = [i for i in ids if i in folios.index]
    filas = folios.loc[presentes].to_dict("records") if presentes else []
    detalle["releidos"] = len(frescas)
    return filas, detalle


# ── lotes por transferencia ──────────────────────────────────────────────────
def leer_lotes(cli, memoria: dict, pids: list[int], cols: list[str]) -> tuple[list[dict], dict]:
    """Devuelve las líneas de movimiento con lote de las transferencias `pids`, releyendo sólo las transferencias
    nuevas o cuyo write_date cambió."""
    pids = sorted({int(p) for p in pids})
    guardados: dict = memoria.setdefault("pickings_wd", {})
    lotes: dict = memoria.setdefault("lotes", {})
    wd_actual: dict[int, str] = {}
    if hasattr(cli, "search_read_por_ids"):
        filas_wd = cli.search_read_por_ids("stock.picking", pids, ["write_date"])
    else:
        filas_wd = []
        for i in range(0, len(pids), 1000):
            filas_wd.extend(cli.search_read("stock.picking", [["id", "in", pids[i:i + 1000]]], ["write_date"], limite=0))
    for f in filas_wd:
        wd_actual[int(f["id"])] = f.get("write_date") or ""
    releer = [p for p in pids if p not in lotes or guardados.get(p) != wd_actual.get(p, "")]
    detalle = {"pickings": len(pids), "releidos": len(releer)}
    if releer:
        if hasattr(cli, "search_read_por_ids"):
            frescas = cli.search_read_por_ids("stock.move.line", releer, cols, dominio_extra=[["lot_id", "!=", False]], campo="picking_id")
        else:
            frescas = []
            for i in range(0, len(releer), 1000):
                frescas.extend(cli.search_read("stock.move.line", [["picking_id", "in", releer[i:i + 1000]], ["lot_id", "!=", False]], cols, limite=0))
        por_picking: dict[int, list[dict]] = {p: [] for p in releer}
        for f in frescas:
            pk = f.get("picking_id")
            pk = int(pk[0]) if isinstance(pk, (list, tuple)) else int(pk or 0)
            por_picking.setdefault(pk, []).append(f)
        for p in releer:
            lotes[p] = por_picking.get(p, [])
            guardados[p] = wd_actual.get(p, "")
    filas = [f for p in pids for f in lotes.get(p, [])]
    return filas, detalle
