"""Inicio de sesión desde Odoo (SSO) con token firmado, y protecciones de datos de la plataforma.

Token (lo genera el addon `cbh_agentes_ia` instalado en Odoo; el formato es idéntico en `odoo_addon/cbh_agentes_ia/sso_token.py`):

    base64url(JSON del payload) + "." + base64url(HMAC-SHA256(secreto, base64url(JSON)))

Payload: {"v": 1, "db": base de Odoo, "login": login del usuario, "nombre": ..., "correo": ..., "rol": consulta|operacion|admin,
          "uid": id en Odoo, "iat": epoch, "exp": epoch (60 s), "jti": nonce, "fin": "sso"|"verificar", "embed": bool}

Reglas de seguridad:
  • el secreto vive sólo en variables de entorno (Render) y en un parámetro de sistema de Odoo (sólo administradores);
  • el token caduca en 60 s, se usa una sola vez (anti-repetición por `jti`) y debe venir de la base configurada;
  • el rol nunca puede ser «condor»: ese rol sólo lo asigna Ingeniería Cóndor desde la plataforma;
  • un usuario SSO nunca suplanta a un usuario local: los usuarios SSO llevan origen «odoo» y se identifican por su login de
    Odoo; si existe un usuario local con el mismo nombre, se rechaza con un mensaje claro;
  • la sesión SSO dura menos (12 h) para que un cambio de grupos en Odoo se refleje pronto.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time

from . import db
from .config import settings

VIGENCIA_S = 90            # tolerancia total (60 s de vigencia + desfase de reloj)
ROLES_SSO = {"consulta", "operacion", "admin"}
_usados: dict[str, float] = {}
_lock = threading.Lock()


def _b64d(s: str) -> bytes:
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def firmar(secreto: str, payload: dict) -> str:
    """Genera un token (sólo se usa en pruebas y en la verificación de instalación; Odoo trae su propia copia)."""
    cuerpo = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode())
    firma = _b64e(hmac.new(secreto.encode(), cuerpo.encode(), hashlib.sha256).digest())
    return f"{cuerpo}.{firma}"


def verificar(token: str, secreto: str | None = None, ahora: float | None = None, base_esperada: str | None = None,
              consumir: bool = True) -> dict:
    """Valida firma, vigencia, base y repetición. Devuelve el payload o lanza ValueError con un motivo legible."""
    secreto = secreto if secreto is not None else settings.SSO_SECRET
    if not secreto:
        raise ValueError("El acceso desde Odoo no está configurado en la plataforma (falta SSO_SECRET).")
    if not token or token.count(".") != 1:
        raise ValueError("Token inválido.")
    cuerpo, firma = token.split(".", 1)
    esperada = _b64e(hmac.new(secreto.encode(), cuerpo.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(esperada, firma):
        raise ValueError("Firma inválida: el secreto de Odoo no coincide con el de la plataforma.")
    try:
        p = json.loads(_b64d(cuerpo))
    except Exception as e:  # noqa: BLE001
        raise ValueError("Token ilegible.") from e
    if p.get("v") != 1:
        raise ValueError("Versión de token no soportada.")
    ahora = time.time() if ahora is None else ahora
    if not isinstance(p.get("exp"), (int, float)) or ahora > float(p["exp"]) or ahora > float(p.get("iat", 0)) + VIGENCIA_S:
        raise ValueError("El enlace caducó: vuelve a abrir Agentes de IA desde Odoo.")
    if float(p.get("iat", 0)) > ahora + 30:
        raise ValueError("Token con fecha futura (reloj desfasado).")
    base = base_esperada if base_esperada is not None else (settings.SSO_DB or settings.ODOO_DB)
    if base and str(p.get("db")) != base:
        raise ValueError(f"El token viene de la base «{p.get('db')}», pero esta plataforma está conectada a «{base}».")
    if not p.get("login") or not p.get("jti"):
        raise ValueError("Token incompleto.")
    if str(p.get("rol")) not in ROLES_SSO:
        raise ValueError("El usuario no tiene un grupo de Agentes de IA en Odoo (Consulta, Operación o Administrador).")
    if consumir:
        with _lock:
            for k, t in list(_usados.items()):
                if t < ahora - 2 * VIGENCIA_S:
                    _usados.pop(k, None)
            if p["jti"] in _usados:
                raise ValueError("Este enlace ya se usó; vuelve a abrir Agentes de IA desde Odoo.")
            _usados[p["jti"]] = ahora
    return p


def entrar(payload: dict) -> dict:
    """Crea o actualiza el usuario de origen Odoo y devuelve el registro (sin sesión)."""
    login = str(payload["login"]).strip().lower()
    with db.conn() as con:
        u = con.execute("SELECT * FROM usuarios WHERE usuario=?", (login,)).fetchone()
        if u and (u["origen"] or "local") != "odoo":
            raise ValueError(f"El usuario «{login}» existe como cuenta local de la plataforma; pide a Ingeniería Cóndor renombrarla "
                             "o usa otro login en Odoo.")
        rol = str(payload.get("rol"))
        if u:
            nuevo_rol = u["rol"] if u["rol"] == "condor" else rol   # condor nunca se otorga ni se quita por SSO
            con.execute("UPDATE usuarios SET nombre=?, rol=?, activo=1, odoo_uid=?, ultimo_sso=? WHERE id=?",
                        (payload.get("nombre") or u["nombre"], nuevo_rol, payload.get("uid"), db.now(), u["id"]))
            uid = u["id"]
        else:
            salt = secrets.token_hex(16)
            uid = con.execute(
                "INSERT INTO usuarios (usuario, nombre, password_hash, salt, rol, creado_en, origen, odoo_uid, ultimo_sso) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (login, payload.get("nombre") or login, db._hash(secrets.token_urlsafe(32), salt), salt, rol, db.now(), "odoo",
                 payload.get("uid"), db.now())).lastrowid
        r = con.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    return dict(r)
