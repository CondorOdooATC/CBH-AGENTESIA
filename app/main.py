"""CBH · Agentes de IA — aplicación web (FastAPI)."""
from __future__ import annotations

import json
from datetime import datetime
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import db, scheduler
from .agents import autonomia, briefing, consumo, demanda, copiloto
from .config import settings
from .llm import claude
from .odoo import acciones as odoo_acciones, client as odoo_client, queries, schema
from .security import COOKIE, requiere_rol, requiere_usuario, usuario_actual

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))
templates.env.globals.update(app_name=settings.APP_NAME, app_short=settings.APP_SHORT, version=settings.VERSION,
                             cliente=settings.CLIENT, org=settings.ORG)

DEMO = os.getenv("DEMO_MODE", "false").lower() in {"1", "true", "yes", "si", "sí"}
_trabajos: dict[str, dict] = {}


# ── arranque ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if DEMO:
        from .odoo.simulado import OdooSimulado
        if not isinstance(odoo_client._cliente, OdooSimulado):   # no reemplazar un simulado ya inyectado (pruebas)
            odoo_client.set_client(OdooSimulado())
            schema.descubrir(odoo_client.get_client())
        db.log("info", "sistema", "Modo DEMO activo: Odoo simulado en memoria")
    elif settings.odoo_configured and (not (db.get_ajuste(schema.AJUSTE_MAPEO) or {}).get("_resuelto") or schema.en_respaldo()):
        # sin mapeo, o con el consumo aún en salidas de inventario: se vuelve a buscar el módulo de folios en cada arranque
        def _desc():
            try:
                schema.descubrir()
            except Exception as e:  # noqa: BLE001
                db.log("warn", "mapeo", "Auto-descubrimiento inicial falló", str(e))
        threading.Thread(target=_desc, daemon=True).start()
    scheduler.iniciar()
    db.log("info", "sistema", f"Aplicación iniciada v{settings.VERSION}", f"env={settings.APP_ENV} demo={DEMO}")
    yield


app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan,
              docs_url=("/api/docs" if settings.APP_ENV != "production" else None), redoc_url=None, openapi_url=("/openapi.json" if settings.APP_ENV != "production" else None))
app.mount("/static", StaticFiles(directory=str(BASE / "web" / "static")), name="static")

_CSP_BASE = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"


def _cookie_samesite() -> str:
    # embebido en Odoo (iframe de otro sitio) requiere SameSite=None; si no, Lax
    return "none" if (settings.EMBED_ORIGINS and settings.APP_ENV != "development") else "lax"


@app.middleware("http")
async def _seguridad(request: Request, call_next):
    """Cabeceras de seguridad y protección CSRF para todas las rutas.
    CSRF: toda petición que cambia estado (POST/PUT/PATCH/DELETE) debe venir del mismo origen (Sec-Fetch-Site same-origin/none o
    cabecera Origin igual al host); así la cookie de sesión no puede usarse desde otra página, ni siquiera con SameSite=None
    (necesario para el modo embebido en Odoo). Las llamadas con cabecera X-Token (API) no usan cookie y quedan exentas."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.headers.get("x-token"):
        sfs = request.headers.get("sec-fetch-site")
        origen = request.headers.get("origin")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        ok = True
        if sfs:
            ok = sfs in ("same-origin", "none")
        elif origen:
            ok = origen.split("://", 1)[-1].rstrip("/") == host
        if not ok:
            db.log("warn", "seguridad", "Petición rechazada por origen cruzado (CSRF)", f"{request.method} {request.url.path} origen={origen} sfs={sfs}")
            return JSONResponse({"detail": "Petición rechazada: origen no permitido."}, status_code=403)
    resp = await call_next(request)
    frame = " ".join(["'self'"] + settings.EMBED_ORIGINS)
    resp.headers.setdefault("Content-Security-Policy", f"{_CSP_BASE}; frame-ancestors {frame}")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith(("/api/", "/sso")) else "private, max-age=0")
    if settings.APP_ENV != "development":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


def _vacio(v) -> bool:
    from jinja2 import Undefined
    return v is None or isinstance(v, Undefined) or v == "" or (isinstance(v, float) and v != v)


def _fmt_num(v, dec: int = 1, vacio: str = "—") -> str:
    """Nunca None/NaN en pantalla."""
    if _vacio(v):
        return vacio
    try:
        if v is None or v == "" or (isinstance(v, float) and v != v):
            return vacio
        x = float(v)
        if abs(x - round(x)) < 1e-9:
            return f"{x:,.0f}"
        return f"{x:,.{dec}f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_cant(v, unidad: str = "", vacio: str = "—") -> str:
    """Cantidad con su unidad y la precisión de esa unidad (mL con decimal, piezas enteras)."""
    if _vacio(v):
        return vacio
    try:
        return autonomia.formato_cantidad(float(v), unidad or "")
    except (TypeError, ValueError):
        return f"{v} {unidad}".strip()


def _fmt_mxn(v, vacio: str = "—") -> str:
    try:
        if _vacio(v):
            return vacio
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return vacio


def _fmt_dias(v, vacio: str = "sin demanda") -> str:
    if _vacio(v):
        return vacio
    try:
        x = float(v)
        return f"{x:,.1f} días" if x < 10 else f"{x:,.0f} días"
    except (TypeError, ValueError):
        return str(v)


templates.env.filters.update({"num": _fmt_num, "cant": _fmt_cant, "mxn": _fmt_mxn, "dias": _fmt_dias})


def _contexto_chat(request: Request, ctx: dict) -> dict:
    """Contexto del chat global según la pantalla: agente, caso o página (todas las pantallas conversan)."""
    if ctx.get("chat_contexto"):
        return ctx["chat_contexto"]
    path = request.url.path
    if path.startswith("/agentes/consumo"):
        return {"agente": "consumo"}
    if path.startswith("/agentes/demanda"):
        return {"agente": "demanda"}
    if path.startswith("/casos/") and ctx.get("caso"):
        return {"caso_id": ctx["caso"]["id"]}
    pagina = {"/": "hoy", "/acciones": "decisiones", "/casos": "casos", "/hallazgos": "hallazgos", "/reportes": "excel",
              "/configuracion": "configuracion", "/bitacora": "bitacora", "/copiloto": "copiloto"}.get(path.split("?")[0], "general")
    return {"pagina": pagina}


def _nombres_reales() -> dict:
    """Nombres que EXISTEN en los datos del cliente (últimas corridas), para que las sugerencias de la interfaz nunca
    mencionen hospitales, productos o proveedores de ejemplo."""
    a1 = db.get_ajuste("agente1_ultimo", {}) or {}
    a2 = db.get_ajuste("agente2_ultimo", {}) or {}
    k1 = a1.get("kpis") or {}
    hospital = k1.get("top_riesgo_hospital") or ""
    producto, proveedor, destino = "", "", ""
    for fila in (a2.get("alertas") or []) + (a2.get("compras") or []):
        producto = producto or str(fila.get("producto") or "")
        proveedor = proveedor or str(fila.get("proveedor") or "")
        destino = destino or str(fila.get("ubicacion") or fila.get("destino") or "")
    for h in (a1.get("top_hallazgos") or []):
        producto = producto or str(h.get("producto") or "")
        hospital = hospital or str(h.get("hospital") or "")
    return {"hospital": hospital, "producto": producto, "proveedor": proveedor, "destino": destino}


def _sugerencias_copiloto() -> list[str]:
    n = _nombres_reales()
    h, p, prov = n["hospital"], n["producto"], n["proveedor"]
    return [
        f"¿Qué pasó con el consumo de {p} en los últimos 30 días por hospital?" if p else "¿Qué productos concentran el consumo de los últimos 30 días por hospital?",
        f"Dame un Excel del kardex de consumo por sub-almacén de {h} del mes" if h else "Dame un Excel del kardex de consumo por sub-almacén del mes",
        "¿Qué productos están en riesgo de desabasto esta semana y qué propones?",
        "Explícame los hallazgos críticos de báscula y qué debo verificar",
        f"Compara el consumo por auxiliar en {h} vs. sus pares" if h else "Compara el consumo por auxiliar de cada unidad vs. sus pares",
        f"¿Qué pasa si {prov} se retrasa una semana?" if prov else "¿Qué pasa si el proveedor principal se retrasa una semana?",
    ]


def render(request: Request, plantilla: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("usuario", usuario_actual(request))
    ctx.setdefault("nombres", _nombres_reales())
    ctx["chat_global"] = _contexto_chat(request, ctx)
    ctx.setdefault("demo", DEMO)
    ctx.setdefault("entorno", "DEMO" if DEMO else {"production": "PRODUCCIÓN", "staging": "STAGING"}.get(settings.APP_ENV, settings.APP_ENV.upper()))
    ctx.setdefault("llm", claude.disponible())
    ctx.setdefault("nivel", autonomia.nivel())
    ctx.setdefault("pendientes", sum(v for k, v in db.resumen_acciones().items() if k in autonomia.ESTADOS_PENDIENTES))
    # aviso global: la plataforma está leyendo movimientos de inventario porque no encontró el módulo de folios de CBH
    ctx.setdefault("modo_respaldo", bool(not DEMO and settings.odoo_configured and schema.en_respaldo()))
    # si la última llamada a la IA falló, se avisa: lo que se muestre de esa parte es determinista y hay que volver a correr
    est = claude.estado() if claude.disponible() else {"ok": True}
    ctx.setdefault("llm_fallo", None if est.get("ok", True) else est)
    # presupuesto mensual de IA: aviso al acercarse y tope duro al agotarse (los agentes y el chat se detienen)
    ctx.setdefault("presupuesto_ia", db.uso_periodo() if settings.LLM_ENABLED else None)
    ctx["request"] = request
    return templates.TemplateResponse(request, plantilla, ctx, status_code=status_code)


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 307 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=307)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, codigo=exc.status_code, detalle=exc.detail)


# ── salud ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "version": settings.VERSION, "env": settings.APP_ENV, "demo": DEMO,
            "odoo_configurado": settings.odoo_configured or DEMO, "llm": claude.disponible(),
            "scheduler": scheduler.estado()["activo"]}


# ── autenticación ───────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/"):
    if usuario_actual(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", next=next, error=None, odoo_url=settings.ODOO_URL if settings.SSO_SECRET else None)


def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "").split(",")[0].strip()


def _abrir_sesion(resp, u: dict, request: Request, origen: str = "local"):
    """Sesión nueva (rotación de token) con cookie segura; cierra cualquier sesión previa del navegador."""
    previo = request.cookies.get(COOKIE)
    if previo:
        db.cerrar_sesion(previo)
    token = db.crear_sesion(u["id"], horas=settings.SESION_HORAS)
    resp.set_cookie(COOKIE, token, httponly=True, samesite=_cookie_samesite(), secure=settings.APP_ENV != "development",
                    max_age=settings.SESION_HORAS * 3600, path="/")
    with db.conn() as con:
        con.execute("UPDATE usuarios SET ultimo_acceso=? WHERE id=?", (db.now(), u["id"]))
    db.log("info", "auth", f"Inicio de sesión ({origen})", f"ip={_ip(request)}", usuario=u["usuario"])
    return resp


@app.post("/login")
def login_post(request: Request, usuario: str = Form(...), password: str = Form(...), next: str = Form("/")):
    clave_u, clave_ip = f"u:{usuario.strip().lower()}", f"ip:{_ip(request)}"
    if max(db.intentos_recientes(clave_u, settings.LOGIN_VENTANA_MIN), db.intentos_recientes(clave_ip, settings.LOGIN_VENTANA_MIN)) >= settings.LOGIN_MAX_INTENTOS:
        db.log("warn", "seguridad", "Acceso bloqueado temporalmente por intentos repetidos", f"{usuario} ip={_ip(request)}")
        return render(request, "login.html", status_code=429, next=next, odoo_url=settings.ODOO_URL if settings.SSO_SECRET else None,
                      error=f"Demasiados intentos. Espera {settings.LOGIN_VENTANA_MIN} minutos o pide a Ingeniería Cóndor que restablezca tu acceso.")
    u = db.verificar_credenciales(usuario, password)
    if not u:
        db.registrar_intento(clave_u); db.registrar_intento(clave_ip)
        db.log("warn", "auth", "Intento de acceso fallido", f"{usuario} ip={_ip(request)}")
        return render(request, "login.html", status_code=401, next=next, odoo_url=settings.ODOO_URL if settings.SSO_SECRET else None, error="Usuario o contraseña incorrectos.")
    db.limpiar_intentos(clave_u)
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    return _abrir_sesion(resp, u, request)


# ── acceso desde Odoo (SSO con token firmado por el addon cbh_agentes_ia) ──
@app.get("/sso")
def sso_entrar(request: Request, token: str = "", embed: int = 0):
    from . import sso
    clave_ip = f"sso:{_ip(request)}"
    if db.intentos_recientes(clave_ip, settings.LOGIN_VENTANA_MIN) >= settings.LOGIN_MAX_INTENTOS * 3:
        return render(request, "error.html", status_code=429, mensaje="Demasiados intentos de acceso desde Odoo; espera unos minutos.")
    try:
        p = sso.verificar(token)
        if p.get("fin", "sso") != "sso":
            raise ValueError("Este enlace es de verificación, no de acceso.")
        u = sso.entrar(p)
    except ValueError as e:
        db.registrar_intento(clave_ip)
        db.log("warn", "seguridad", "Acceso SSO rechazado", str(e))
        return render(request, "error.html", status_code=403, mensaje=f"No se pudo entrar desde Odoo: {e}",
                      sso_reintento=(settings.ODOO_URL + "/cbh_agentes_ia/abrir") if settings.ODOO_URL else None)
    resp = RedirectResponse("/", status_code=303)
    return _abrir_sesion(resp, u, request, origen="Odoo SSO")


@app.get("/sso/verificar")
def sso_verificar(request: Request, token: str = ""):
    """Prueba de instalación desde Odoo (botón «Probar conexión»): valida el token sin abrir sesión ni crear usuarios."""
    from . import sso
    try:
        p = sso.verificar(token, consumir=False)
        if p.get("fin") != "verificar":
            raise ValueError("Token de verificación inválido.")
        return {"ok": True, "plataforma": settings.APP_NAME, "version": settings.VERSION, "env": settings.APP_ENV,
                "usuario": p.get("login"), "rol": p.get("rol"), "base": p.get("db"),
                "embebido_permitido": bool(settings.EMBED_ORIGINS), "odoo_configurado": settings.odoo_configured}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/logout")
def logout(request: Request):
    db.cerrar_sesion(request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ── páginas ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def hoy(request: Request, u: dict = Depends(requiere_usuario)):
    a1 = db.get_ajuste("agente1_ultimo", {}) or {}
    a2 = db.get_ajuste("agente2_ultimo", {}) or {}
    b = db.get_ajuste("ultimo_briefing", {}) or {}
    pendientes = _ordenar_acciones([a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, limite=300)])
    return render(request, "hoy.html", a1=a1, a2=a2, briefing=b, uso=db.uso_periodo(), decisiones=pendientes[:3], n_pendientes=len(pendientes),
                  casos=db.casos(estado="abierto", limite=4), agentes=_estado_agentes(), actividad=_actividad(25),
                  exposicion=db.exposicion_economica(), plan=(a2.get("plan_razonado") or {}), vigilancia=db.get_ajuste("ultima_vigilancia", {}) or {},
                  odoo=odoo_client.get_client().probar() if (DEMO or settings.odoo_configured) else {"ok": False, "error": "Sin configurar"})


@app.get("/agentes/consumo", response_class=HTMLResponse)
def pagina_consumo(request: Request, u: dict = Depends(requiere_usuario)):
    a1 = db.get_ajuste("agente1_ultimo", {}) or {}
    return render(request, "agente_consumo.html", a1=a1, corridas=db.corridas("consumo", 15), agente=_estado_agentes()["consumo"],
                  casos=db.casos(agente="consumo", limite=12), exposicion=db.exposicion_economica(),
                  hallazgos=db.anomalias(corrida_id=a1.get("corrida_id"), limite=300) if a1 else [])


@app.get("/agentes/demanda", response_class=HTMLResponse)
def pagina_demanda(request: Request, u: dict = Depends(requiere_usuario)):
    a2 = db.get_ajuste("agente2_ultimo", {}) or {}
    return render(request, "agente_demanda.html", a2=a2, corridas=db.corridas("demanda", 15), agente=_estado_agentes()["demanda"],
                  plan=(a2.get("plan_razonado") or {}), autoeval=(a2.get("autoevaluacion") or {}))


@app.get("/casos", response_class=HTMLResponse)
def pagina_casos(request: Request, estado: str | None = "abierto", u: dict = Depends(requiere_usuario)):
    return render(request, "casos.html", casos=db.casos(estado=None if estado == "todos" else estado, limite=300), estado=estado,
                  exposicion=db.exposicion_economica())


@app.get("/casos/{cid}", response_class=HTMLResponse)
def pagina_caso(request: Request, cid: int, u: dict = Depends(requiere_usuario)):
    c = db.caso(cid)
    if not c:
        raise HTTPException(404, "Caso no encontrado")
    return render(request, "caso.html", caso=c, similares=[x for x in db.casos_similares(c.get("entidades") or {}, 6) if x["id"] != cid],
                  aclaraciones=db.aclaraciones(cid) + [a for a in db.aclaraciones_para(c.get("entidades") or {}) if a.get("caso_id") != cid])


class Aclaracion(BaseModel):
    texto: str
    alcance: str = "caso"


@app.post("/api/casos/{cid}/aclaracion")
def api_aclaracion(cid: int, body: Aclaracion, u: dict = Depends(requiere_usuario)):
    """Explicación aportada por una persona: queda como declaración trazable (autor, fecha, alcance), no como hecho."""
    c = db.caso(cid)
    if not c:
        raise HTTPException(404, "Caso no encontrado")
    if not body.texto.strip():
        raise HTTPException(400, "Escribe la aclaración.")
    i = db.agregar_aclaracion(cid, u["usuario"], body.texto, body.alcance, c.get("entidades") or {}, fuente="formulario")
    db.log("info", "casos", f"Aclaración #{i} registrada en el caso #{cid}", body.texto[:200], u["usuario"])
    return {"id": i, "alcance": body.alcance}


@app.post("/api/casos/{cid}/aclaracion/{aid}/verificar")
def api_aclaracion_verificar(cid: int, aid: int, u: dict = Depends(requiere_rol("operacion"))):
    db.verificar_aclaracion(aid, u["usuario"], True)
    db.log("info", "casos", f"Aclaración #{aid} marcada como verificada (caso #{cid})", usuario=u["usuario"])
    return {"id": aid, "verificada": True}


class ResolverCaso(BaseModel):
    estado: str
    resolucion: str = ""


@app.post("/api/casos/{cid}/resolver")
def api_resolver_caso(cid: int, body: ResolverCaso, u: dict = Depends(requiere_rol("operacion"))):
    c = db.caso(cid)
    if not c:
        raise HTTPException(404, "Caso no encontrado")
    db.resolver_caso(cid, body.estado, body.resolucion, u["usuario"])
    if body.resolucion:
        ent = c.get("entidades") or {}
        # la resolución queda como declaración verificada por operación (trazable) y como nota de aprendizaje acotada al hospital
        aid = db.agregar_aclaracion(cid, u["usuario"], body.resolucion, "caso", ent, fuente="resolucion")
        db.verificar_aclaracion(aid, u["usuario"], True)
        db.agregar_aprendizaje("hospital" if ent.get("hospital") else "global", ent.get("hospital") or "",
                               f"Caso #{cid} «{c['titulo']}» {body.estado} (resolución declarada por {u['usuario']}, alcance: ese caso): {body.resolucion}",
                               usuario=u["usuario"])
    # la resolución del caso también clasifica el hallazgo de origen (aprendizaje de umbrales)
    ref = c.get("referencias") or {}
    if ref.get("huella"):
        with db.conn() as con:
            r = con.execute("SELECT id FROM anomalias WHERE huella=? ORDER BY id DESC LIMIT 1", (ref["huella"],)).fetchone()
        if r:
            estado_h = {"resuelto": "justificada", "descartado": "descartada"}.get(body.estado)
            if estado_h:
                consumo.retroalimentar(r["id"], estado_h, body.resolucion, u["usuario"])
    db.log("info", "casos", f"Caso #{cid} {body.estado}", body.resolucion, u["usuario"])
    return {"id": cid, "estado": body.estado}


@app.post("/api/casos/investigar/{hid}")
def api_investigar_hallazgo(hid: int, u: dict = Depends(requiere_rol("operacion"))):
    """Investiga (o re-investiga) un hallazgo concreto bajo demanda."""
    from .agents import investigador
    with db.conn() as con:
        a = con.execute("SELECT * FROM anomalias WHERE id=?", (hid,)).fetchone()
    if not a:
        raise HTTPException(404, "Hallazgo no encontrado")
    a = dict(a)
    for k in ("metodos", "motivos"):
        try:
            a[k] = json.loads(a[k] or "[]")
        except (json.JSONDecodeError, TypeError):
            a[k] = []
    a["tipo"], a["titulo"] = "anomalia", f"{a.get('producto')} · {a.get('folio')} · {a.get('hospital')}"
    df = queries.consumo(dias=settings.AGENT1_LOOKBACK_DAYS)
    ctx = investigador.Contexto(df, {int(k): float(v) for k, v in (db.get_ajuste("densidades", {}) or {}).items()})
    r = investigador.investigar(a, ctx, con_llm=True, usuario=u["usuario"])
    e = r["expediente"]
    cid = db.guardar_caso({"corrida_id": a.get("corrida_id"), "agente": "consumo", "tipo": "anomalia", "titulo": a["titulo"],
                           "severidad": a.get("severidad"), "entidades": {k: a.get(k) for k in ("producto", "hospital", "medico", "lote", "folio") if a.get(k)},
                           "referencias": {"anomalia_id": hid, "huella": a.get("huella")}, "expediente": e, "conclusion": e.get("conclusion"),
                           "confianza": e.get("confianza"), "impacto_mxn": float(e.get("impacto_mxn") or 0),
                           "accion_recomendada": e.get("accion_recomendada"), "responsable": e.get("responsable"),
                           "investigado_con": r["modo"], "herramientas": r["trazas"], "huella": a.get("huella")})
    return {"caso_id": cid, "modo": r["modo"], "expediente": e}


@app.get("/hallazgos", response_class=HTMLResponse)
def pagina_hallazgos(request: Request, estado: str | None = None, severidad: str | None = None,
                     u: dict = Depends(requiere_usuario)):
    rows = db.anomalias(estado=estado, severidad=severidad, limite=500)
    for r in rows:
        try:
            r["motivos"] = json.loads(r["motivos"] or "[]")
        except (json.JSONDecodeError, TypeError):
            r["motivos"] = []
    return render(request, "hallazgos.html", hallazgos=rows, estado=estado, severidad=severidad)


_PRIORIDAD_TIPO = {"transferencia_interna": 0, "solicitud_compra": 1, "regla_reabastecimiento": 2, "actividad": 3,
                   "nota_chatter": 4, "alerta": 5}
_PRIORIDAD_CRIT = {"desabasto": 0, "critico": 1, "reordenar": 2}


def _urgencia_dias(a: dict) -> float:
    """Días hasta la fecha necesaria (o de quiebre) de la acción; 999 si no aplica."""
    i = a.get("impacto") or {}
    f = a.get("fecha_requerida") or i.get("fecha_necesaria") or i.get("fecha_quiebre")
    if not f:
        return 999.0
    try:
        return float((datetime.fromisoformat(str(f)[:10]).date() - datetime.now().date()).days)
    except ValueError:
        return 999.0


def _anotar_prioridad(a: dict) -> dict:
    """Prioridad explicable: urgencia (fecha necesaria), dependencia (una compra que no llega a tiempo depende de una
    transferencia; una transferencia topada depende de una compra) e impacto (importe)."""
    i = a.get("impacto") or {}
    dias = _urgencia_dias(a)
    a["urgencia_dias"] = None if dias >= 999 else int(dias)
    a["prioridad"] = ("inmediata" if dias <= 1 else "alta" if dias <= 3 else "media" if dias <= 7 else "normal") if dias < 999 else \
        ("alta" if i.get("criticidad") in ("desabasto", "critico") else "normal")
    notas = []
    if i.get("llega_a_tiempo") is False:
        notas.append("no llega antes del quiebre: depende de una transferencia interna o de reclamar una entrega")
    if a.get("motivo") and "faltan" in str(a.get("motivo")) and a["tipo"] == "transferencia_interna":
        notas.append("el origen no cubre todo: depende de una compra por el resto")
    if i.get("cubierto_previo_destino"):
        notas.append(f"complementa {_fmt_cant(i['cubierto_previo_destino'], i.get('unidad', ''))} ya propuestos al mismo destino")
    a["dependencias"] = notas
    return a


def _ordenar_acciones(acciones: list[dict]) -> list[dict]:
    """Primero lo urgente (fecha necesaria más próxima), luego la criticidad del destino, luego el importe."""
    for a in acciones:
        _anotar_prioridad(a)
    return sorted(acciones, key=lambda a: (_urgencia_dias(a), _PRIORIDAD_CRIT.get((a.get("impacto") or {}).get("criticidad", ""), 5),
                                           _PRIORIDAD_TIPO.get(a["tipo"], 9), -float((a.get("impacto") or {}).get("importe") or 0)))


_ETIQUETA_TIPO = {"transferencia_interna": "Transferencias internas", "solicitud_compra": "Compras a proveedor",
                  "regla_reabastecimiento": "Reglas min/max", "ticket_helpdesk": "Tickets de Helpdesk", "cuarentena_lote": "Cuarentena de lotes",
                  "desechar_lote": "Desechos", "solicitar_conteo": "Conteos físicos", "recordatorio_proveedor": "Recordatorios a proveedor",
                  "reprogramar_compra": "Reprogramación de compras", "confirmar_transferencia": "Confirmar transferencias",
                  "validar_recepcion": "Validar recepciones", "ajustar_lead_time_proveedor": "Plazos de proveedor",
                  "actividad": "Actividades", "nota_chatter": "Notas", "alerta": "Alertas internas", "aviso_equipo": "Avisos a personas"}


def _agrupar_decisiones(acciones: list[dict]) -> list[dict]:
    """Agrupa las pendientes por tipo y destino/hospital para decidir en bloque, ordenadas por urgencia."""
    grupos: dict[str, dict] = {}
    for a in acciones:
        i = a.get("impacto") or {}
        p = a.get("payload") or {}
        if a["tipo"] == "transferencia_interna":
            clave = f"Transferencias → {p.get('destino') or '?'}"
        elif a["tipo"] == "solicitud_compra":
            clave = "Compras a proveedor"
        elif a["tipo"] in ("ticket_helpdesk",):
            clave = f"Tickets · {p.get('equipo') or 'Helpdesk'}"
        elif a["tipo"] == "aviso_equipo":
            clave = f"Avisar a {p.get('equipo_nombre') or p.get('equipo') or 'personas'}"
        else:
            clave = _ETIQUETA_TIPO.get(a["tipo"], a["tipo"])
        g = grupos.setdefault(clave, {"titulo": clave, "acciones": [], "importe": 0.0, "urgencia": 9, "riesgo_max": "bajo"})
        g["acciones"].append(a)
        g["importe"] += float(i.get("importe") or 0)
        g["urgencia"] = min(g["urgencia"], _urgencia_dias(a), _PRIORIDAD_CRIT.get(i.get("criticidad", ""), 5) + 10)
        g["urgencia_dias"] = min(g.get("urgencia_dias", 999), _urgencia_dias(a))
        if {"bajo": 0, "medio": 1, "alto": 2}.get(a["riesgo"], 0) > {"bajo": 0, "medio": 1, "alto": 2}.get(g["riesgo_max"], 0):
            g["riesgo_max"] = a["riesgo"]
    return sorted(grupos.values(), key=lambda g: (g["urgencia"], -g["importe"]))


@app.get("/acciones", response_class=HTMLResponse)
def pagina_acciones(request: Request, estado: str | None = "pendientes", u: dict = Depends(requiere_usuario)):
    if estado == "pendientes":
        acciones = _ordenar_acciones([a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, limite=300)])
        grupos = _agrupar_decisiones(acciones)
    else:
        acciones = db.acciones(estado=None if estado == "todas" else estado, limite=300)
        grupos = []
    return render(request, "acciones.html", acciones=acciones, grupos=grupos,
                  estado=estado, conteo=db.resumen_acciones(), autonomia=autonomia.resumen())


@app.get("/copiloto", response_class=HTMLResponse)
def pagina_copiloto(request: Request, c: int | None = None, u: dict = Depends(requiere_usuario)):
    convs = db.conversaciones(u["usuario"], 30)
    if c:
        cv = db.conversacion(c)
        if not cv or cv["usuario"] != u["usuario"]:      # privacidad estricta: ni admin ni condor leen conversaciones ajenas
            raise HTTPException(403, "Esa conversación no es tuya.")
    cid = c or (convs[0]["id"] if convs else db.nueva_conversacion(u["usuario"]))
    msgs = [m for m in db.mensajes(cid) if m["texto"]]
    return render(request, "copiloto.html", conversaciones=convs, conversacion_id=cid, mensajes=msgs, sugerencias=_sugerencias_copiloto())


@app.get("/reportes", response_class=HTMLResponse)
def pagina_reportes(request: Request, u: dict = Depends(requiere_usuario)):
    return render(request, "reportes.html", reportes=db.reportes(200))


@app.get("/reportes/{rid}/descargar")
def descargar_reporte(rid: int, u: dict = Depends(requiere_usuario)):
    r = db.reporte(rid)
    if not r or not Path(r["ruta"]).exists():
        raise HTTPException(404, "Reporte no encontrado")
    return FileResponse(r["ruta"], filename=r["archivo"],
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _preparacion(mapeo: dict, auto: dict, uso: dict, avisos: dict) -> list[dict]:
    """Lista de preparación de la plataforma (semáforo): cada punto dice si está listo, por qué y qué hacer.
    Es lo primero que ve quien configura; no requiere entender la parte técnica."""
    pts: list[dict] = []

    def punto(clave, titulo, estado, detalle, accion=None):
        pts.append({"clave": clave, "titulo": titulo, "estado": estado, "detalle": detalle, "accion": accion})

    # 1) Odoo
    if DEMO:
        punto("odoo", "Conexión a Odoo", "a", "Modo demo: se usa un Odoo simulado. Para conectar el real hay que definir ODOO_URL, ODOO_DB, ODOO_USER y ODOO_API_KEY en Render.")
    elif settings.odoo_configured:
        punto("odoo", "Conexión a Odoo", "v", f"{settings.ODOO_URL} · base {settings.ODOO_DB} · usuario técnico {settings.ODOO_USER}.", "probar")
    else:
        punto("odoo", "Conexión a Odoo", "r", "Faltan variables de entorno en Render (ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY).")
    # 2) módulo de folios
    ent = (mapeo.get("entidades") or {})
    folio = ent.get("folio") or {}
    if not mapeo.get("_resuelto"):
        punto("folios", "Módulo de folios (CB Ticket) de CBH", "a", "Aún no se ha leído la estructura de Odoo. Pulsa «Re-descubrir» una vez conectado.", "descubrir")
    elif schema.en_respaldo():
        punto("folios", "Módulo de folios (CB Ticket) de CBH", "r",
              "No se encontró el módulo de operación médica: el consumo se está leyendo de salidas de inventario, sin médico, auxiliar ni báscula. "
              "Suele ser falta de permisos de lectura del usuario técnico sobre ese módulo.", "descubrir")
    else:
        punto("folios", "Módulo de folios (CB Ticket) de CBH", "v", f"Detectado: {folio.get('modelo')} · consumos en {(ent.get('consumo') or {}).get('modelo')}.")
    # 3) IA
    if not settings.LLM_ENABLED:
        punto("ia", "Inteligencia artificial (Claude)", "r", "Sin clave de API: los agentes no pueden analizar ni el copiloto responder. La configura Ingeniería Cóndor en Render.")
    else:
        est = claude.estado() if claude.disponible() else {"ok": True}
        if est.get("ok", True):
            punto("ia", "Inteligencia artificial (Claude)", "v", f"Activa · modelo {settings.ANTHROPIC_MODEL}.")
        else:
            punto("ia", "Inteligencia artificial (Claude)", "a", f"La última llamada falló: {est.get('error') or est.get('motivo') or 'error'}. Revisa la bitácora; si persiste, avisa a Cóndor.")
    # 4) presupuesto
    if not settings.LLM_ENABLED:
        punto("presupuesto", "Presupuesto mensual de IA", "a", "No aplica hasta que la IA esté activa.", "presupuesto")
    else:
        if uso.get("agotado"):
            punto("presupuesto", "Presupuesto mensual de IA", "r", f"Agotado ({uso['pct_max']} % usado). Los agentes y el chat están detenidos hasta ampliar el paquete o que inicie el mes.", "presupuesto")
        elif uso.get("aviso"):
            punto("presupuesto", "Presupuesto mensual de IA", "a", f"{uso['pct_max']} % del paquete usado en {uso['periodo']}. Considera ampliar el paquete antes de que se agote.", "presupuesto")
        else:
            punto("presupuesto", "Presupuesto mensual de IA", "v", f"{uso['pct_max']} % usado en {uso['periodo']} ({uso['llamadas']} llamadas, ≈ ${uso['costo_usd']} USD).", "presupuesto")
    # 5) SSO
    if settings.SSO_SECRET:
        punto("sso", "Acceso desde Odoo (un clic)", "v", "Los usuarios de Odoo entran desde el menú «Agentes de IA» sin otra contraseña; su rol viene de sus grupos de Odoo.")
    else:
        punto("sso", "Acceso desde Odoo (un clic)", "a", "SSO_SECRET no está definido en Render: los usuarios entran con usuario y contraseña de esta plataforma. Para producción, define el mismo secreto aquí y en Odoo (Ajustes ▸ Agentes de IA).")
    # 6) autonomía y topes
    p = auto.get("politicas") or {}
    if auto.get("nivel", 1) == 0:
        punto("autonomia", "Autonomía de los agentes", "a", "Nivel 0: los agentes sólo observan e informan; no proponen acciones ni el copiloto crea documentos en Odoo.", "autonomia")
    elif float(p.get("tope_importe") or 0) < float(p.get("importe_riesgo_alto") or 0):
        punto("autonomia", "Autonomía de los agentes", "a",
              f"El importe máximo por acción (${float(p.get('tope_importe') or 0):,.0f} MXN) es menor que el umbral de riesgo alto "
              f"(${float(p.get('importe_riesgo_alto') or 0):,.0f}): ninguna compra por encima de ${float(p.get('tope_importe') or 0):,.0f} se creará, "
              "ni aunque se apruebe. Revisa los límites de seguridad.", "autonomia")
    else:
        punto("autonomia", "Autonomía de los agentes", "v",
              f"Nivel {auto.get('nivel')}: {auto.get('nivel_nombre')}. Límite por acción ${float(p.get('tope_importe') or 0):,.0f} MXN; riesgo alto desde ${float(p.get('importe_riesgo_alto') or 0):,.0f}.", "autonomia")
    # 7) avisos
    eq = (avisos or {}).get("equipos") or {}
    con_logins = any((v.get("logins") or []) for v in eq.values())
    if avisos.get("incluir_admin_agentes") or con_logins:
        punto("avisos", "Avisos a personas en Odoo", "v", "Los seguimientos se avisan a los grupos de Odoo de cada equipo" + (" y a los administradores de Agentes de IA." if avisos.get("incluir_admin_agentes") else "."), "avisos")
    else:
        punto("avisos", "Avisos a personas en Odoo", "a", "No hay logins adicionales ni se incluye a los administradores: si un grupo de Odoo está vacío, el aviso no llegará a nadie.", "avisos")
    # 8) programación
    if settings.SCHEDULE_ENABLED:
        punto("programacion", "Corridas automáticas", "v", f"Consumo: {settings.SCHEDULE_AGENT1_CRON} · Abasto: {settings.SCHEDULE_AGENT2_CRON} (hora de Ciudad de México).")
    else:
        punto("programacion", "Corridas automáticas", "a", "Desactivadas (SCHEDULE_ENABLED=false): los agentes sólo corren cuando alguien pulsa «Ejecutar».")
    # 9) entorno
    if settings.APP_ENV == "production":
        punto("entorno", "Entorno", "v", "PRODUCCIÓN. Las acciones marcadas «sólo propuesta» quedan para ejecución manual en Odoo.")
    else:
        punto("entorno", "Entorno", "a", f"{settings.APP_ENV.upper()}: todo se ejecuta contra la base conectada (staging). Para producción: APP_ENV=production, ODOO_URL de la base productiva y claves nuevas (ver docs/PRODUCCION.md).")
    return pts


@app.get("/configuracion", response_class=HTMLResponse)
def pagina_config(request: Request, u: dict = Depends(requiere_rol("operacion"))):
    with db.conn() as con:
        usuarios = [dict(r) for r in con.execute("SELECT id, usuario, nombre, rol, activo, creado_en FROM usuarios")]
    from .odoo import memoria_consumo
    mapeo_, auto_, uso_, avisos_ = schema.cargar(), autonomia.resumen(), db.uso_periodo(), odoo_acciones.configuracion_avisos()
    return render(request, "configuracion.html", mapeo=mapeo_, autonomia=auto_, preparacion=_preparacion(mapeo_, auto_, uso_, avisos_),
                  memoria=memoria_consumo.estado(),
                  aprendizaje=db.aprendizaje(limite=200), usuarios=usuarios, scheduler=scheduler.estado(),
                  odoo_conf={"url": settings.ODOO_URL, "db": settings.ODOO_DB, "usuario": settings.ODOO_USER,
                             "configurado": settings.odoo_configured},
                  llm_conf={"modelo": settings.ANTHROPIC_MODEL, "modelo_rapido": settings.ANTHROPIC_MODEL_FAST, "activo": settings.LLM_ENABLED},
                  a1_cfg=db.get_ajuste("agente1_config", {}) or {}, a2_cfg=db.get_ajuste("agente2_config", {}) or {},
                  regiones=json.dumps(db.get_ajuste("regiones", {}) or {}, ensure_ascii=False),
                  avisos=odoo_acciones.configuracion_avisos(), uso=db.uso_periodo(), presupuesto=db.presupuesto(),
                  cron={"a1": settings.SCHEDULE_AGENT1_CRON, "a2": settings.SCHEDULE_AGENT2_CRON, "on": settings.SCHEDULE_ENABLED})


@app.get("/bitacora", response_class=HTMLResponse)
def pagina_bitacora(request: Request, nivel: str | None = None, u: dict = Depends(requiere_usuario)):
    return render(request, "bitacora.html", eventos=db.bitacora(400, nivel), nivel_filtro=nivel, uso=db.uso_periodo(),
                  corridas=db.corridas(limite=40))


# ── API: agentes (ejecución en segundo plano) ───────────────────────────────
def _lanzar(nombre: str, fn, **kw) -> str:
    jid = uuid.uuid4().hex[:12]
    _trabajos[jid] = {"id": jid, "agente": nombre, "estado": "ejecutando", "inicio": time.time(), "resultado": None, "error": None, "pasos": []}
    if len(_trabajos) > 60:  # no acumular trabajos viejos en memoria
        for k in sorted(_trabajos, key=lambda k: _trabajos[k]["inicio"])[:-40]:
            _trabajos.pop(k, None)

    def _progreso(paso: str, detalle: str = ""):
        _trabajos[jid]["pasos"].append({"t": round(time.time() - _trabajos[jid]["inicio"], 1), "paso": paso, "detalle": detalle})

    def _run():
        try:
            import inspect
            if "progreso" in inspect.signature(fn).parameters:
                kw["progreso"] = _progreso
            r = fn(**kw)
            _trabajos[jid].update(estado="ok", resultado={k: v for k, v in r.items() if k not in ("acciones", "informe", "hallazgos")}
                                  | {"acciones": len(r.get("acciones", [])) if isinstance(r.get("acciones"), list) else r.get("acciones")})
        except Exception as e:  # noqa: BLE001
            _trabajos[jid].update(estado="error", error=str(e))
        _trabajos[jid]["fin"] = time.time()
    threading.Thread(target=_run, daemon=True).start()
    return jid


class EjecutarAgente(BaseModel):
    dias: int | None = None
    horizonte: int | None = None
    con_llm: bool = True


@app.post("/api/agentes/{agente}/ejecutar")
def api_ejecutar(agente: str, body: EjecutarAgente | None = None, u: dict = Depends(requiere_rol("operacion"))):
    body = body or EjecutarAgente()
    if any(t["estado"] == "ejecutando" and t["agente"] == agente for t in _trabajos.values()):
        raise HTTPException(409, "Ese agente ya está en ejecución.")
    if agente == "consumo":
        jid = _lanzar("consumo", consumo.ejecutar, dias=body.dias, usuario=u["usuario"], disparo="manual", con_llm=body.con_llm)
    elif agente == "demanda":
        jid = _lanzar("demanda", demanda.ejecutar, horizonte=body.horizonte, usuario=u["usuario"], disparo="manual", con_llm=body.con_llm)
    elif agente == "briefing":
        jid = _lanzar("briefing", briefing.generar, usuario=u["usuario"], con_llm=body.con_llm)
    elif agente == "vigilancia":
        from .agents import vigilancia
        jid = _lanzar("vigilancia", vigilancia.ejecutar, usuario=u["usuario"])
    else:
        raise HTTPException(404, "Agente desconocido")
    return {"trabajo": jid}


@app.get("/api/trabajos/{jid}")
def api_trabajo(jid: str, u: dict = Depends(requiere_usuario)):
    t = _trabajos.get(jid)
    if not t:
        raise HTTPException(404, "Trabajo no encontrado")
    out = dict(t)
    out["segundos"] = round((t.get("fin") or time.time()) - t["inicio"], 1)
    return out


@app.get("/api/estado")
def api_estado(u: dict = Depends(requiere_usuario)):
    return {"odoo": odoo_client.get_client().probar() if (DEMO or settings.odoo_configured) else {"ok": False},
            "llm": claude.disponible(), "uso": db.uso_periodo(), "autonomia": autonomia.resumen(),
            "corridas": db.corridas(limite=10), "scheduler": scheduler.estado(),
            "trabajos": [t for t in _trabajos.values() if t["estado"] == "ejecutando"]}


def _estado_agentes() -> dict:
    a1, a2, vg = db.get_ajuste("agente1_ultimo", {}) or {}, db.get_ajuste("agente2_ultimo", {}) or {}, db.get_ajuste("ultima_vigilancia", {}) or {}
    sch = {j["id"]: j["proxima"] for j in scheduler.estado().get("trabajos", [])}
    corriendo = {t["agente"] for t in _trabajos.values() if t["estado"] == "ejecutando"}
    def item(clave, ultimo, prox, resumen):
        return {"ultima": ultimo.get("fecha"), "proxima": sch.get(prox), "corriendo": clave in corriendo, "resumen": resumen}
    k1, k2 = a1.get("kpis") or {}, a2.get("kpis") or {}
    return {
        "consumo": item("consumo", a1, "agente1", (f"{k1.get('criticos', 0)} casos críticos, {k1.get('patrones', 0)} patrones, "
                                                   f"{len(a1.get('casos') or [])} expedientes · en revisión ${k1.get('importe_riesgo', 0):,.0f}") if k1 else "Sin corridas aún"),
        "demanda": item("demanda", a2, "agente2", (f"{(k2.get('desabasto', 0) + k2.get('critico', 0))} ubicaciones en riesgo, "
                                                   f"{len((a2.get('plan_razonado') or {}).get('decisiones', []))} decisiones anticipadas, "
                                                   f"compra sugerida ${k2.get('importe_compra', 0):,.0f}") if k2 else "Sin corridas aún"),
        "vigilancia": item("vigilancia", vg, "vigilancia", (f"{len(vg.get('nuevas_alertas') or [])} alertas nuevas · " +
                                                            ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in (vg.get('resumen') or {}).items() if v)) if vg else "Sin vigilancia aún"),
    }


def _actividad(limite: int = 40) -> list[dict]:
    """Línea de tiempo de lo que hacen los agentes (bitácora + corridas + casos + acciones)."""
    iconos = {"agente1": "🔎", "agente2": "📈", "investigador": "🕵️", "planificador": "🧭", "vigilancia": "👁", "autonomia": "✅",
              "aprendizaje": "🧠", "casos": "📁", "scheduler": "⏰", "briefing": "📝", "llm": "🤖"}
    ev = [e for e in db.bitacora(200) if e["origen"] in iconos]
    out = [{"ts": e["ts"], "icono": iconos.get(e["origen"], "•"), "origen": e["origen"], "texto": e["evento"],
            "detalle": (e["detalle"] or "")[:160], "nivel": e["nivel"]} for e in ev]
    return out[:limite]


@app.get("/api/actividad")
def api_actividad(u: dict = Depends(requiere_usuario)):
    return {"actividad": _actividad(), "agentes": _estado_agentes()}


@app.get("/api/panorama")
def api_panorama(dias: int = 30, u: dict = Depends(requiere_usuario)):
    return queries.panorama(dias)


# ── API: acciones ───────────────────────────────────────────────────────────
class Nota(BaseModel):
    nota: str = ""
    ids: list[int] | None = None


@app.post("/api/acciones/{aid}/aprobar")
def api_aprobar(aid: int, u: dict = Depends(requiere_rol("operacion"))):
    return autonomia.aprobar(aid, u["usuario"], u["rol"])


@app.post("/api/acciones/{aid}/rechazar")
def api_rechazar(aid: int, body: Nota | None = None, u: dict = Depends(requiere_rol("operacion"))):
    return autonomia.rechazar(aid, u["usuario"], (body or Nota()).nota)


@app.post("/api/acciones/{aid}/revertir")
def api_revertir(aid: int, u: dict = Depends(requiere_rol("admin"))):
    return autonomia.revertir(aid, u["usuario"])


@app.post("/api/acciones/verificar")
def api_verificar(u: dict = Depends(requiere_rol("operacion"))):
    return autonomia.verificar_ejecutadas()


@app.post("/api/acciones/aprobar-varias")
def api_aprobar_varias(body: Nota, u: dict = Depends(requiere_rol("operacion"))):
    return autonomia.aprobar_varias(body.ids or [], u["usuario"], u["rol"])


@app.get("/api/acciones")
def api_acciones(estado: str | None = None, u: dict = Depends(requiere_usuario)):
    return {"acciones": db.acciones(estado=estado, limite=300), "conteo": db.resumen_acciones()}


# ── API: hallazgos / aprendizaje ────────────────────────────────────────────
class Clasificar(BaseModel):
    estado: str
    nota: str = ""


@app.post("/api/hallazgos/{hid}/clasificar")
def api_clasificar(hid: int, body: Clasificar, u: dict = Depends(requiere_rol("operacion"))):
    return consumo.retroalimentar(hid, body.estado, body.nota, u["usuario"])


class Aprendizaje(BaseModel):
    ambito: str = "global"
    clave: str = ""
    nota: str


@app.post("/api/aprendizaje")
def api_aprendizaje(body: Aprendizaje, u: dict = Depends(requiere_rol("operacion"))):
    return {"id": db.agregar_aprendizaje(body.ambito, body.clave, body.nota, usuario=u["usuario"])}


@app.delete("/api/aprendizaje/{aid}")
def api_aprendizaje_borrar(aid: int, u: dict = Depends(requiere_rol("operacion"))):
    db.desactivar_aprendizaje(aid)
    return {"ok": True}


# ── API: copiloto ───────────────────────────────────────────────────────────
class Chat(BaseModel):
    conversacion_id: int | None = None
    texto: str
    contexto: dict | None = None      # {"agente": "consumo"|"demanda"} o {"caso_id": N}: conversar con ese agente


def _conversacion_contextual(usuario: str, contexto: dict) -> int:
    """Una conversación persistente por usuario y contexto (agente, caso o pantalla)."""
    titulo = (f"Caso #{contexto['caso_id']}" if contexto.get("caso_id") else
              f"Agente · {contexto.get('agente')}" if contexto.get("agente") else f"Pantalla · {contexto.get('pagina', 'general')}")
    with db.conn() as con:
        r = con.execute("SELECT id FROM conversaciones WHERE usuario=? AND titulo=? ORDER BY id DESC LIMIT 1", (usuario, titulo)).fetchone()
    return r["id"] if r else db.nueva_conversacion(usuario, titulo)


@app.post("/api/chat")
def api_chat(body: Chat, u: dict = Depends(requiere_usuario)):
    if body.conversacion_id:
        cv = db.conversacion(body.conversacion_id)
        if not cv or cv["usuario"] != u["usuario"]:
            raise HTTPException(403, "Esa conversación no es tuya.")
    if body.contexto and not body.conversacion_id:
        cid = _conversacion_contextual(u["usuario"], body.contexto)
    else:
        cid = body.conversacion_id or db.nueva_conversacion(u["usuario"])
    r = copiloto.responder(cid, body.texto.strip(), u["usuario"], u["rol"], contexto=body.contexto)
    return {"conversacion_id": cid, **r}


@app.get("/api/chat/contexto")
def api_chat_contexto(agente: str | None = None, caso_id: int | None = None, pagina: str | None = None, u: dict = Depends(requiere_usuario)):
    """Historial de la conversación contextual del usuario con un agente, sobre un caso o en una pantalla."""
    contexto = {"caso_id": caso_id} if caso_id else ({"agente": agente} if agente else {"pagina": pagina or "general"})
    cid = _conversacion_contextual(u["usuario"], contexto)
    return {"conversacion_id": cid, "mensajes": [{"rol": m["rol"], "texto": m["texto"]} for m in db.mensajes(cid) if m["texto"]]}


@app.post("/api/conversaciones")
def api_nueva_conv(u: dict = Depends(requiere_usuario)):
    return {"id": db.nueva_conversacion(u["usuario"])}


# ── API: configuración ──────────────────────────────────────────────────────
@app.post("/api/config/probar-odoo")
def api_probar(u: dict = Depends(requiere_rol("operacion"))):
    return odoo_client.get_client().probar()


@app.post("/api/config/descubrir")
def api_descubrir(u: dict = Depends(requiere_rol("operacion"))):
    return schema.descubrir()


@app.post("/api/config/releer-consumo")
def api_releer_consumo(u: dict = Depends(requiere_rol("admin"))):
    """Borra la memoria local del consumo: la siguiente corrida vuelve a bajar todo de Odoo (por ejemplo, tras cambiar
    de base de datos o si se sospecha que la memoria quedó desfasada)."""
    from .odoo import memoria_consumo, queries as _q
    n = memoria_consumo.borrar()
    _q._CONSUMO_CACHE.clear()
    db.log("info", "odoo", "Memoria de consumo borrada: la siguiente corrida lee todo de Odoo", f"{n} archivo(s)", u["usuario"])
    return {"ok": True, "borradas": n}


@app.get("/api/config/memoria-consumo")
def api_memoria_consumo(u: dict = Depends(requiere_rol("operacion"))):
    from .odoo import memoria_consumo
    return memoria_consumo.estado()


@app.get("/api/config/buscar-modelos")
def api_buscar_modelos(q: str = "", u: dict = Depends(requiere_rol("admin"))):
    """Busca en Odoo modelos cuyo nombre técnico o etiqueta contenga el texto (para localizar el módulo de folios de CBH
    cuando su nombre no está entre los candidatos). Sólo lectura de ir.model / ir.model.fields."""
    q = (q or "").strip()
    if len(q) < 3:
        raise HTTPException(400, "Escribe al menos 3 letras.")
    cli = odoo_client.get_client()
    try:
        modelos = cli.search_read("ir.model", ["|", ["model", "ilike", q], ["name", "ilike", q]], ["model", "name", "transient"], limite=40)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"No se pudo leer ir.model con el usuario técnico: {str(e)[:200]}"}
    out = []
    for m in modelos:
        if m.get("transient"):
            continue
        campos = []
        try:
            f = cli.fields_get(m["model"])
            campos = sorted(k for k, v in f.items() if v.get("type") not in ("one2many", "many2many", "binary"))[:60]
            n = cli.search_count(m["model"], [])
        except Exception:  # noqa: BLE001
            n = None
        out.append({"modelo": m["model"], "nombre": m.get("name"), "registros": n, "campos": campos})
    return {"ok": True, "modelos": out}


class Mapeo(BaseModel):
    entidad: str
    modelo: str | None = None
    campos: dict[str, str] | None = None


@app.post("/api/config/mapeo")
def api_mapeo(body: Mapeo, u: dict = Depends(requiere_rol("admin"))):
    return schema.sobreescribir(body.entidad, body.modelo, body.campos)


class Nivel(BaseModel):
    nivel: int


@app.post("/api/config/nivel")
def api_nivel(body: Nivel, u: dict = Depends(requiere_rol("admin"))):
    return {"nivel": autonomia.set_nivel(body.nivel, u["usuario"])}


@app.post("/api/config/politicas")
def api_politicas(body: dict, u: dict = Depends(requiere_rol("admin"))):
    return autonomia.set_politicas(body, u["usuario"])


@app.post("/api/presupuesto-ia")
def api_presupuesto_ia(body: dict, u: dict = Depends(requiere_rol("condor"))):
    """Paquete mensual de tokens (sólo Ingeniería Cóndor): al agotarse, agentes y chat se detienen hasta ampliarlo."""
    try:
        ent = int(float(body.get("tokens_entrada") or 0)); sal = int(float(body.get("tokens_salida") or 0)); aviso = int(float(body.get("aviso_pct") or 80))
    except (TypeError, ValueError):
        raise HTTPException(400, "Valores inválidos.")
    if ent <= 0 or sal <= 0 or not (1 <= aviso <= 99):
        raise HTTPException(400, "Los tokens deben ser mayores que cero y el aviso entre 1 y 99 %.")
    nuevo = {"tokens_entrada": ent, "tokens_salida": sal, "aviso_pct": aviso, "paquete": str(body.get("paquete") or "")[:80],
             "actualizado_por": u["usuario"], "actualizado_en": db.now()}
    db.set_ajuste("presupuesto_ia", nuevo)
    db.log("info", "config", "Presupuesto mensual de IA actualizado", json.dumps(nuevo, ensure_ascii=False), u["usuario"])
    return db.uso_periodo()


@app.get("/api/avisos/destinatarios")
def api_avisos_destinatarios(u: dict = Depends(requiere_rol("admin"))):
    """Quién recibiría hoy un aviso de cada equipo (resuelto en Odoo con la configuración vigente)."""
    from .odoo import acciones as OA
    out = {}
    for eq in OA.EQUIPOS_AVISO:
        try:
            d = OA.destinatarios_equipo(eq)
            out[eq] = {"nombre": d["nombre"], "personas": [{"nombre": p["nombre"], "login": p["login"]} for p in d["personas"]],
                       "fuentes": d["fuentes"], "avisos": d["avisos"]}
        except Exception as e:  # noqa: BLE001
            out[eq] = {"nombre": OA.EQUIPOS_AVISO[eq]["nombre"], "personas": [], "fuentes": [], "avisos": [str(e)[:200]]}
    return out


@app.post("/api/avisos")
def api_avisos(body: dict, u: dict = Depends(requiere_rol("admin"))):
    """Configuración de avisos: logins de Odoo por equipo (coma) y si se incluye a los administradores de Agentes de IA."""
    from .odoo import acciones as OA
    actual = db.get_ajuste("avisos", {}) or {}
    equipos = dict(actual.get("equipos") or {})
    for eq, v in (body.get("equipos") or {}).items():
        if eq not in OA.EQUIPOS_AVISO:
            continue
        raw = v.get("logins") if isinstance(v, dict) else v
        logins = [s.strip().lower() for s in (raw.split(",") if isinstance(raw, str) else (raw or [])) if str(s).strip()]
        equipos[eq] = {**(equipos.get(eq) or {}), "logins": logins}
    nuevo = {"incluir_admin_agentes": bool(body.get("incluir_admin_agentes", actual.get("incluir_admin_agentes", True))), "equipos": equipos}
    db.set_ajuste("avisos", nuevo)
    db.log("info", "config", "Destinatarios de avisos actualizados", json.dumps(nuevo, ensure_ascii=False), u["usuario"])
    return OA.configuracion_avisos()


@app.post("/api/config/agente/{agente}")
def api_config_agente(agente: str, body: dict, u: dict = Depends(requiere_rol("admin"))):
    clave = {"consumo": "agente1_config", "demanda": "agente2_config"}.get(agente)
    if not clave:
        raise HTTPException(404, "Agente desconocido")
    if any(k.startswith("modelo") for k in body) and u["rol"] != "condor":
        raise HTTPException(403, "La configuración del modelo de lenguaje sólo la modifica el equipo de Ingeniería Cóndor.")
    actual = db.get_ajuste(clave, {}) or {}
    actual.update({k: v for k, v in body.items() if v not in (None, "")})
    db.set_ajuste(clave, actual)
    db.log("info", "config", f"Parámetros de {agente} actualizados", json.dumps(body, ensure_ascii=False), u["usuario"])
    return actual


@app.post("/api/config/regiones")
def api_regiones(body: dict, u: dict = Depends(requiere_rol("admin"))):
    """Mapa ubicación/prefijo → región, para que el rebalanceo prefiera fuentes cercanas."""
    limpio = {str(k).strip(): str(v).strip() for k, v in body.items() if str(k).strip() and str(v).strip()}
    db.set_ajuste("regiones", limpio)
    db.log("info", "config", "Regiones actualizadas", json.dumps(limpio, ensure_ascii=False), u["usuario"])
    return limpio


class NuevoUsuario(BaseModel):
    usuario: str
    password: str
    nombre: str = ""
    rol: str = "consulta"


@app.post("/api/usuarios")
def api_usuario(body: NuevoUsuario, u: dict = Depends(requiere_rol("admin"))):
    if body.rol == "condor" and u["rol"] != "condor":
        raise HTTPException(403, "Sólo el equipo de Ingeniería Cóndor puede crear usuarios condor.")
    if body.rol not in ("consulta", "operacion", "admin", "condor"):
        raise HTTPException(400, "Rol inválido")
    if len(body.password or "") < settings.PASSWORD_MIN or body.password.lower() in (body.usuario.lower(), "password", "contraseña", "12345678", "admin1234"):
        raise HTTPException(400, f"La contraseña debe tener al menos {settings.PASSWORD_MIN} caracteres y no ser trivial.")
    try:
        return {"id": db.crear_usuario(body.usuario, body.password, body.nombre, body.rol)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"No se pudo crear: {e}")


@app.post("/api/usuarios/{uid}/desactivar")
def api_usuario_off(uid: int, u: dict = Depends(requiere_rol("admin"))):
    with db.conn() as con:
        obj = con.execute("SELECT rol FROM usuarios WHERE id=?", (uid,)).fetchone()
        if obj and obj["rol"] == "condor" and u["rol"] != "condor":
            raise HTTPException(403, "Sólo el equipo de Cóndor puede desactivar usuarios condor.")
        con.execute("UPDATE usuarios SET activo=0 WHERE id=? AND id<>?", (uid, u["id"]))
    return {"ok": True}


@app.get("/api/reportes")
def api_reportes(u: dict = Depends(requiere_usuario)):
    return {"reportes": db.reportes(100)}


@app.get("/api/respaldo")
def api_respaldo(u: dict = Depends(requiere_rol("condor"))):
    """Descarga una copia consistente de la base (bitácora, aprendizaje, hallazgos, acciones)."""
    import sqlite3
    destino = settings.DATA_DIR / "respaldo_cbh_agentes.db"
    src = sqlite3.connect(settings.DB_PATH)
    dst = sqlite3.connect(destino)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    db.log("info", "sistema", "Respaldo de base descargado", usuario=u["usuario"])
    return FileResponse(destino, filename=f"cbh_agentes_{time.strftime('%Y%m%d')}.db", media_type="application/octet-stream")
