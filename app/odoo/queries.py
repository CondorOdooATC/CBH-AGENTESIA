"""Consultas de negocio sobre Odoo, normalizadas a DataFrames de pandas.

Todas las funciones devuelven columnas con nombres estables
(fecha, folio, hospital, medico, producto, cantidad, …) sin importar cómo se
llamen los campos en la base real: la traducción la hace ``schema``.
"""
from __future__ import annotations

import math
import time

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from .. import db
from ..config import settings
from . import memoria_consumo as mc
from . import schema
from .client import OdooClient, get_client

FMT = "%Y-%m-%d %H:%M:%S"


# ── utilidades de normalización ─────────────────────────────────────────────
def _m2o(v: Any) -> tuple[int | None, str]:
    """Convierte el formato many2one de Odoo [id, 'nombre'] en (id, nombre)."""
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return int(v[0]), str(v[1])
    if isinstance(v, (int, float)) and v:
        return int(v), ""
    return None, ""


def _expandir(df: pd.DataFrame, columnas: list[str]) -> pd.DataFrame:
    for c in columnas:
        if c in df.columns:
            ids, nombres = zip(*df[c].map(_m2o)) if len(df) else ((), ())
            df[f"{c}_id"] = list(ids)
            df[c] = list(nombres)
    return df


def _rango(desde: str | date | None, hasta: str | date | None, dias: int = 180) -> tuple[str, str]:
    hoy = datetime.now()
    if isinstance(hasta, date):
        hasta = hasta.isoformat()
    if isinstance(desde, date):
        desde = desde.isoformat()
    h = f"{hasta} 23:59:59" if hasta else hoy.strftime(FMT)
    d = f"{desde} 00:00:00" if desde else (hoy - timedelta(days=dias)).strftime(FMT)
    return d, h


# ── catálogos ───────────────────────────────────────────────────────────────
def catalogo_productos(cli: OdooClient | None = None, solo_almacenables: bool = True,
                       limite: int = 0) -> pd.DataFrame:
    cli = cli or get_client()
    ent = "producto"
    m, mp = schema.modelo(ent), schema.mapa(ent)
    dominio: list = [["active", "=", True]]
    if solo_almacenables and mp.get("tipo"):
        dominio.append([mp["tipo"], "in", ["product", "consu"]])
    cols = [v for v in mp.values() if v]
    rows = cli.search_read_all(m, dominio, list(dict.fromkeys(cols)), tope=limite or 200_000)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["producto_id", "producto", "codigo", "categoria", "unidad"])
    ren = {v: k for k, v in mp.items() if v in df.columns}
    df = df.rename(columns=ren)
    df = _expandir(df, [c for c in ("categoria", "unidad") if c in df.columns])
    df["producto_id"] = df["id"]
    df["producto"] = df.get("nombre", df.get("codigo", ""))
    return df


def almacenes(cli: OdooClient | None = None) -> pd.DataFrame:
    cli = cli or get_client()
    mp = schema.mapa("almacen")
    rows = cli.search_read(schema.modelo("almacen"), [], [v for v in mp.values() if v] + ["id"])
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["id", "nombre", "codigo"])
    df = df.rename(columns={v: k for k, v in mp.items() if v in df.columns})
    return _expandir(df, [c for c in ("ubicacion_stock", "compania") if c in df.columns])


def ubicaciones(cli: OdooClient | None = None, solo_internas: bool = True) -> pd.DataFrame:
    cli = cli or get_client()
    mp = schema.mapa("ubicacion")
    dominio = [["usage", "=", "internal"]] if solo_internas else []
    rows = cli.search_read(schema.modelo("ubicacion"), dominio,
                           [v for v in mp.values() if v] + ["id"], limite=5000)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["id", "nombre", "uso"])
    df = df.rename(columns={v: k for k, v in mp.items() if v in df.columns})
    return _expandir(df, [c for c in ("almacen", "padre") if c in df.columns])


def unidades_medicas(cli: OdooClient | None = None) -> pd.DataFrame:
    """Contactos marcados como unidad médica; si el campo no existe, devuelve vacío."""
    cli = cli or get_client()
    mp = schema.mapa("unidad_medica")
    campo_check = mp.get("es_unidad")
    if not campo_check:
        return pd.DataFrame(columns=["id", "nombre"])
    try:
        rows = cli.search_read(schema.modelo("unidad_medica"), [[campo_check, "=", True]],
                               ["id", mp.get("nombre", "name")], limite=2000)
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer unidades médicas", str(e))
        return pd.DataFrame(columns=["id", "nombre"])
    df = pd.DataFrame(rows)
    return df.rename(columns={mp.get("nombre", "name"): "nombre"}) if not df.empty else \
        pd.DataFrame(columns=["id", "nombre"])


# ── consumo (fuente principal de los dos agentes) ───────────────────────────
_CONSUMO_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}


def consumo(desde: str | None = None, hasta: str | None = None, dias: int = 180,
            productos: list[int] | None = None, hospitales: list[int] | None = None,
            almacenes_ids: list[int] | None = None, cli: OdooClient | None = None,
            tope: int = 200_000, progreso=None) -> pd.DataFrame:
    """Líneas de consumo normalizadas.

    Usa el modelo custom de operaciones médicas si existe; si no, cae a
    ``stock.move.line`` (salidas confirmadas desde ubicaciones internas).
    """
    import time as _t
    from ..config import settings as _settings
    ttl = int(getattr(_settings, "CONSUMO_CACHE_SEG", 0) or 0)
    clave = (str(desde), str(hasta), int(dias), tuple(productos or ()), tuple(hospitales or ()), tuple(almacenes_ids or ()), int(tope), _t.strftime("%Y-%m-%d"))
    if ttl and cli is None:
        hit = _CONSUMO_CACHE.get(clave)
        if hit and _t.time() - hit[0] < ttl:
            return hit[1].copy()
    df = _consumo(desde, hasta, dias, productos, hospitales, almacenes_ids, cli, tope, progreso)
    if ttl and cli is None:
        _CONSUMO_CACHE.clear()
        _CONSUMO_CACHE[clave] = (_t.time(), df.copy())
    return df


def _consumo(desde, hasta, dias, productos, hospitales, almacenes_ids, cli, tope, progreso=None) -> pd.DataFrame:
    avisar = progreso or (lambda *a, **k: None)
    cli = cli or get_client()
    mapeo = schema.cargar()
    ent = mapeo["entidades"].get("consumo", {})
    modelo_consumo = ent.get("modelo") or "stock.move.line"
    usa_respaldo = modelo_consumo == "stock.move.line" or not ent.get("campos")

    d, h = _rango(desde, hasta, dias)
    if usa_respaldo:
        return _consumo_respaldo(cli, d, h, productos, almacenes_ids, tope)

    mp = ent["campos"]
    ent_f = mapeo["entidades"].get("folio", {}) or {}
    modelo_folio, mp_f = ent_f.get("modelo") or "", ent_f.get("campos", {}) or {}
    tipo_fecha_f = ((ent_f.get("detalle") or {}).get("fecha") or {}).get("tipo", "")
    # La línea del CB Ticket no tiene fecha propia: se filtra por la fecha del folio (dominio con ruta many2one) y se
    # excluyen los folios cancelados. Si la línea sí tiene fecha real, se usa la suya.
    campo_fecha = mp.get("fecha") or "create_date"
    por_folio = bool(mp.get("folio_id") and mp_f.get("fecha") and modelo_folio and modelo_folio != "stock.picking"
                     and campo_fecha in ("create_date", "write_date"))
    if por_folio:
        campo_fecha_dom = f"{mp['folio_id']}.{mp_f['fecha']}"
        d_dom, h_dom = (d[:10], h[:10]) if tipo_fecha_f == "date" else (d, h)
    else:
        campo_fecha_dom, d_dom, h_dom = campo_fecha, d, h
    dominio: list = [[campo_fecha_dom, ">=", d_dom], [campo_fecha_dom, "<=", h_dom]]
    if por_folio and mp_f.get("estado"):
        dominio.append([f"{mp['folio_id']}.{mp_f['estado']}", "not in", ["cancel", "cancelled", "cancelado"]])
    if productos and mp.get("producto"):
        dominio.append([mp["producto"], "in", productos])
    if hospitales and mp.get("hospital"):
        dominio.append([mp["hospital"], "in", hospitales])

    # La memoria incremental necesita write_date en el modelo (columna estándar de Odoo); si no la hay, se lee todo.
    try:
        con_wd = "write_date" in (cli.fields_get(modelo_consumo) or {})
    except Exception:  # noqa: BLE001
        con_wd = False
    incremental = mc.activa() and con_wd
    cols = sin_paciente(list(dict.fromkeys([v for v in mp.values() if v] + ["id"] + (["write_date"] if incremental else []))))
    cols_folio = _cols_folio(mp_f, incremental) if (modelo_folio and modelo_folio != "stock.picking" and mp_f) else []
    _t = {"inicio": time.time()}
    # Memoria local: la primera vez se baja todo; después sólo lo nuevo o lo que cambió (ver memoria_consumo.py)
    memoria = mc.cargar(modelo_consumo, cols, modelo_folio, cols_folio) if incremental else None
    detalle_inc: dict | None = None
    rows = None
    if memoria is not None:
        try:
            rows, detalle_inc = mc.leer_lineas(cli, memoria, modelo_consumo, dominio, cols, tope, modelo_folio, mp.get("folio_id"), por_folio)
        except Exception as e:  # noqa: BLE001
            db.log("warn", "odoo", "Memoria de consumo: falló la lectura incremental; se lee todo de Odoo", str(e))
            memoria, rows = None, None
    if rows is None:
        try:
            rows = cli.search_read_all(modelo_consumo, dominio, cols, tope=tope)
        except Exception as e:  # noqa: BLE001
            db.log("warn", "odoo", f"Falla leyendo {modelo_consumo}; uso respaldo", str(e))
            return _consumo_respaldo(cli, d, h, productos, almacenes_ids, tope)
        if incremental:
            try:
                memoria = mc.nueva(modelo_consumo, cols, modelo_folio, cols_folio)
                memoria["lineas"] = mc._df_desde(rows)
                memoria["sync_lineas"] = mc.max_write_date(rows)
                hoy_s = datetime.now().strftime("%Y-%m-%d")
                memoria["visto"] = {int(r["id"]): hoy_s for r in rows}
            except Exception as e:  # noqa: BLE001
                db.log("warn", "odoo", "Memoria de consumo: no se pudo iniciar", str(e))
                memoria = None
    _t["lineas"] = time.time()
    if len(rows) >= tope:
        db.log("warn", "odoo", f"La lectura de consumo alcanzó el tope de {tope:,} líneas",
               "hay más consumo en la ventana del que se leyó; reduce la ventana o sube el tope")
    if detalle_inc:
        avisar("Líneas de consumo al día", f"{len(rows):,} vigentes · releídas {detalle_inc.get('releidas', 0):,} "
               f"({detalle_inc.get('nuevas', 0):,} nuevas, {detalle_inc.get('cambiadas', 0):,} cambiadas, {detalle_inc.get('por_folio', 0):,} por folio) "
               f"en {_t['lineas'] - _t['inicio']:.0f} s")
    else:
        avisar("Líneas de consumo leídas", f"{len(rows):,} en {_t['lineas'] - _t['inicio']:.0f} s (lectura completa; las siguientes serán incrementales)")

    df = pd.DataFrame(rows)
    if df.empty:
        return _df_consumo_vacio()

    df = df.rename(columns={v: k for k, v in mp.items() if v in df.columns})
    df = _expandir(df, [c for c in ("producto", "producto_efectivo", "unidad", "lote", "ubicacion", "ubicacion_destino",
                                    "hospital", "medico", "folio_id", "compania", "auxiliar",
                                    "subalmacen") if c in df.columns])
    df["fecha"] = pd.to_datetime(df.get("fecha"), errors="coerce")
    df["cantidad"] = pd.to_numeric(df.get("cantidad", 0), errors="coerce").fillna(0.0)
    df["importe"] = pd.to_numeric(df.get("importe", 0), errors="coerce").fillna(0.0) if "importe" in df.columns else 0.0
    # _expandir dejó el nombre en «folio_id» y el id en «folio_id_id»: se normalizan a folio / folio_id
    df = df.rename(columns={"folio_id": "folio"})
    if "folio_id_id" in df.columns:
        df = df.rename(columns={"folio_id_id": "folio_id"})
    if "producto_efectivo_id" in df.columns:
        # la variante que realmente se movió en inventario manda para existencias y pronóstico
        ef = df["producto_efectivo_id"].notna()
        df.loc[ef, "producto_id"] = df.loc[ef, "producto_efectivo_id"]
        df.loc[ef & (df["producto_efectivo"].astype(str) != ""), "producto"] = df.loc[ef, "producto_efectivo"]
    df = df.drop(columns=["write_date"], errors="ignore")
    df = _unir_cabecera_folio(df, cli, modelo_folio, mp_f, tipo_fecha_f, memoria)
    _t["cabeceras"] = time.time()
    avisar("Cabeceras de folio unidas", f"{df['folio_id'].nunique() if 'folio_id' in df.columns else 0:,} folios en {_t['cabeceras'] - _t['lineas']:.0f} s")
    df = _unir_lotes_de_consumo(df, cli, memoria)
    _t["lotes"] = time.time()
    df = _valorar_a_costo(df, cli)
    _t["costo"] = time.time()
    df["origen_datos"] = modelo_consumo
    if memoria is not None:
        try:
            det = memoria.pop("_detalle", {})
            mc.podar(memoria)
            mc.guardar(memoria)
        except Exception as e:  # noqa: BLE001
            det = {}
            db.log("warn", "odoo", "Memoria de consumo: no se pudo guardar", str(e))
    else:
        det = {}
    modo = "incremental" if detalle_inc else "completa"
    extra = ""
    if detalle_inc:
        extra = (f" · releídas {detalle_inc.get('releidas', 0):,} de {detalle_inc.get('ids', 0):,} líneas"
                 f" · folios releídos {det.get('folios', {}).get('releidos', 0):,}"
                 f" · transferencias releídas {det.get('lotes', {}).get('releidos', 0):,}")
    db.log("info", "odoo", f"Lectura de consumo ({modo}): {len(df):,} líneas en {_t['costo'] - _t['inicio']:.0f} s",
           f"líneas {_t['lineas'] - _t['inicio']:.0f} s · cabeceras {_t['cabeceras'] - _t['lineas']:.0f} s · lotes {_t['lotes'] - _t['cabeceras']:.0f} s · "
           f"costo {_t['costo'] - _t['lotes']:.0f} s · {getattr(settings, 'ODOO_LECTURAS_PARALELAS', 1)} conexiones en paralelo{extra}")
    return _completar_consumo(df)


def _hora_float_a_td(v) -> pd.Timedelta | None:
    """8.5 (horas decimales, formato de Odoo) → 08:30."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0 or f != f or f > 48:
        return None
    return pd.Timedelta(hours=int(f), minutes=int(round((f - int(f)) * 60)))


_LOGICOS_FOLIO = ("nombre", "fecha", "fecha_registro", "estado", "hospital", "medico", "cirujano", "empleado", "ubicacion",
                  "hora_inicio", "hora_fin", "inicio_dt", "fin_dt", "duracion_min", "tipo_cirugia", "especialidad",
                  "tipo_evento", "turno", "quirofano", "picking_consumo", "paquete", "importe_paquete")


_VETADOS_PACIENTE = ("patient", "paciente", "nss", "curp", "diagnos", "birthdate", "gender")


def sin_paciente(cols: list[str]) -> list[str]:
    """Minimización de datos: ningún campo de paciente se pide a Odoo, aunque un mapeo lo trajera por error."""
    return [c for c in cols if not any(v in str(c).lower() for v in _VETADOS_PACIENTE)]


def _cols_folio(mp_f: dict, con_wd: bool = True) -> list[str]:
    logicos = [k for k in _LOGICOS_FOLIO if mp_f.get(k)]
    return sin_paciente(list(dict.fromkeys([mp_f[k] for k in logicos] + ["id"] + (["write_date"] if con_wd else []))))     # nunca «paciente»


def _unir_cabecera_folio(df: pd.DataFrame, cli: OdooClient, modelo_folio: str, mp_f: dict, tipo_fecha_f: str,
                         memoria: dict | None = None) -> pd.DataFrame:
    """Trae de la cabecera del folio lo que la línea no tiene: fecha real de la cirugía (con hora de inicio de anestesia),
    médico (anestesiólogo), técnico/auxiliar, quirófano, sub-almacén de surtido, tipo de evento, duración.
    Con memoria local, sólo se releen los folios nuevos o modificados."""
    if "folio_id" not in df.columns or not modelo_folio or modelo_folio == "stock.picking" or not mp_f:
        return df
    ids = sorted({int(x) for x in df["folio_id"].dropna().tolist()})
    if not ids:
        return df
    logicos = [k for k in _LOGICOS_FOLIO if mp_f.get(k)]
    cols = _cols_folio(mp_f, memoria is not None)
    filas = None
    if memoria is not None:
        try:
            filas, det = mc.leer_folios(cli, memoria, modelo_folio, ids, cols)
            memoria.setdefault("_detalle", {})["folios"] = det
        except Exception as e:  # noqa: BLE001
            db.log("warn", "odoo", "Memoria de consumo: falló la lectura incremental de folios; se leen completos", str(e))
            filas = None
    if filas is None:
        try:
            filas = cli.search_read_por_ids(modelo_folio, ids, cols)
        except Exception as e:  # noqa: BLE001
            db.log("warn", "odoo", f"No se pudo leer la cabecera de los folios ({modelo_folio})", str(e))
            return df
    if not filas:
        return df
    fdf = pd.DataFrame(filas).drop(columns=["write_date"], errors="ignore").rename(columns={mp_f[k]: f"f_{k}" for k in logicos})
    fdf = fdf.rename(columns={"id": "folio_id"})
    fdf = _expandir(fdf, [c for c in ("f_hospital", "f_medico", "f_cirujano", "f_empleado", "f_ubicacion", "f_quirofano", "f_picking_consumo")
                          if c in fdf.columns])
    df = df.merge(fdf, on="folio_id", how="left")
    # fecha real: fecha de cirugía + hora de inicio de anestesia (o inicio del procedimiento); si no, fecha de registro
    # la hora de inicio puede venir como horas decimales (8.5 = 08:30, formato del CB Ticket) o como datetime completo
    hora_ini_num = pd.to_numeric(df["f_hora_inicio"], errors="coerce") if "f_hora_inicio" in df.columns else pd.Series(float("nan"), index=df.index)
    hora_fin_num = pd.to_numeric(df["f_hora_fin"], errors="coerce") if "f_hora_fin" in df.columns else pd.Series(float("nan"), index=df.index)
    hora_ini_dt = (pd.to_datetime(df["f_hora_inicio"].map(lambda v: None if v in (False, None) or isinstance(v, (int, float)) else v), errors="coerce")
                   if "f_hora_inicio" in df.columns else pd.Series(pd.NaT, index=df.index))
    hora_fin_dt = (pd.to_datetime(df["f_hora_fin"].map(lambda v: None if v in (False, None) or isinstance(v, (int, float)) else v), errors="coerce")
                   if "f_hora_fin" in df.columns else pd.Series(pd.NaT, index=df.index))
    if "f_inicio_dt" in df.columns:
        ini_dt = pd.to_datetime(df["f_inicio_dt"].map(lambda v: None if v is False else v), errors="coerce")
        hora_ini_dt = hora_ini_dt.where(hora_ini_dt.notna(), ini_dt)
    if "f_fin_dt" in df.columns:
        fin_dt = pd.to_datetime(df["f_fin_dt"].map(lambda v: None if v is False else v), errors="coerce")
        hora_fin_dt = hora_fin_dt.where(hora_fin_dt.notna(), fin_dt)
    if "f_fecha" in df.columns:
        base = pd.to_datetime(df["f_fecha"].map(lambda v: None if v is False else v), errors="coerce")
        if "f_fecha_registro" in df.columns:
            base = base.fillna(pd.to_datetime(df["f_fecha_registro"].map(lambda v: None if v is False else v), errors="coerce"))
        hora = pd.to_timedelta(hora_ini_num.map(_hora_float_a_td), errors="coerce")
        con_hora = base.notna() & hora.notna()
        fecha = base.copy()
        fecha[con_hora] = base[con_hora] + hora[con_hora]
        sin_hora = base.notna() & ~con_hora & hora_ini_dt.notna()
        fecha[sin_hora] = hora_ini_dt[sin_hora]
        # si la fecha de cirugía viene vacía pero hay hora de inicio completa, ésa manda
        fecha = fecha.where(fecha.notna(), hora_ini_dt)
        df["fecha"] = fecha.where(fecha.notna(), df["fecha"])
    # duración en minutos: horas decimales de anestesia (cruza medianoche) o datetimes de inicio/fin
    dur = (hora_fin_num - hora_ini_num) * 60.0
    dur = dur.where(dur >= 0, dur + 24 * 60)
    dur = dur.where((hora_ini_num > 0) & (hora_fin_num > 0) & (dur > 0) & (dur < 24 * 60))
    dur_dt = ((hora_fin_dt - hora_ini_dt).dt.total_seconds() / 60.0)
    dur_dt = dur_dt.where((dur_dt > 0) & (dur_dt < 24 * 60))
    dur = dur.where(dur.notna(), dur_dt)
    if dur.notna().any():
        df["duracion_min"] = dur.where(dur.notna(), pd.to_numeric(df.get("duracion_min"), errors="coerce") if "duracion_min" in df.columns else None)
    elif "f_duracion_min" in df.columns:
        df["duracion_min"] = pd.to_numeric(df["f_duracion_min"], errors="coerce")
    # actores y lugar (la línea gana si ya los trae)
    for logico, origen in (("hospital", "f_hospital"), ("medico", "f_medico"), ("auxiliar", "f_empleado"), ("subalmacen", "f_ubicacion"),
                           ("quirofano", "f_quirofano"), ("tipo_cirugia", "f_tipo_cirugia"), ("especialidad", "f_especialidad"),
                           ("tipo_evento", "f_tipo_evento"), ("turno", "f_turno"), ("estado_folio", "f_estado"), ("cirujano", "f_cirujano"),
                           ("paquete", "f_paquete")):
        if origen in df.columns:
            nuevo = df[origen].map(lambda v: "" if v in (None, False) else str(v))
            if logico in df.columns:
                actual = df[logico].map(lambda v: "" if v in (None, False) else str(v))
                df[logico] = actual.where(actual != "", nuevo)
            else:
                df[logico] = nuevo
            if f"{origen}_id" in df.columns:
                if f"{logico}_id" in df.columns:
                    df[f"{logico}_id"] = df[f"{logico}_id"].where(df[f"{logico}_id"].notna(), df[f"{origen}_id"])
                else:
                    df[f"{logico}_id"] = df[f"{origen}_id"]
    if "f_medico" not in df.columns and "f_cirujano" in df.columns and "medico" not in df.columns:
        df["medico"] = df["cirujano"]; df["medico_id"] = df.get("cirujano_id")
    if "subalmacen" in df.columns and ("ubicacion" not in df.columns or (df["ubicacion"].astype(str) == "").all()):
        df["ubicacion"] = df["subalmacen"]
        if "subalmacen_id" in df.columns:
            df["ubicacion_id"] = df["subalmacen_id"]
    if "f_nombre" in df.columns:
        df["folio"] = df["f_nombre"].map(lambda v: "" if v in (None, False) else str(v)).where(lambda s: s != "", df.get("folio", ""))
    if "f_picking_consumo_id" in df.columns:
        df["picking_consumo_id"] = df["f_picking_consumo_id"]
    if "f_importe_paquete" in df.columns:
        df["importe_paquete"] = pd.to_numeric(df["f_importe_paquete"], errors="coerce")
    return df.drop(columns=[c for c in df.columns if c.startswith("f_")])


def _unir_lotes_de_consumo(df: pd.DataFrame, cli: OdooClient, memoria: dict | None = None) -> pd.DataFrame:
    """El lote consumido vive en el movimiento de inventario del folio (picking de consumo real): se une por
    (picking, producto). Si el modelo ya trae lote en la línea, no se toca. Con memoria local, sólo se releen las
    transferencias nuevas o modificadas."""
    if "picking_consumo_id" not in df.columns or ("lote" in df.columns and (df["lote"].astype(str) != "").any()):
        return df
    pids = sorted({int(x) for x in df["picking_consumo_id"].dropna().tolist()})
    if not pids or len(pids) > 40_000:
        return df
    try:
        campos = set(cli.fields_get("stock.move.line").keys())
        cols = [c for c in ("picking_id", "product_id", "lot_id", "expiration_date", "quantity") if c in campos]
        if "lot_id" not in cols:
            return df
        filas = None
        if memoria is not None:
            try:
                filas, det = mc.leer_lotes(cli, memoria, pids, cols)
                memoria.setdefault("_detalle", {})["lotes"] = det
            except Exception as e:  # noqa: BLE001
                db.log("warn", "odoo", "Memoria de consumo: falló la lectura incremental de lotes; se leen completos", str(e))
                filas = None
        if filas is None:
            filas = cli.search_read_por_ids("stock.move.line", pids, cols, dominio_extra=[["lot_id", "!=", False]], campo="picking_id")
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer los lotes de los movimientos de consumo", str(e))
        return df
    if not filas:
        return df
    lotes: dict[tuple[int, int], tuple[str, Any]] = {}
    for f in filas:
        pk, _ = _m2o(f.get("picking_id")); pr, _ = _m2o(f.get("product_id")); _, lname = _m2o(f.get("lot_id"))
        if pk and pr and (pk, pr) not in lotes:
            lotes[(pk, pr)] = (lname, f.get("expiration_date") or None)
    claves = list(zip(df["picking_consumo_id"].map(lambda v: int(v) if pd.notna(v) else None),
                      df["producto_id"].map(lambda v: int(v) if pd.notna(v) else None)))
    df["lote"] = [lotes.get(k, ("", None))[0] for k in claves]
    df["caducidad"] = [lotes.get(k, ("", None))[1] for k in claves]
    return df


def _valorar_a_costo(df: pd.DataFrame, cli: OdooClient) -> pd.DataFrame:
    """Sin importe por línea (el CB Ticket se factura por paquete), el importe en riesgo se estima a costo estándar del
    producto: cantidad × costo, en la unidad base. Queda marcado como estimado."""
    if "importe" in df.columns and pd.to_numeric(df["importe"], errors="coerce").fillna(0).abs().sum() > 0:
        df["importe_estimado"] = False
        return df
    df["importe"] = 0.0
    df["importe_estimado"] = False
    try:
        ids = sorted({int(x) for x in df["producto_id"].dropna().tolist()})
        costos: dict[int, float] = {}
        for p in cli.search_read_por_ids("product.product", ids, ["standard_price"], bloque=500):
            costos[int(p["id"])] = float(p.get("standard_price") or 0.0)
        if costos:
            c = df["producto_id"].map(lambda v: costos.get(int(v), 0.0) if pd.notna(v) else 0.0)
            df["importe"] = (pd.to_numeric(df["cantidad"], errors="coerce").fillna(0.0) * c).round(2)
            df["importe_estimado"] = c > 0
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudo valorar el consumo a costo estándar", str(e))
    return df


def _consumo_respaldo(cli: OdooClient, d: str, h: str, productos: list[int] | None,
                      almacenes_ids: list[int] | None, tope: int) -> pd.DataFrame:
    """Demanda real a partir de movimientos de inventario ya validados."""
    dominio: list = [
        ["state", "=", "done"], ["date", ">=", d], ["date", "<=", h],
        ["location_id.usage", "=", "internal"],
        ["location_dest_id.usage", "in", ["customer", "production", "inventory", "transit"]],
    ]
    if productos:
        dominio.append(["product_id", "in", productos])
    cols = ["id", "date", "product_id", "qty_done", "quantity", "product_uom_id", "lot_id",
            "location_id", "location_dest_id", "picking_id", "move_id", "reference",
            "company_id", "state"]
    disponibles = set(cli.fields_get("stock.move.line").keys())
    cols = [c for c in cols if c in disponibles]
    rows = cli.search_read_all("stock.move.line", dominio, cols, tope=tope, orden="date")
    df = pd.DataFrame(rows)
    if df.empty:
        return _df_consumo_vacio()

    df["cantidad"] = pd.to_numeric(df.get("quantity", df.get("qty_done", 0)), errors="coerce").fillna(0.0)
    df["fecha"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.rename(columns={"product_id": "producto", "product_uom_id": "unidad", "lot_id": "lote",
                            "location_id": "ubicacion", "location_dest_id": "ubicacion_destino",
                            "picking_id": "folio", "company_id": "compania"})
    df = _expandir(df, ["producto", "unidad", "lote", "ubicacion", "ubicacion_destino", "folio", "compania"])
    df["hospital"] = df.get("ubicacion_destino", "")
    df["hospital_id"] = df.get("ubicacion_destino_id")
    df["medico"] = ""
    df["medico_id"] = None
    # Importe estimado a costo estándar del producto (sólo cuando la línea está en la unidad base del producto); así el
    # «impacto» de hallazgos y propuestas no queda en $0 en modo de respaldo. Queda marcado como estimado.
    df["importe"] = 0.0
    df["importe_estimado"] = False
    try:
        ids = sorted({int(x) for x in df.get("producto_id", pd.Series(dtype=float)).dropna().tolist()})
        if ids:
            costos = {}
            for i in range(0, len(ids), 500):
                for p in cli.search_read("product.product", [["id", "in", ids[i:i + 500]]], ["standard_price", "uom_id"], limite=0):
                    costos[int(p["id"])] = (float(p.get("standard_price") or 0.0),
                                            int(p["uom_id"][0]) if isinstance(p.get("uom_id"), (list, tuple)) else None)
            if costos:
                uom_linea = df.get("unidad_id")
                for idx in df.index:
                    pid = df.at[idx, "producto_id"]
                    if pid is None or pd.isna(pid):
                        continue
                    costo, uom_base = costos.get(int(pid), (0.0, None))
                    ul = uom_linea.at[idx] if uom_linea is not None else None
                    if costo > 0 and (uom_base is None or ul is None or pd.isna(ul) or int(ul) == uom_base):
                        df.at[idx, "importe"] = round(float(df.at[idx, "cantidad"]) * costo, 2)
                        df.at[idx, "importe_estimado"] = True
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudo valorar el consumo de respaldo a costo estándar", str(e))
    df["origen_datos"] = "stock.move.line"
    return _completar_consumo(df)


def _df_consumo_vacio() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "id", "fecha", "folio", "folio_id", "hospital", "hospital_id", "medico", "medico_id",
        "almacen", "ubicacion", "ubicacion_id", "producto", "producto_id", "lote", "caducidad",
        "cantidad", "unidad", "importe", "origen_datos"])


def _completar_consumo(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("folio", "hospital", "medico", "almacen", "ubicacion", "producto", "lote", "unidad"):
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str)
    for c in ("producto_id", "hospital_id", "medico_id", "ubicacion_id", "folio_id"):
        if c not in df.columns:
            df[c] = None
    if "almacen" in df.columns and (df["almacen"] == "").all() and "ubicacion" in df.columns:
        # "CEDIS/Stock" → "CEDIS"
        df["almacen"] = df["ubicacion"].str.split("/").str[0]
    if "caducidad" not in df.columns:
        df["caducidad"] = None
    for c in ("auxiliar", "subalmacen"):
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str)
    for c in ("peso_inicial", "peso_final", "consumo_peso", "duracion_min", "consumo_ml", "cantidad_max", "cantidad_paquete"):
        if c not in df.columns:
            df[c] = None
        df[c] = pd.to_numeric(df[c].map(lambda v: None if v is False else v), errors="coerce")
    for c in ("quirofano", "tipo_cirugia", "tipo_evento", "turno", "estado_folio", "cirujano", "paquete", "tipo_linea"):
        if c in df.columns:
            df[c] = df[c].map(lambda v: "" if v in (None, False) else str(v))
    if "pesable" in df.columns:
        df["pesable"] = df["pesable"].map(lambda v: bool(v) if v not in (None, "") else False)
    if "importe_estimado" not in df.columns:
        df["importe_estimado"] = False
    if "caducidad" in df.columns:
        df["caducidad"] = pd.to_datetime(df["caducidad"].map(lambda v: None if v is False else v), errors="coerce")
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["dia"] = df["fecha"].dt.date
    df = df[df["cantidad"] != 0]
    return df.reset_index(drop=True)


# ── existencias ─────────────────────────────────────────────────────────────
def existencias(productos: list[int] | None = None, cli: OdooClient | None = None,
                solo_internas: bool = True) -> pd.DataFrame:
    cli = cli or get_client()
    mp = schema.mapa("existencias")
    dominio: list = []
    if solo_internas:
        dominio.append(["location_id.usage", "=", "internal"])
    if productos and mp.get("producto"):
        dominio.append([mp["producto"], "in", productos])
    modelo_ex = schema.modelo("existencias") or "stock.quant"
    if not mp:
        mp = {k: v[0][0] for k, v in schema.CANDIDATOS["existencias"]["campos"].items()}
    cols = list(dict.fromkeys([v for v in mp.values() if v] + ["id"]))
    try:
        disponibles = set(cli.fields_get(modelo_ex).keys())
        cols = [c for c in cols if c in disponibles]
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", f"No se pudieron leer los campos de {modelo_ex}; se usan los estándar", str(e))
    rows = cli.search_read_all(modelo_ex, dominio, cols, tope=100_000)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["producto_id", "producto", "almacen", "ubicacion",
                                     "cantidad", "disponible", "lote", "caducidad"])
    df = df.rename(columns={v: k for k, v in mp.items() if v in df.columns})
    df = _expandir(df, [c for c in ("producto", "ubicacion", "almacen", "lote", "compania")
                        if c in df.columns])
    for c in ("cantidad", "disponible", "reservado"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    if "reservado" not in df.columns:
        df["reservado"] = 0.0
    if "disponible" not in df.columns:
        df["disponible"] = (df["cantidad"] - df["reservado"]).clip(lower=0)
    if "caducidad" in df.columns:
        df["caducidad"] = pd.to_datetime(df["caducidad"].map(lambda v: None if v is False else v), errors="coerce")
        df["caducado"] = df["caducidad"].notna() & (df["caducidad"] < pd.Timestamp.now())
    else:
        df["caducidad"] = pd.NaT
        df["caducado"] = False
    # utilizable = disponible (sin reservas) y sin lotes caducados
    df["utilizable"] = df["disponible"].where(~df["caducado"], 0.0)
    if "almacen" not in df.columns or (df["almacen"] == "").all():
        df["almacen"] = df["ubicacion"].str.split("/").str[0]
    return df


def existencias_resumen(productos: list[int] | None = None,
                        cli: OdooClient | None = None) -> pd.DataFrame:
    df = existencias(productos, cli)
    if df.empty:
        return df
    return (df.groupby(["producto_id", "producto", "almacen"], as_index=False)
              .agg(stock_actual=("cantidad", "sum"), disponible=("disponible", "sum"),
                   reservado=("reservado", "sum"), utilizable=("utilizable", "sum")))


# ── proveedores / lead time ─────────────────────────────────────────────────
def lead_times(productos: list[int] | None = None, cli: OdooClient | None = None) -> dict[int, float]:
    """Días de entrega del PROVEEDOR PRINCIPAL de cada producto (menor secuencia en product.supplierinfo),
    que es el que Odoo usa al reabastecer. Un proveedor alterno más rápido no se toma como referencia."""
    cli = cli or get_client()
    dominio: list = []
    if productos:
        dominio.append(["product_id", "in", productos])
    try:
        rows = cli.search_read("product.supplierinfo", dominio,
                               ["product_id", "product_tmpl_id", "delay", "sequence", "partner_id"], limite=20_000,
                               orden="sequence, id")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, float] = {}
    for r in rows:
        pid, _ = _m2o(r.get("product_id"))
        if not pid:
            pid, _ = _m2o(r.get("product_tmpl_id"))
        if pid and r.get("delay") is not None and pid not in out:
            out[pid] = float(r["delay"])
    return out


_UOM_CACHE: dict[str, tuple[float, dict]] = {}


def catalogo_uom(cli: OdooClient | None = None) -> dict[int, dict]:
    """{id: {"nombre", "factor", "rounding"}} tolerante a la versión de Odoo. En Odoo ≤ 18 ``uom.uom.factor`` es la razón
    respecto a la unidad de referencia (frasco 250 mL → 1/250); en Odoo 19 las unidades son relativas (``relative_factor``
    y ``relative_uom_id``: 1 frasco = 250 mL) y el factor absoluto se reconstruye recorriendo la cadena."""
    import time as _t
    cli = cli or get_client()
    hit = _UOM_CACHE.get("uom")
    if hit and _t.time() - hit[0] < 900:
        return hit[1]
    try:
        campos = set(cli.fields_get("uom.uom").keys())
        cols = ["name"] + [c for c in ("factor", "relative_factor", "relative_uom_id", "rounding") if c in campos]
        rows = cli.search_read("uom.uom", [], cols, limite=5000)
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer las unidades de medida", str(e))
        return {}
    por_id = {int(r["id"]): r for r in rows}
    out: dict[int, dict] = {}

    def factor_abs(uid: int, visto: set | None = None) -> float:
        r = por_id.get(uid) or {}
        if "factor" in r and r.get("factor") not in (None, False):
            return float(r["factor"]) or 1.0
        visto = visto or set()
        padre, _ = _m2o(r.get("relative_uom_id"))
        rf = float(r.get("relative_factor") or 1.0) or 1.0
        if not padre or padre in visto or padre == uid:
            return 1.0 / rf if rf != 1.0 else 1.0
        visto.add(uid)
        return factor_abs(padre, visto) / rf

    for uid, r in por_id.items():
        out[uid] = {"nombre": str(r.get("name") or ""), "factor": factor_abs(uid), "rounding": float(r.get("rounding") or 1.0)}
    _UOM_CACHE["uom"] = (_t.time(), out)
    return out


def unidad_compra_de(producto_id: int, cli: OdooClient | None = None) -> dict:
    """Unidad de compra de un producto: {"uom_id", "uom_base", "nombre", "ratio"} (ratio = unidades base por unidad de compra).
    Odoo ≤ 18: ``product.uom_po_id``. Odoo 19 (sin unidad de compra en el producto): la unidad del proveedor principal
    (``product.supplierinfo.product_uom_id``); si no hay, la unidad base."""
    cli = cli or get_client()
    prod = cli.search_read("product.product", [["id", "=", int(producto_id)]],
                           ["uom_id", "product_tmpl_id"] + (["uom_po_id"] if "uom_po_id" in cli.fields_get("product.product") else []), limite=1)
    if not prod:
        return {}
    uom_base, nombre_base = _m2o(prod[0].get("uom_id"))
    uom_po, nombre_po = _m2o(prod[0].get("uom_po_id")) if prod[0].get("uom_po_id") else (None, "")
    if not uom_po:
        campos_si = cli.fields_get("product.supplierinfo")
        c_uom = next((c for c in ("product_uom_id", "product_uom") if c in campos_si), None)
        if c_uom:
            tmpl, _ = _m2o(prod[0].get("product_tmpl_id"))
            dom = ["|", ["product_id", "=", int(producto_id)], ["product_tmpl_id", "=", tmpl]] if tmpl else [["product_id", "=", int(producto_id)]]
            si = cli.search_read("product.supplierinfo", dom, [c_uom, "sequence"], limite=1, orden="sequence")
            if si and si[0].get(c_uom):
                uom_po, nombre_po = _m2o(si[0][c_uom])
    if not uom_po or uom_po == uom_base:
        return {"uom_id": uom_base, "uom_base": uom_base, "nombre": nombre_base, "ratio": 1.0}
    cat = catalogo_uom(cli)
    fb, fp = (cat.get(uom_base) or {}).get("factor"), (cat.get(uom_po) or {}).get("factor")
    ratio = (fb / fp) if (fb and fp) else None
    return {"uom_id": uom_po, "uom_base": uom_base, "nombre": nombre_po or (cat.get(uom_po) or {}).get("nombre", ""), "ratio": ratio}


def unidades_compra(cli: OdooClient | None = None) -> dict[int, dict]:
    """Unidad de compra y factor de conversión por producto (p. ej. 1 frasco 250 mL = 250 mL), para todos los productos activos.
    Tolerante a Odoo 19 (sin ``uom_po_id``: se usa la unidad del proveedor principal)."""
    cli = cli or get_client()
    try:
        campos_p = cli.fields_get("product.product")
        tiene_po = "uom_po_id" in campos_p
        prods = cli.search_read("product.product", [["active", "=", True]], ["uom_id", "product_tmpl_id"] + (["uom_po_id"] if tiene_po else []), limite=50_000)
        cat = catalogo_uom(cli)
        po_prov: dict = {}
        if not tiene_po:
            campos_si = cli.fields_get("product.supplierinfo")
            c_uom = next((c for c in ("product_uom_id", "product_uom") if c in campos_si), None)
            if c_uom:
                for si in cli.search_read("product.supplierinfo", [], ["product_id", "product_tmpl_id", c_uom, "sequence"], limite=50_000, orden="sequence"):
                    pid_, _ = _m2o(si.get("product_id")); tid_, _ = _m2o(si.get("product_tmpl_id")); u, un = _m2o(si.get(c_uom))
                    if not u:
                        continue
                    if pid_ and ("p", pid_) not in po_prov:
                        po_prov[("p", pid_)] = (u, un)
                    if tid_ and ("t", tid_) not in po_prov:
                        po_prov[("t", tid_)] = (u, un)
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer unidades de compra", str(e))
        return {}
    out: dict[int, dict] = {}
    for p in prods:
        uid, uname = _m2o(p.get("uom_id"))
        if tiene_po:
            pid, pname = _m2o(p.get("uom_po_id"))
        else:
            tid, _ = _m2o(p.get("product_tmpl_id"))
            pid, pname = po_prov.get(("p", int(p["id"]))) or po_prov.get(("t", tid)) or (None, "")
        if not uid or not pid or uid == pid:
            continue
        uname = uname or (cat.get(uid) or {}).get("nombre", ""); pname = pname or (cat.get(pid) or {}).get("nombre", "")
        fu, fp = (cat.get(uid) or {}).get("factor"), (cat.get(pid) or {}).get("factor")
        if not fu or not fp:
            # se conoce la unidad de compra pero NO la conversión: el motor lo señala y bloquea la propuesta afectada
            out[int(p["id"])] = {"unidad_base": uname, "unidad_compra": pname, "ratio": None}
            continue
        ratio = fu / fp                     # unidades base por unidad de compra (250 mL por frasco)
        if ratio > 0:
            out[int(p["id"])] = {"unidad_base": uname, "unidad_compra": pname, "ratio": round(ratio, 6)}
    return out


def precision_uom(cli: OdooClient | None = None) -> dict[str, int]:
    """Decimales permitidos por unidad de medida (a partir de uom.uom.rounding): {"mL": 1, "pz": 0, ...}."""
    cli = cli or get_client()
    out: dict[str, int] = {}
    try:
        for u in catalogo_uom(cli).values():
            r = float(u.get("rounding") or 1.0)
            dec = 0 if r >= 1 else int(round(-math.log10(r)))
            out[str(u.get("nombre") or "")] = max(0, min(dec, 6))
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudo leer la precisión de las unidades", str(e))
    return out


_CAMPO_UOM_CACHE: dict[str, str] = {}


def campo_uom(modelo: str, cli: OdooClient | None = None) -> str:
    """Nombre real del campo «unidad de medida» en un modelo de líneas: Odoo 19 renombró ``product_uom`` → ``product_uom_id``
    en purchase.order.line (stock.move conserva ``product_uom``). Se comprueba una vez por modelo con fields_get."""
    if modelo in _CAMPO_UOM_CACHE:
        return _CAMPO_UOM_CACHE[modelo]
    cli = cli or get_client()
    nombre = "product_uom"
    try:
        campos = cli.fields_get(modelo)
        for c in ("product_uom_id", "product_uom", "uom_id"):
            if c in campos:
                nombre = c
                break
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", f"No se pudo determinar el campo de unidad en {modelo}; uso product_uom", str(e))
    _CAMPO_UOM_CACHE[modelo] = nombre
    return nombre


def _factores_uom(cli: OdooClient) -> dict[int, float]:
    return {uid: float(v.get("factor") or 1.0) for uid, v in catalogo_uom(cli).items()}


def _a_unidad_base(qty: float, uom_origen: int | None, uom_base: int | None, factores: dict[int, float]) -> float:
    """Convierte una cantidad de la unidad del documento a la unidad base del producto (regla de Odoo:
    qty / factor_origen * factor_destino). Si falta información, devuelve la cantidad sin convertir."""
    if not uom_origen or not uom_base or uom_origen == uom_base:
        return qty
    fo, fb = factores.get(uom_origen), factores.get(uom_base)
    if not fo or not fb:
        return qty
    return qty / fo * fb


def abastecimiento_pendiente(cli: OdooClient | None = None) -> pd.DataFrame:
    """Compras confirmadas por recibir y transferencias INTERNAS pendientes, cada entrada contada una sola vez y
    en la UNIDAD BASE del producto (una compra en frascos se convierte a mL).

    • Compras: líneas de purchase.order confirmadas con cantidad pendiente de recibir (product_qty − qty_received en la
      unidad de la compra → unidad base). Su recepción (stock.move desde proveedor) NO se cuenta aparte.
    • Transferencias: stock.move pendientes con origen Y destino internos (product_qty ya está en unidad base).
    Columnas: tipo, ref, proveedor, producto_id, producto, cantidad (unidad base), cantidad_doc, unidad_doc,
    ubicacion_destino, ubicacion_origen, fecha_prevista, confirmada, retrasada."""
    cli = cli or get_client()
    filas: list[dict] = []
    hoy = pd.Timestamp.now()
    factores = _factores_uom(cli)
    uom_base: dict[int, int] = {}
    try:
        for pr in cli.search_read("product.product", [["active", "=", True]], ["uom_id"], limite=50_000):
            uid, _ = _m2o(pr.get("uom_id"))
            if uid:
                uom_base[int(pr["id"])] = uid
    except Exception:  # noqa: BLE001
        pass
    # ── compras confirmadas ──
    try:
        c_uom_pol = campo_uom("purchase.order.line", cli)
        lineas = cli.search_read("purchase.order.line", [["state", "in", ["purchase", "done"]]],
                                 ["order_id", "product_id", "product_qty", "qty_received", "date_planned", "state", c_uom_pol],
                                 limite=20_000)
        ordenes = {}
        ids = list({_m2o(l.get("order_id"))[0] for l in lineas if l.get("order_id")})
        tipos = {}
        if ids:
            for o in cli.search_read("purchase.order", [["id", "in", ids]], ["name", "picking_type_id", "date_planned", "partner_id", "state"], limite=5000):
                ordenes[o["id"]] = o
            tipos = {t["id"]: t for t in cli.search_read("stock.picking.type", [["code", "=", "incoming"]],
                                                          ["default_location_dest_id", "warehouse_id"], limite=500)}
        for l in lineas:
            pendiente_doc = float(l.get("product_qty") or 0) - float(l.get("qty_received") or 0)
            if pendiente_doc <= 0:
                continue
            oid, oname = _m2o(l.get("order_id"))
            o = ordenes.get(oid, {})
            if o.get("state") in ("cancel", "draft", "sent", "to approve"):
                continue
            tid, _ = _m2o(o.get("picking_type_id"))
            dest = tipos.get(tid, {}).get("default_location_dest_id") if tid else None
            fecha = pd.to_datetime(l.get("date_planned") or o.get("date_planned") or None, errors="coerce")
            pid, pname = _m2o(l.get("product_id"))
            uom_doc, uom_doc_nombre = _m2o(l.get(c_uom_pol))
            cantidad = _a_unidad_base(pendiente_doc, uom_doc, uom_base.get(pid), factores)
            filas.append({"tipo": "compra", "ref": oname, "proveedor": _m2o(o.get("partner_id"))[1], "producto_id": pid, "producto": pname,
                          "cantidad": round(cantidad, 3), "cantidad_doc": pendiente_doc, "unidad_doc": uom_doc_nombre,
                          "ubicacion_destino": _m2o(dest)[1] if dest else "", "ubicacion_origen": _m2o(o.get("partner_id"))[1],
                          "fecha_prevista": fecha, "confirmada": True,
                          "retrasada": bool(pd.notna(fecha) and fecha < hoy)})
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer compras pendientes", str(e))
    # ── transferencias internas pendientes (origen y destino internos: las recepciones de proveedor ya van en compras) ──
    try:
        c_uom_mv = campo_uom("stock.move", cli)
        moves = cli.search_read("stock.move",
                                [["state", "in", ["draft", "waiting", "confirmed", "partially_available", "assigned"]],
                                 ["location_dest_id.usage", "=", "internal"], ["location_id.usage", "=", "internal"]],
                                ["product_id", "product_uom_qty", "product_qty", "quantity", c_uom_mv, "location_id", "location_dest_id", "date",
                                 "picking_id", "reference", "state"], limite=20_000)
        for m in moves:
            pid, pname = _m2o(m.get("product_id"))
            fecha = pd.to_datetime(m.get("date") or None, errors="coerce")
            uom_doc, uom_doc_nombre = _m2o(m.get(c_uom_mv))
            qty_doc = float(m.get("product_uom_qty") or 0)
            cantidad = float(m.get("product_qty") or 0) or _a_unidad_base(qty_doc, uom_doc, uom_base.get(pid), factores)
            filas.append({"tipo": "transferencia", "ref": m.get("reference") or _m2o(m.get("picking_id"))[1], "proveedor": "",
                          "producto_id": pid, "producto": pname, "cantidad": round(cantidad, 3), "cantidad_doc": qty_doc,
                          "unidad_doc": uom_doc_nombre, "ubicacion_destino": _m2o(m.get("location_dest_id"))[1],
                          "ubicacion_origen": _m2o(m.get("location_id"))[1], "fecha_prevista": fecha,
                          "confirmada": m.get("state") in ("assigned", "confirmed", "partially_available", "waiting"),
                          "retrasada": bool(pd.notna(fecha) and fecha < hoy - pd.Timedelta(days=1))})
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer transferencias pendientes", str(e))
    cols = ["tipo", "ref", "proveedor", "producto_id", "producto", "cantidad", "cantidad_doc", "unidad_doc", "ubicacion_destino",
            "ubicacion_origen", "fecha_prevista", "confirmada", "retrasada"]
    return pd.DataFrame(filas, columns=cols)


def folios_programados(dias: int = 30, cli: OdooClient | None = None) -> pd.DataFrame:
    """Cirugías/folios con fecha futura (programados) por hospital y día, si el modelo de folio existe."""
    cli = cli or get_client()
    ent = schema.cargar()["entidades"].get("folio", {})
    modelo_folio, mp = ent.get("modelo"), ent.get("campos", {})
    if not modelo_folio or not mp.get("fecha"):
        return pd.DataFrame(columns=["hospital", "hospital_id", "dia", "folios"])
    ahora = datetime.now()
    cols = list(dict.fromkeys([v for k, v in mp.items() if v and k in ("nombre", "fecha", "estado", "hospital", "tipo_cirugia", "duracion_min",
                                                                        "tipo_evento", "quirofano")] + ["id"]))
    tipo_fecha = ((ent.get("detalle") or {}).get("fecha") or {}).get("tipo", "")
    desde_s, hasta_s = ((ahora.strftime("%Y-%m-%d"), (ahora + timedelta(days=dias)).strftime("%Y-%m-%d")) if tipo_fecha == "date"
                        else (ahora.strftime(FMT), (ahora + timedelta(days=dias)).strftime(FMT)))
    try:
        rows = cli.search_read_all(modelo_folio, [[mp["fecha"], ">", desde_s], [mp["fecha"], "<=", hasta_s]], cols, tope=20_000)
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", "No se pudieron leer folios programados", str(e))
        return pd.DataFrame(columns=["hospital", "hospital_id", "dia", "folios"])
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["hospital", "hospital_id", "dia", "folios"])
    df = df.rename(columns={v: k for k, v in mp.items() if v in df.columns})
    df = _expandir(df, [c for c in ("hospital", "quirofano") if c in df.columns])
    if "estado" in df.columns:
        # programados = aún no realizados (borrador/confirmado); realizados, cerrados, facturados o cancelados no cuentan
        df = df[~df["estado"].isin(["cancel", "cancelled", "cancelado", "done", "closed", "invoiced"])]
    df["dia"] = pd.to_datetime(df["fecha"], errors="coerce").dt.date
    if "hospital" not in df.columns:
        df["hospital"], df["hospital_id"] = "", None
    return df.groupby(["hospital", "hospital_id", "dia"], dropna=False).size().reset_index(name="folios")


# ── folios ──────────────────────────────────────────────────────────────────
def folios(desde: str | None = None, hasta: str | None = None, dias: int = 90,
           cli: OdooClient | None = None) -> pd.DataFrame:
    cli = cli or get_client()
    ent = schema.cargar()["entidades"].get("folio", {})
    modelo_folio, mp = ent.get("modelo"), ent.get("campos", {})
    if not modelo_folio or not mp:
        return pd.DataFrame()
    d, h = _rango(desde, hasta, dias)
    campo_fecha = mp.get("fecha", "create_date")
    if ((ent.get("detalle") or {}).get("fecha") or {}).get("tipo") == "date":
        d, h = d[:10], h[:10]
    # minimización de datos: la identidad del paciente nunca sale de Odoo (no la necesitan los agentes, el LLM ni los Excel)
    cols = sin_paciente(list(dict.fromkeys([v for k, v in mp.items() if v and k not in ("paciente",)] + ["id"])))
    try:
        rows = cli.search_read_all(modelo_folio, [[campo_fecha, ">=", d], [campo_fecha, "<=", h]],
                                   cols, tope=50_000)
    except Exception as e:  # noqa: BLE001
        db.log("warn", "odoo", f"No se pudieron leer folios de {modelo_folio}", str(e))
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={v: k for k, v in mp.items() if v in df.columns})
    df = _expandir(df, [c for c in ("hospital", "medico", "almacen", "empleado", "compania")
                        if c in df.columns])
    df["fecha"] = pd.to_datetime(df.get("fecha"), errors="coerce")
    return df


# ── panorama general (para el dashboard y el copiloto) ──────────────────────
def panorama(dias: int = 30, cli: OdooClient | None = None) -> dict:
    cli = cli or get_client()
    try:
        df = consumo(dias=dias, cli=cli, tope=60_000)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if df.empty:
        return {"ok": True, "dias": dias, "lineas": 0, "mensaje": "Sin consumo en el periodo."}
    return {
        "ok": True,
        "dias": dias,
        "lineas": int(len(df)),
        "folios": int(df["folio"].nunique()),
        "productos": int(df["producto_id"].nunique()),
        "hospitales": int(df["hospital"].nunique()),
        "cantidad_total": round(float(df["cantidad"].sum()), 2),
        "importe_total": round(float(df["importe"].sum()), 2),
        "origen_datos": df["origen_datos"].iloc[0],
        "top_productos": (df.groupby("producto")["cantidad"].sum()
                            .sort_values(ascending=False).head(10).round(2).to_dict()),
        "top_hospitales": (df.groupby("hospital")["cantidad"].sum()
                             .sort_values(ascending=False).head(10).round(2).to_dict()),
    }


# ── facturación y cobranza (análisis contable básico para el copiloto) ──────────────────────────────────────────────────
def facturacion(desde: str | None = None, hasta: str | None = None, dias: int = 90, tipo: str = "cliente",
                cli: OdooClient | None = None, tope: int = 50_000) -> pd.DataFrame:
    """Facturas de cliente (o de proveedor) contabilizadas o en borrador: cliente, fechas, importes, saldo pendiente,
    estado de pago y vencimiento. Sólo lectura; no incluye nómina ni asientos manuales."""
    cli = cli or get_client()
    d, h = _rango(desde, hasta, dias)
    tipos = ["out_invoice", "out_refund"] if tipo == "cliente" else ["in_invoice", "in_refund"]
    campos = set(cli.fields_get("account.move").keys())
    cols = [c for c in ("name", "partner_id", "invoice_date", "invoice_date_due", "amount_untaxed", "amount_total", "amount_residual",
                        "state", "payment_state", "move_type", "currency_id", "invoice_origin", "ref", "company_id") if c in campos]
    rows = cli.search_read_all("account.move", [["move_type", "in", tipos], ["invoice_date", ">=", d[:10]], ["invoice_date", "<=", h[:10]],
                                                ["state", "!=", "cancel"]], cols, tope=tope)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["factura", "cliente", "fecha", "vencimiento", "subtotal", "total", "saldo", "estado", "estado_pago", "tipo", "vencida"])
    df = df.rename(columns={"name": "factura", "partner_id": "cliente", "invoice_date": "fecha", "invoice_date_due": "vencimiento",
                            "amount_untaxed": "subtotal", "amount_total": "total", "amount_residual": "saldo", "state": "estado",
                            "payment_state": "estado_pago", "move_type": "tipo", "invoice_origin": "origen", "company_id": "compania"})
    df = _expandir(df, [c for c in ("cliente", "currency_id", "compania") if c in df.columns])
    for c in ("subtotal", "total", "saldo"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
    for c in ("estado", "estado_pago", "tipo"):
        if c not in df.columns:
            df[c] = ""
    df["fecha"] = pd.to_datetime(df.get("fecha").map(lambda v: None if v is False else v), errors="coerce")
    if "vencimiento" in df.columns:
        df["vencimiento"] = pd.to_datetime(df["vencimiento"].map(lambda v: None if v is False else v), errors="coerce")
        df["vencida"] = (df["vencimiento"] < pd.Timestamp.now().normalize()) & (df.get("saldo", 0) > 0) & (df.get("estado") == "posted")
    else:
        df["vencida"] = False
    signo = df["tipo"].map(lambda t: -1.0 if str(t).endswith("refund") else 1.0)
    for c in ("subtotal", "total", "saldo"):
        df[c] = df[c] * signo
    df["mes"] = df["fecha"].dt.to_period("M").astype(str)
    return df


def facturacion_lineas(desde: str | None = None, hasta: str | None = None, dias: int = 90, tipo: str = "cliente",
                       cli: OdooClient | None = None, tope: int = 100_000) -> pd.DataFrame:
    """Líneas de factura con producto, cantidad e importe (qué se facturó, a quién y cuánto), para análisis por producto/unidad."""
    cli = cli or get_client()
    d, h = _rango(desde, hasta, dias)
    tipos = ["out_invoice", "out_refund"] if tipo == "cliente" else ["in_invoice", "in_refund"]
    campos = set(cli.fields_get("account.move.line").keys())
    cols = [c for c in ("move_id", "partner_id", "product_id", "quantity", "price_unit", "price_subtotal", "date", "move_type", "display_type",
                        "product_uom_id", "name") if c in campos]
    dominio = [["move_id.move_type", "in", tipos], ["move_id.state", "!=", "cancel"], ["date", ">=", d[:10]], ["date", "<=", h[:10]],
               ["product_id", "!=", False]]
    if "display_type" in campos:
        dominio.append(["display_type", "=", "product"])
    rows = cli.search_read_all("account.move.line", dominio, cols, tope=tope)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["factura", "cliente", "producto", "cantidad", "precio_unitario", "importe", "fecha"])
    df = df.rename(columns={"move_id": "factura", "partner_id": "cliente", "product_id": "producto", "quantity": "cantidad",
                            "price_unit": "precio_unitario", "price_subtotal": "importe", "product_uom_id": "unidad"})
    df = _expandir(df, [c for c in ("factura", "cliente", "producto", "unidad") if c in df.columns])
    for c in ("cantidad", "precio_unitario", "importe"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce").fillna(0.0)
    df["fecha"] = pd.to_datetime(df["date"], errors="coerce")
    df["mes"] = df["fecha"].dt.to_period("M").astype(str)
    return df


def resumen_facturacion(df: pd.DataFrame) -> dict:
    """Facturado, cobrado, por cobrar y vencido; top clientes y meses. Todo a partir de las facturas leídas."""
    if df.empty:
        return {"facturas": 0, "facturado": 0.0, "cobrado": 0.0, "por_cobrar": 0.0, "vencido": 0.0}
    contab = df[df["estado"] == "posted"] if "estado" in df.columns else df
    facturado = float(contab["total"].sum()); por_cobrar = float(contab["saldo"].sum())
    vencido = float(contab.loc[contab["vencida"], "saldo"].sum()) if "vencida" in contab.columns else 0.0
    por_cliente = (contab.groupby("cliente")[["total", "saldo"]].sum().sort_values("total", ascending=False).head(10).round(2).reset_index()
                   .to_dict(orient="records"))
    por_mes = contab.groupby("mes")[["total", "saldo"]].sum().round(2).reset_index().to_dict(orient="records")
    return {"facturas": int(len(contab)), "borrador": int((df.get("estado") == "draft").sum()) if "estado" in df.columns else 0,
            "facturado": round(facturado, 2), "cobrado": round(facturado - por_cobrar, 2), "por_cobrar": round(por_cobrar, 2),
            "vencido": round(vencido, 2), "facturas_vencidas": int(contab["vencida"].sum()) if "vencida" in contab.columns else 0,
            "por_cliente": por_cliente, "por_mes": por_mes}
