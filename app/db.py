"""Capa de persistencia (SQLite sobre el disco persistente de Render).

Todo lo que debe sobrevivir a un redeploy vive aquí:
bitácora de ejecuciones, hallazgos, pronósticos, retroalimentación del usuario
(el "aprendizaje"), conversaciones, consumo de tokens y el mapeo de campos Odoo.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import settings

_LOCK = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario TEXT UNIQUE NOT NULL,
    nombre TEXT,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    rol TEXT NOT NULL DEFAULT 'consulta',      -- admin | operacion | consulta
    activo INTEGER NOT NULL DEFAULT 1,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    token TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
    expira_en TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ajustes (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corridas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agente TEXT NOT NULL,                      -- consumo | demanda | copiloto
    estado TEXT NOT NULL,                      -- ejecutando | ok | error
    disparo TEXT NOT NULL DEFAULT 'manual',    -- manual | programado | api
    usuario TEXT,
    parametros TEXT,
    registros INTEGER DEFAULT 0,
    hallazgos INTEGER DEFAULT 0,
    resumen TEXT,
    error TEXT,
    inicio TEXT NOT NULL,
    fin TEXT,
    duracion_seg REAL
);

CREATE TABLE IF NOT EXISTS anomalias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrida_id INTEGER REFERENCES corridas(id),
    fecha TEXT,
    folio TEXT,
    hospital TEXT,
    unidad_medica TEXT,
    medico TEXT,
    almacen TEXT,
    producto_id INTEGER,
    producto TEXT,
    lote TEXT,
    cantidad REAL,
    unidad TEXT,
    esperado REAL,
    desviacion REAL,
    importe_riesgo REAL,
    score REAL,
    severidad TEXT,                            -- critica | alta | media | baja
    metodos TEXT,                              -- json: métodos que dispararon
    motivos TEXT,                              -- json: motivos legibles
    explicacion TEXT,
    huella TEXT,                               -- hash para deduplicar
    estado TEXT NOT NULL DEFAULT 'nueva',      -- nueva | revisada | justificada | confirmada | descartada
    feedback TEXT,
    feedback_usuario TEXT,
    feedback_en TEXT,
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_anom_huella ON anomalias(huella);
CREATE INDEX IF NOT EXISTS ix_anom_corrida ON anomalias(corrida_id);

CREATE TABLE IF NOT EXISTS pronosticos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrida_id INTEGER REFERENCES corridas(id),
    producto_id INTEGER,
    producto TEXT,
    almacen TEXT,
    metodo TEXT,
    mape REAL,
    fecha TEXT,
    pronostico REAL,
    inferior REAL,
    superior REAL,
    demanda_proyectada REAL,
    demanda_agenda REAL,
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pron_corrida ON pronosticos(corrida_id);

CREATE TABLE IF NOT EXISTS resurtido (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrida_id INTEGER REFERENCES corridas(id),
    producto_id INTEGER,
    producto TEXT,
    almacen TEXT,
    unidad TEXT,
    stock_actual REAL,
    demanda_diaria REAL,
    sigma_diaria REAL,
    lead_time_dias REAL,
    stock_seguridad REAL,
    punto_reorden REAL,
    demanda_horizonte REAL,
    sugerido REAL,
    dias_cobertura REAL,
    criticidad TEXT,                           -- desabasto | critico | reordenar | ok | exceso
    metodo TEXT,
    mape REAL,
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_resur_corrida ON resurtido(corrida_id);

CREATE TABLE IF NOT EXISTS aprendizaje (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ambito TEXT NOT NULL,                      -- global | producto | hospital | medico | unidad | agente
    clave TEXT NOT NULL DEFAULT '',
    nota TEXT NOT NULL,
    peso REAL NOT NULL DEFAULT 1.0,
    origen TEXT NOT NULL DEFAULT 'usuario',    -- usuario | sistema
    activo INTEGER NOT NULL DEFAULT 1,
    creado_por TEXT,
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_apr_ambito ON aprendizaje(ambito, clave);

CREATE TABLE IF NOT EXISTS aclaraciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caso_id INTEGER REFERENCES casos(id),
    usuario TEXT NOT NULL,
    texto TEXT NOT NULL,
    alcance TEXT NOT NULL DEFAULT 'caso',      -- caso | actor | producto_hospital | hospital
    entidades TEXT,                            -- JSON con las entidades a las que aplica
    fuente TEXT NOT NULL DEFAULT 'chat',       -- chat | formulario | resolucion
    verificada INTEGER NOT NULL DEFAULT 0,     -- 0 = declarada (no verificada), 1 = verificada por operación
    verificada_por TEXT,
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_acl_caso ON aclaraciones(caso_id);

CREATE TABLE IF NOT EXISTS conversaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario TEXT,
    titulo TEXT,
    creado_en TEXT NOT NULL,
    actualizado_en TEXT
);

CREATE TABLE IF NOT EXISTS mensajes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversacion_id INTEGER NOT NULL REFERENCES conversaciones(id),
    rol TEXT NOT NULL,                         -- user | assistant | tool
    contenido TEXT NOT NULL,                   -- json crudo del bloque
    texto TEXT,                                -- versión legible
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON mensajes(conversacion_id);

CREATE TABLE IF NOT EXISTS uso_llm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    periodo TEXT NOT NULL,                     -- YYYY-MM
    usuario TEXT,
    modelo TEXT,
    origen TEXT,                               -- copiloto | agente1 | agente2 | reporte
    tokens_entrada INTEGER DEFAULT 0,
    tokens_salida INTEGER DEFAULT 0,
    costo_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_uso_periodo ON uso_llm(periodo);

CREATE TABLE IF NOT EXISTS reportes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archivo TEXT NOT NULL,
    ruta TEXT NOT NULL,
    tipo TEXT NOT NULL,
    titulo TEXT,
    parametros TEXT,
    filas INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    creado_por TEXT,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bitacora (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    nivel TEXT NOT NULL,                       -- info | warn | error
    origen TEXT,
    evento TEXT NOT NULL,
    detalle TEXT,
    usuario TEXT
);
CREATE INDEX IF NOT EXISTS ix_bit_ts ON bitacora(ts);

CREATE TABLE IF NOT EXISTS perfiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ambito TEXT NOT NULL,                      -- producto | producto_hospital | producto_medico | producto_unidad
    clave TEXT NOT NULL,                       -- identificador compuesto
    etiqueta TEXT,
    n INTEGER DEFAULT 0,
    media REAL, mediana REAL, mad REAL, desv REAL,
    p05 REAL, p25 REAL, p75 REAL, p95 REAL,
    minimo REAL, maximo REAL,
    unidad TEXT,
    estacionalidad TEXT,                       -- json: factor por día de semana
    ajuste_umbral REAL NOT NULL DEFAULT 0,     -- se SUMA al umbral z: + = más tolerante (justificada), − = más estricto (confirmada)
    justificadas INTEGER DEFAULT 0,
    confirmadas INTEGER DEFAULT 0,
    actualizado_en TEXT NOT NULL,
    UNIQUE(ambito, clave)
);
CREATE INDEX IF NOT EXISTS ix_perf_ambito ON perfiles(ambito);

CREATE TABLE IF NOT EXISTS casos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrida_id INTEGER REFERENCES corridas(id),
    agente TEXT NOT NULL,                      -- consumo | demanda | vigilancia
    tipo TEXT NOT NULL,                        -- anomalia | patron | abasto | bascula | vigilancia
    titulo TEXT NOT NULL,
    severidad TEXT,
    entidades TEXT,                            -- json: producto, hospital, auxiliar, medico, lote, folio, ubicacion
    referencias TEXT,                          -- json: ids de anomalías / claves de patrón
    expediente TEXT,                           -- json estructurado (qué pasó, evidencia, hipótesis, conclusión…)
    conclusion TEXT,
    confianza TEXT,                            -- alta | media | baja
    impacto_mxn REAL DEFAULT 0,
    accion_recomendada TEXT,
    responsable TEXT,
    investigado_con TEXT,                      -- claude:<modelo> | determinista
    herramientas TEXT,                         -- json: trazas de investigación
    estado TEXT NOT NULL DEFAULT 'abierto',    -- abierto | en_revision | resuelto | descartado
    resolucion TEXT,
    resuelto_por TEXT,
    resuelto_en TEXT,
    huella TEXT,
    creado_en TEXT NOT NULL,
    actualizado_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_casos_estado ON casos(estado);
CREATE INDEX IF NOT EXISTS ix_casos_huella ON casos(huella);

CREATE TABLE IF NOT EXISTS acciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrida_id INTEGER REFERENCES corridas(id),
    agente TEXT NOT NULL,
    tipo TEXT NOT NULL,                        -- transferencia_interna | solicitud_compra | regla_reabastecimiento | actividad | nota_chatter | alerta
    titulo TEXT NOT NULL,
    motivo TEXT,
    payload TEXT NOT NULL,                     -- json con los datos para Odoo
    impacto TEXT,                              -- json: cantidad, importe estimado, almacenes
    riesgo TEXT NOT NULL DEFAULT 'bajo',       -- bajo | medio | alto
    nivel_requerido INTEGER NOT NULL DEFAULT 2,
    estado TEXT NOT NULL DEFAULT 'propuesta',  -- propuesta | aprobada | ejecutada | rechazada | error | revertida | bloqueada
    odoo_modelo TEXT,
    odoo_id INTEGER,
    odoo_ref TEXT,
    resultado TEXT,
    error TEXT,
    aprobado_por TEXT,
    aprobado_en TEXT,
    ejecutado_en TEXT,
    creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_acc_estado ON acciones(estado);
CREATE INDEX IF NOT EXISTS ix_acc_corrida ON acciones(corrida_id);
"""


# ── conexión ────────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(settings.DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def conn():
    con = _connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def periodo(ts: str | None = None) -> str:
    d = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
    return d.strftime("%Y-%m")


def init_db() -> None:
    """Crea el esquema y el usuario administrador inicial."""
    with _LOCK, conn() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(anomalias)")}
        if "importe_riesgo" not in cols:
            con.execute("ALTER TABLE anomalias ADD COLUMN importe_riesgo REAL")
        cols_p = {r["name"] for r in con.execute("PRAGMA table_info(pronosticos)")}
        if "demanda_proyectada" not in cols_p:
            con.execute("ALTER TABLE pronosticos ADD COLUMN demanda_proyectada REAL")
        if "demanda_agenda" not in cols_p:
            con.execute("ALTER TABLE pronosticos ADD COLUMN demanda_agenda REAL")
        cols_a = {r["name"] for r in con.execute("PRAGMA table_info(acciones)")}
        for col, tipo in (("efecto", "TEXT"), ("estado_odoo", "TEXT"), ("verificado_en", "TEXT"),
                          ("fecha_requerida", "TEXT"), ("verificacion", "TEXT"), ("aprobado_por_2", "TEXT"),
                          ("aprobado_en_2", "TEXT"), ("revalidacion", "TEXT"), ("referencia", "TEXT"),
                          ("version", "INTEGER DEFAULT 1"), ("version_aprobada", "INTEGER"), ("caso_id", "INTEGER"),
                          ("clave", "TEXT"), ("ultima_corrida_id", "INTEGER"), ("unidad", "TEXT"), ("efecto_aprobado", "TEXT"),
                          ("cantidad_aprobada", "REAL"), ("importe_aprobado", "REAL"), ("cantidad_ejecutada", "REAL"),
                          ("importe_ejecutado", "REAL")):
            if col not in cols_a:
                con.execute(f"ALTER TABLE acciones ADD COLUMN {col} {tipo}")
        cols_u = {r["name"] for r in con.execute("PRAGMA table_info(usuarios)")}
        for col, tipo in (("origen", "TEXT DEFAULT 'local'"), ("odoo_uid", "INTEGER"), ("ultimo_sso", "TEXT"), ("ultimo_acceso", "TEXT")):
            if col not in cols_u:
                con.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {tipo}")
        cols_l = {r["name"] for r in con.execute("PRAGMA table_info(uso_llm)")}
        for col in ("tokens_cache_escritura", "tokens_cache_lectura", "tokens_razonamiento"):   # v1.3.11: caché de prompts
            if col not in cols_l:
                con.execute(f"ALTER TABLE uso_llm ADD COLUMN {col} INTEGER DEFAULT 0")
        con.execute("CREATE TABLE IF NOT EXISTS intentos_acceso (clave TEXT NOT NULL, momento REAL NOT NULL)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_intentos ON intentos_acceso(clave, momento)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_acciones_clave ON acciones(clave)")
        # migración: acciones creadas antes de v1.3 no tienen clave → se calcula para que la deduplicación y la caducidad las alcancen
        sin_clave = con.execute("SELECT id, tipo, payload FROM acciones WHERE clave IS NULL OR clave=''").fetchall()
        for r in sin_clave:
            try:
                pl = json.loads(r["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pl = {}
            partes = [r["tipo"], str(pl.get("producto_id", ""))]
            for k in ("origen", "destino", "ubicacion", "lote", "orden", "picking", "modelo", "res_id"):
                if pl.get(k) not in (None, ""):
                    partes.append(f"{k}={pl[k]}")
            if r["tipo"] not in ("transferencia_interna", "solicitud_compra", "regla_reabastecimiento") and len(partes) == 2:
                partes.append("t=" + hashlib.sha1(str(pl.get("titulo_ticket") or pl.get("resumen") or pl.get("cuerpo") or r["id"]).encode()).hexdigest()[:12])
            con.execute("UPDATE acciones SET clave=?, unidad=COALESCE(unidad, ?) WHERE id=?", ("|".join(partes), pl.get("unidad"), r["id"]))
        con.execute("CREATE INDEX IF NOT EXISTS ix_acciones_estado ON acciones(estado)")
        row = con.execute("SELECT COUNT(*) c FROM usuarios").fetchone()
        if row["c"] == 0:
            crear_usuario(
                settings.ADMIN_USER,
                settings.ADMIN_PASSWORD,
                nombre="Ingeniería Cóndor",
                rol="condor",
                _con=con,
            )
    log("info", "sistema", "Base de datos inicializada", str(settings.DB_PATH))


# ── usuarios y sesiones ─────────────────────────────────────────────────────
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def crear_usuario(usuario: str, password: str, nombre: str = "", rol: str = "consulta",
                  _con: sqlite3.Connection | None = None) -> int:
    salt = secrets.token_hex(16)
    sql = ("INSERT INTO usuarios (usuario, nombre, password_hash, salt, rol, creado_en) "
           "VALUES (?,?,?,?,?,?)")
    args = (usuario.strip().lower(), nombre or usuario, _hash(password, salt), salt, rol, now())
    if _con is not None:
        return _con.execute(sql, args).lastrowid
    with conn() as con:
        return con.execute(sql, args).lastrowid


def verificar_credenciales(usuario: str, password: str) -> dict | None:
    with conn() as con:
        u = con.execute("SELECT * FROM usuarios WHERE usuario=? AND activo=1",
                        (usuario.strip().lower(),)).fetchone()
    if not u:
        _hash(password or "", "0" * 32)      # tiempo constante: no revelar si el usuario existe
        return None
    if (u["origen"] or "local") == "odoo":
        return None                           # los usuarios de Odoo entran sólo por SSO
    if secrets.compare_digest(_hash(password, u["salt"]), u["password_hash"]):
        return dict(u)
    return None


# ── límite de intentos de acceso (por usuario y por IP) ─────────────────────
def intentos_recientes(clave: str, ventana_min: int) -> int:
    limite = time.time() - ventana_min * 60
    with conn() as con:
        con.execute("DELETE FROM intentos_acceso WHERE momento < ?", (limite - 3600,))
        return int(con.execute("SELECT COUNT(*) c FROM intentos_acceso WHERE clave=? AND momento>=?", (clave, limite)).fetchone()["c"])


def registrar_intento(clave: str) -> None:
    with conn() as con:
        con.execute("INSERT INTO intentos_acceso (clave, momento) VALUES (?,?)", (clave, time.time()))


def limpiar_intentos(clave: str) -> None:
    with conn() as con:
        con.execute("DELETE FROM intentos_acceso WHERE clave=?", (clave,))


def crear_sesion(usuario_id: int, horas: int = 12) -> str:
    token = secrets.token_urlsafe(32)
    with conn() as con:
        con.execute("INSERT INTO sesiones (token, usuario_id, expira_en, creado_en) VALUES (?,?,?,?)",
                    (token, usuario_id,
                     (datetime.now(timezone.utc) + timedelta(hours=horas)).isoformat(), now()))
    return token


def usuario_por_token(token: str) -> dict | None:
    if not token:
        return None
    with conn() as con:
        r = con.execute(
            "SELECT u.* , s.expira_en FROM sesiones s JOIN usuarios u ON u.id=s.usuario_id "
            "WHERE s.token=? AND u.activo=1", (token,)).fetchone()
    if not r:
        return None
    if datetime.fromisoformat(r["expira_en"]) < datetime.now(timezone.utc):
        cerrar_sesion(token)
        return None
    return dict(r)


def cerrar_sesion(token: str) -> None:
    with conn() as con:
        con.execute("DELETE FROM sesiones WHERE token=?", (token,))


# ── ajustes clave/valor ─────────────────────────────────────────────────────
def set_ajuste(clave: str, valor: Any) -> None:
    with conn() as con:
        con.execute(
            "INSERT INTO ajustes (clave, valor, actualizado_en) VALUES (?,?,?) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, actualizado_en=excluded.actualizado_en",
            (clave, json.dumps(valor, ensure_ascii=False, default=str), now()))


def get_ajuste(clave: str, default: Any = None) -> Any:
    with conn() as con:
        r = con.execute("SELECT valor FROM ajustes WHERE clave=?", (clave,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["valor"])
    except (json.JSONDecodeError, TypeError):
        return default


# ── bitácora ────────────────────────────────────────────────────────────────
def log(nivel: str, origen: str, evento: str, detalle: str = "", usuario: str | None = None) -> None:
    try:
        with conn() as con:
            con.execute("INSERT INTO bitacora (ts, nivel, origen, evento, detalle, usuario) VALUES (?,?,?,?,?,?)",
                        (now(), nivel, origen, evento, (detalle or "")[:4000], usuario))
    except Exception:  # la bitácora nunca debe tumbar la app
        pass


def bitacora(limite: int = 200, nivel: str | None = None) -> list[dict]:
    q = "SELECT * FROM bitacora"
    args: list = []
    if nivel:
        q += " WHERE nivel=?"
        args.append(nivel)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


# ── corridas ────────────────────────────────────────────────────────────────
def iniciar_corrida(agente: str, parametros: dict, disparo: str = "manual",
                    usuario: str | None = None) -> int:
    with conn() as con:
        return con.execute(
            "INSERT INTO corridas (agente, estado, disparo, usuario, parametros, inicio) VALUES (?,?,?,?,?,?)",
            (agente, "ejecutando", disparo, usuario,
             json.dumps(parametros, ensure_ascii=False, default=str), now())).lastrowid


def cerrar_corrida(corrida_id: int, estado: str, registros: int = 0, hallazgos: int = 0,
                   resumen: str = "", error: str = "") -> None:
    with conn() as con:
        ini = con.execute("SELECT inicio FROM corridas WHERE id=?", (corrida_id,)).fetchone()
        dur = None
        if ini:
            dur = (datetime.now(timezone.utc) - datetime.fromisoformat(ini["inicio"])).total_seconds()
        con.execute(
            "UPDATE corridas SET estado=?, registros=?, hallazgos=?, resumen=?, error=?, fin=?, duracion_seg=? "
            "WHERE id=?",
            (estado, registros, hallazgos, resumen[:8000], error[:4000], now(), dur, corrida_id))


def corridas(agente: str | None = None, limite: int = 50) -> list[dict]:
    q = "SELECT * FROM corridas"
    args: list = []
    if agente:
        q += " WHERE agente=?"
        args.append(agente)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


def ultima_corrida(agente: str) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT * FROM corridas WHERE agente=? AND estado='ok' ORDER BY id DESC LIMIT 1",
                        (agente,)).fetchone()
    return dict(r) if r else None


# ── inserciones masivas ─────────────────────────────────────────────────────
def _insert_many(tabla: str, filas: Iterable[dict]) -> int:
    filas = list(filas)
    if not filas:
        return 0
    cols = list(filas[0].keys())
    sql = f"INSERT INTO {tabla} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with conn() as con:
        con.executemany(sql, [tuple(f.get(c) for c in cols) for f in filas])
    return len(filas)


def guardar_anomalias(filas: list[dict]) -> int:
    for f in filas:
        f.setdefault("creado_en", now())
    return _insert_many("anomalias", filas)


def guardar_pronosticos(filas: list[dict]) -> int:
    for f in filas:
        f.setdefault("creado_en", now())
    return _insert_many("pronosticos", filas)


def guardar_resurtido(filas: list[dict]) -> int:
    for f in filas:
        f.setdefault("creado_en", now())
    return _insert_many("resurtido", filas)


def anomalias(corrida_id: int | None = None, estado: str | None = None,
              severidad: str | None = None, limite: int = 1000) -> list[dict]:
    q, args = "SELECT * FROM anomalias WHERE 1=1", []
    if corrida_id:
        q += " AND corrida_id=?"; args.append(corrida_id)
    if estado:
        q += " AND estado=?"; args.append(estado)
    if severidad:
        q += " AND severidad=?"; args.append(severidad)
    q += " ORDER BY score DESC, id DESC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


def resurtido(corrida_id: int | None = None, criticidad: str | None = None,
              limite: int = 2000) -> list[dict]:
    q, args = "SELECT * FROM resurtido WHERE 1=1", []
    if corrida_id:
        q += " AND corrida_id=?"; args.append(corrida_id)
    if criticidad:
        q += " AND criticidad=?"; args.append(criticidad)
    q += " ORDER BY CASE criticidad WHEN 'desabasto' THEN 0 WHEN 'critico' THEN 1 "
    q += "WHEN 'reordenar' THEN 2 WHEN 'ok' THEN 3 ELSE 4 END, dias_cobertura ASC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


def pronosticos(corrida_id: int | None = None, producto_id: int | None = None,
                limite: int = 5000) -> list[dict]:
    q, args = "SELECT * FROM pronosticos WHERE 1=1", []
    if corrida_id:
        q += " AND corrida_id=?"; args.append(corrida_id)
    if producto_id:
        q += " AND producto_id=?"; args.append(producto_id)
    q += " ORDER BY producto, almacen, fecha LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


def huellas_conocidas() -> set[str]:
    with conn() as con:
        return {r["huella"] for r in con.execute("SELECT DISTINCT huella FROM anomalias WHERE huella IS NOT NULL")}


def clasificar_anomalia(anomalia_id: int, estado: str, nota: str = "", usuario: str = "") -> None:
    with conn() as con:
        con.execute("UPDATE anomalias SET estado=?, feedback=?, feedback_usuario=?, feedback_en=? WHERE id=?",
                    (estado, nota, usuario, now(), anomalia_id))


# ── aprendizaje (no se borra) ───────────────────────────────────────────────
def agregar_aprendizaje(ambito: str, clave: str, nota: str, peso: float = 1.0,
                        origen: str = "usuario", usuario: str = "") -> int:
    with conn() as con:
        return con.execute(
            "INSERT INTO aprendizaje (ambito, clave, nota, peso, origen, creado_por, creado_en) "
            "VALUES (?,?,?,?,?,?,?)",
            (ambito, clave or "", nota, peso, origen, usuario, now())).lastrowid


def aprendizaje(ambito: str | None = None, clave: str | None = None, limite: int = 500) -> list[dict]:
    q, args = "SELECT * FROM aprendizaje WHERE activo=1", []
    if ambito:
        q += " AND ambito=?"; args.append(ambito)
    if clave:
        q += " AND clave=?"; args.append(clave)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


# ── aclaraciones: explicaciones aportadas por personas (declaradas, trazables, NO hechos verificados) ──
def agregar_aclaracion(caso_id: int | None, usuario: str, texto: str, alcance: str = "caso", entidades: dict | None = None,
                       fuente: str = "chat") -> int:
    alcance = alcance if alcance in ("caso", "actor", "producto_hospital", "hospital") else "caso"
    with conn() as con:
        return con.execute("INSERT INTO aclaraciones (caso_id, usuario, texto, alcance, entidades, fuente, creado_en) VALUES (?,?,?,?,?,?,?)",
                           (caso_id, usuario, texto.strip()[:2000], alcance, json.dumps(entidades or {}, ensure_ascii=False), fuente, now())).lastrowid


def aclaraciones(caso_id: int | None = None, limite: int = 50) -> list[dict]:
    q, args = "SELECT * FROM aclaraciones WHERE 1=1", []
    if caso_id:
        q += " AND caso_id=?"; args.append(caso_id)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limite)
    out = []
    with conn() as con:
        for r in con.execute(q, args):
            d = dict(r)
            try:
                d["entidades"] = json.loads(d["entidades"] or "{}")
            except json.JSONDecodeError:
                d["entidades"] = {}
            out.append(d)
    return out


def aclaraciones_para(entidades: dict, limite: int = 10) -> list[dict]:
    """Aclaraciones cuyo alcance cubre estas entidades (actor, producto+hospital u hospital); las de alcance 'caso'
    sólo aplican a su caso."""
    ent = {k: str(v) for k, v in (entidades or {}).items() if v}
    out = []
    for a in aclaraciones(limite=500):
        e = a.get("entidades") or {}
        if a["alcance"] == "actor" and any(e.get(k) and ent.get(k) == e.get(k) for k in ("auxiliar", "medico")):
            out.append(a)
        elif a["alcance"] == "producto_hospital" and e.get("producto") == ent.get("producto") and e.get("hospital") == ent.get("hospital") and e.get("producto"):
            out.append(a)
        elif a["alcance"] == "hospital" and e.get("hospital") and e.get("hospital") == ent.get("hospital"):
            out.append(a)
        if len(out) >= limite:
            break
    return out


def verificar_aclaracion(id_: int, usuario: str, verificada: bool = True) -> None:
    with conn() as con:
        con.execute("UPDATE aclaraciones SET verificada=?, verificada_por=? WHERE id=?", (1 if verificada else 0, usuario, id_))


def desactivar_aprendizaje(id_: int) -> None:
    with conn() as con:
        con.execute("UPDATE aprendizaje SET activo=0 WHERE id=?", (id_,))


# ── conversaciones ──────────────────────────────────────────────────────────
def nueva_conversacion(usuario: str, titulo: str = "Nueva conversación") -> int:
    with conn() as con:
        return con.execute(
            "INSERT INTO conversaciones (usuario, titulo, creado_en, actualizado_en) VALUES (?,?,?,?)",
            (usuario, titulo, now(), now())).lastrowid


def guardar_mensaje(conversacion_id: int, rol: str, contenido: Any, texto: str = "") -> int:
    with conn() as con:
        con.execute("UPDATE conversaciones SET actualizado_en=? WHERE id=?", (now(), conversacion_id))
        return con.execute(
            "INSERT INTO mensajes (conversacion_id, rol, contenido, texto, creado_en) VALUES (?,?,?,?,?)",
            (conversacion_id, rol, json.dumps(contenido, ensure_ascii=False, default=str),
             texto[:20000], now())).lastrowid


def mensajes(conversacion_id: int, limite: int = 200) -> list[dict]:
    with conn() as con:
        rows = con.execute("SELECT * FROM mensajes WHERE conversacion_id=? ORDER BY id LIMIT ?",
                           (conversacion_id, limite)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["contenido"] = json.loads(d["contenido"])
        except (json.JSONDecodeError, TypeError):
            pass
        out.append(d)
    return out


def conversaciones(usuario: str | None = None, limite: int = 50) -> list[dict]:
    q, args = "SELECT * FROM conversaciones", []
    if usuario:
        q += " WHERE usuario=?"; args.append(usuario)
    q += " ORDER BY COALESCE(actualizado_en, creado_en) DESC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [dict(r) for r in con.execute(q, args)]


# ── uso de LLM ──────────────────────────────────────────────────────────────
def registrar_uso(modelo: str, origen: str, tok_in: int, tok_out: int, usuario: str = "",
                  cache_escritura: int = 0, cache_lectura: int = 0, razonamiento: int = 0) -> None:
    """`tokens_entrada` guarda la entrada EQUIVALENTE (lo que cuesta): tokens sin caché + escritura en caché × 1.25 +
    lectura de caché × 0.1 (0.05 en Opus 5.5). Así el presupuesto del paquete sigue midiendo dinero aunque la caché
    abarate la mayor parte de la entrada. Las columnas de caché guardan los tokens reales para el desglose."""
    equivalente = settings.entrada_equivalente(tok_in, cache_escritura, cache_lectura, modelo)
    with conn() as con:
        con.execute(
            "INSERT INTO uso_llm (ts, periodo, usuario, modelo, origen, tokens_entrada, tokens_salida, costo_usd, "
            "tokens_cache_escritura, tokens_cache_lectura, tokens_razonamiento) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now(), periodo(), usuario, modelo, origen, equivalente, tok_out,
             settings.cost_usd(tok_in, tok_out, modelo, cache_escritura, cache_lectura),
             int(cache_escritura or 0), int(cache_lectura or 0), int(razonamiento or 0)))


def presupuesto() -> dict:
    """Presupuesto mensual de IA vigente: el paquete fijado por Ingeniería Cóndor (Configuración ▸ Presupuesto de IA)
    gana sobre las variables de entorno BUDGET_INPUT_TOKENS / BUDGET_OUTPUT_TOKENS."""
    a = get_ajuste("presupuesto_ia", {}) or {}
    try:
        ent = int(a.get("tokens_entrada") or 0) or int(settings.BUDGET_INPUT_TOKENS)
        sal = int(a.get("tokens_salida") or 0) or int(settings.BUDGET_OUTPUT_TOKENS)
    except (TypeError, ValueError):
        ent, sal = int(settings.BUDGET_INPUT_TOKENS), int(settings.BUDGET_OUTPUT_TOKENS)
    return {"tokens_entrada": max(1, ent), "tokens_salida": max(1, sal), "paquete": str(a.get("paquete") or ""),
            "aviso_pct": int(a.get("aviso_pct") or 80), "actualizado_por": a.get("actualizado_por", ""), "actualizado_en": a.get("actualizado_en", "")}


def uso_periodo(p: str | None = None) -> dict:
    p = p or periodo()
    with conn() as con:
        r = con.execute(
            "SELECT COALESCE(SUM(tokens_entrada),0) ti, COALESCE(SUM(tokens_salida),0) to_, "
            "COALESCE(SUM(costo_usd),0) costo, COUNT(*) llamadas, COALESCE(SUM(tokens_cache_lectura),0) cache_lectura, "
            "COALESCE(SUM(tokens_cache_escritura),0) cache_escritura, COALESCE(SUM(tokens_razonamiento),0) razonamiento "
            "FROM uso_llm WHERE periodo=?", (p,)).fetchone()
    ti, to_ = r["ti"], r["to_"]
    pr = presupuesto()
    pct_e, pct_s = round(100 * ti / pr["tokens_entrada"], 1), round(100 * to_ / pr["tokens_salida"], 1)
    return {
        "periodo": p,
        "tokens_entrada": ti,
        "tokens_salida": to_,
        "llamadas": r["llamadas"],
        "costo_usd": round(r["costo"], 2),
        "presupuesto_entrada": pr["tokens_entrada"],
        "presupuesto_salida": pr["tokens_salida"],
        "pct_entrada": pct_e,
        "pct_salida": pct_s,
        "pct_max": max(pct_e, pct_s),
        "agotado": ti >= pr["tokens_entrada"] or to_ >= pr["tokens_salida"],
        "aviso": max(pct_e, pct_s) >= pr["aviso_pct"],
        "paquete": pr["paquete"],
        "cache_lectura": r["cache_lectura"], "cache_escritura": r["cache_escritura"], "razonamiento": r["razonamiento"],
    }


def presupuesto_agotado() -> bool:
    return bool(uso_periodo()["agotado"])


MENSAJE_PRESUPUESTO_AGOTADO = ("El presupuesto mensual de IA está agotado ({pct_e}% de {ent:,} tokens de entrada y {pct_s}% de {sal:,} de salida "
                               "usados en {periodo}). Los agentes y el chat no pueden analizar hasta que se compren más tokens: contacta a "
                               "Ingeniería Cóndor para ampliar el paquete.")


def mensaje_presupuesto_agotado() -> str:
    u = uso_periodo()
    return MENSAJE_PRESUPUESTO_AGOTADO.format(pct_e=u["pct_entrada"], ent=u["presupuesto_entrada"], pct_s=u["pct_salida"],
                                              sal=u["presupuesto_salida"], periodo=u["periodo"])


# ── reportes ────────────────────────────────────────────────────────────────
def registrar_reporte(archivo: str, ruta: str, tipo: str, titulo: str, parametros: dict,
                      filas: int, bytes_: int, usuario: str = "") -> int:
    with conn() as con:
        return con.execute(
            "INSERT INTO reportes (archivo, ruta, tipo, titulo, parametros, filas, bytes, creado_por, creado_en) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (archivo, ruta, tipo, titulo, json.dumps(parametros, ensure_ascii=False, default=str),
             filas, bytes_, usuario, now())).lastrowid


def reportes(limite: int = 100) -> list[dict]:
    with conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM reportes ORDER BY id DESC LIMIT ?", (limite,))]


def reporte(id_: int) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT * FROM reportes WHERE id=?", (id_,)).fetchone()
    return dict(r) if r else None


# ── perfiles aprendidos (baselines que se refinan en cada corrida) ──────────
def guardar_perfil(ambito: str, clave: str, datos: dict) -> None:
    datos = dict(datos)
    datos["actualizado_en"] = now()
    cols = ["etiqueta", "n", "media", "mediana", "mad", "desv", "p05", "p25", "p75", "p95",
            "minimo", "maximo", "unidad", "estacionalidad", "actualizado_en"]
    vals = [datos.get(c) for c in cols]
    with conn() as con:
        con.execute(
            f"INSERT INTO perfiles (ambito, clave, {','.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))}) "
            f"ON CONFLICT(ambito, clave) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols),
            [ambito, clave, *vals])


def perfiles(ambito: str | None = None, claves: list[str] | None = None) -> dict[str, dict]:
    q, args = "SELECT * FROM perfiles WHERE 1=1", []
    if ambito:
        q += " AND ambito=?"; args.append(ambito)
    if claves:
        q += f" AND clave IN ({','.join('?' * len(claves))})"; args.extend(claves)
    with conn() as con:
        return {f"{r['ambito']}|{r['clave']}": dict(r) for r in con.execute(q, args)}


def ajustar_umbral_perfil(ambito: str, clave: str, delta: float, justificada: bool) -> None:
    """El feedback humano mueve el umbral del perfil: justificar lo relaja, confirmar lo endurece."""
    campo = "justificadas" if justificada else "confirmadas"
    with conn() as con:
        con.execute(
            f"INSERT INTO perfiles (ambito, clave, ajuste_umbral, {campo}, actualizado_en) VALUES (?,?,?,1,?) "
            f"ON CONFLICT(ambito, clave) DO UPDATE SET "
            f"ajuste_umbral = MAX(-1.5, MIN(1.5, perfiles.ajuste_umbral + ?)), "
            f"{campo} = perfiles.{campo} + 1, actualizado_en = excluded.actualizado_en",
            (ambito, clave, delta, now(), delta))


# ── acciones autónomas ──────────────────────────────────────────────────────
def _limpio(obj):
    """NaN/inf → None y tipos numpy → nativos, para que todo sea JSON válido."""
    import math
    if isinstance(obj, dict):
        return {str(k): _limpio(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_limpio(v) for v in obj]
    if hasattr(obj, "item"):
        try:
            obj = obj.item()
        except (ValueError, AttributeError):
            return str(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def proponer_accion(agente: str, tipo: str, titulo: str, payload: dict, motivo: str = "",
                    impacto: dict | None = None, riesgo: str = "bajo",
                    nivel_requerido: int = 2, corrida_id: int | None = None) -> int:
    with conn() as con:
        return con.execute(
            "INSERT INTO acciones (corrida_id, agente, tipo, titulo, motivo, payload, impacto, riesgo, "
            "nivel_requerido, estado, creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (corrida_id, agente, tipo, titulo, motivo,
             json.dumps(_limpio(payload), ensure_ascii=False, default=str),
             json.dumps(_limpio(impacto or {}), ensure_ascii=False, default=str),
             riesgo, nivel_requerido, "propuesta", now())).lastrowid


def acciones(estado: str | None = None, agente: str | None = None, corrida_id: int | None = None,
             limite: int = 500) -> list[dict]:
    q, args = "SELECT * FROM acciones WHERE 1=1", []
    if estado:
        q += " AND estado=?"; args.append(estado)
    if agente:
        q += " AND agente=?"; args.append(agente)
    if corrida_id:
        q += " AND corrida_id=?"; args.append(corrida_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limite)
    out = []
    with conn() as con:
        for r in con.execute(q, args):
            d = dict(r)
            for k in ("payload", "impacto"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else {}
                except (json.JSONDecodeError, TypeError):
                    d[k] = {}
            out.append(d)
    return out


def accion(id_: int) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT * FROM acciones WHERE id=?", (id_,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("payload", "impacto"):
        try:
            d[k] = json.loads(d[k]) if d[k] else {}
        except (json.JSONDecodeError, TypeError):
            d[k] = {}
    return d


def conversacion(id_: int) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT * FROM conversaciones WHERE id=?", (id_,)).fetchone()
    return dict(r) if r else None


def modificar_propuesta(id_: int, **campos) -> int:
    """Cambia una propuesta pendiente: sube la versión y, si tenía una aprobación parcial, la anula (la aprobación
    queda ligada a la versión exacta que se aprobó). Devuelve la nueva versión."""
    with conn() as con:
        r = con.execute("SELECT version, estado FROM acciones WHERE id=?", (id_,)).fetchone()
        if not r:
            return 0
        nueva = int(r["version"] or 1) + 1
        sets = ["version=?"] + [f"{k}=?" for k in campos]
        vals = [nueva, *campos.values()]
        if r["estado"] == "aprobada_parcial":
            sets += ["estado=?", "aprobado_por=NULL", "aprobado_en=NULL"]
            vals.append("propuesta")
        con.execute(f"UPDATE acciones SET {', '.join(sets)} WHERE id=?", [*vals, id_])
        return nueva


def transicion_accion(id_: int, de: str | tuple, a: str, **campos) -> bool:
    """Cambio de estado atómico: sólo procede si la acción sigue en el estado esperado.
    Evita que dos aprobaciones simultáneas ejecuten dos veces."""
    de = (de,) if isinstance(de, str) else tuple(de)
    sets = ", ".join(["estado=?"] + [f"{k}=?" for k in campos])
    with conn() as con:
        cur = con.execute(f"UPDATE acciones SET {sets} WHERE id=? AND estado IN ({','.join('?' * len(de))})",
                          [a, *campos.values(), id_, *de])
        return cur.rowcount == 1


def actualizar_accion(id_: int, **campos) -> None:
    if not campos:
        return
    sets = ", ".join(f"{k}=?" for k in campos)
    with conn() as con:
        con.execute(f"UPDATE acciones SET {sets} WHERE id=?", [*campos.values(), id_])


def resumen_acciones() -> dict:
    with conn() as con:
        rows = con.execute("SELECT estado, COUNT(*) c FROM acciones GROUP BY estado").fetchall()
    return {r["estado"]: r["c"] for r in rows}


# ── casos (expedientes de investigación) ────────────────────────────────────
def guardar_caso(caso: dict) -> int:
    caso = dict(caso)
    for k in ("entidades", "referencias", "expediente", "herramientas"):
        if k in caso and not isinstance(caso[k], str):
            caso[k] = json.dumps(caso[k], ensure_ascii=False, default=str)
    caso.setdefault("creado_en", now())
    caso["actualizado_en"] = now()
    cols = list(caso.keys())
    with conn() as con:
        return con.execute(f"INSERT INTO casos ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           [caso[c] for c in cols]).lastrowid


def _caso_dict(r) -> dict:
    d = dict(r)
    for k in ("entidades", "referencias", "expediente", "herramientas"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k != "herramientas" else [])
        except (json.JSONDecodeError, TypeError):
            d[k] = {}
    return d


def casos(estado: str | None = None, agente: str | None = None, corrida_id: int | None = None, limite: int = 200) -> list[dict]:
    q, args = "SELECT * FROM casos WHERE 1=1", []
    if estado:
        q += " AND estado=?"; args.append(estado)
    if agente:
        q += " AND agente=?"; args.append(agente)
    if corrida_id:
        q += " AND corrida_id=?"; args.append(corrida_id)
    q += " ORDER BY CASE severidad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, impacto_mxn DESC, id DESC LIMIT ?"
    args.append(limite)
    with conn() as con:
        return [_caso_dict(r) for r in con.execute(q, args)]


def caso(id_: int) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT * FROM casos WHERE id=?", (id_,)).fetchone()
    return _caso_dict(r) if r else None


def caso_por_huella(huella: str) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT * FROM casos WHERE huella=? ORDER BY id DESC LIMIT 1", (huella,)).fetchone()
    return _caso_dict(r) if r else None


def resolver_caso(id_: int, estado: str, resolucion: str, usuario: str) -> None:
    with conn() as con:
        con.execute("UPDATE casos SET estado=?, resolucion=?, resuelto_por=?, resuelto_en=?, actualizado_en=? WHERE id=?",
                    (estado, resolucion, usuario, now(), now(), id_))


def casos_similares(entidades: dict, limite: int = 5) -> list[dict]:
    """Casos previos (preferentemente resueltos) que comparten producto, hospital, auxiliar, médico o lote."""
    claves = [str(v) for k, v in (entidades or {}).items() if v and k in ("producto", "hospital", "auxiliar", "medico", "lote")]
    if not claves:
        return []
    with conn() as con:
        rows = [_caso_dict(r) for r in con.execute("SELECT * FROM casos ORDER BY id DESC LIMIT 400")]
    def puntaje(c):
        e = c.get("entidades") or {}
        return sum(1 for v in e.values() if v and str(v) in claves) + (2 if c.get("estado") == "resuelto" else 0)
    out = sorted((c for c in rows if puntaje(c) > 0), key=puntaje, reverse=True)
    return out[:limite]


def exposicion_economica() -> dict:
    """Separa el dinero por estado: sujeto a revisión, confirmado, aclarado."""
    with conn() as con:
        rows = con.execute("SELECT estado, COALESCE(SUM(importe_riesgo),0) s, COUNT(*) n FROM anomalias GROUP BY estado").fetchall()
    d = {r["estado"]: (float(r["s"]), int(r["n"])) for r in rows}
    def suma(*estados):
        return round(sum(d.get(e, (0, 0))[0] for e in estados), 2), sum(d.get(e, (0, 0))[1] for e in estados)
    rev = suma("nueva", "revisada")
    conf = suma("confirmada")
    acl = suma("justificada", "descartada")
    return {"sujeto_a_revision": rev[0], "n_revision": rev[1], "confirmado": conf[0], "n_confirmado": conf[1],
            "aclarado": acl[0], "n_aclarado": acl[1]}
