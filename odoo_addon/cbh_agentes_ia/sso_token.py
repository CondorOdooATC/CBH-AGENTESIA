# -*- coding: utf-8 -*-
"""Token de acceso firmado (misma definición que app/sso.py en la plataforma). Sin dependencias de Odoo."""
import base64
import hashlib
import hmac
import json
import secrets
import time

VIGENCIA_S = 60


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def firmar(secreto: str, payload: dict) -> str:
    cuerpo = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode())
    firma = _b64e(hmac.new(secreto.encode(), cuerpo.encode(), hashlib.sha256).digest())
    return f"{cuerpo}.{firma}"


def token_para(secreto: str, db: str, login: str, nombre: str, correo: str, rol: str, uid: int,
               fin: str = "sso", embed: bool = False, ahora: float = None) -> str:
    ahora = time.time() if ahora is None else ahora
    payload = {"v": 1, "db": db, "login": login, "nombre": nombre or login, "correo": correo or "", "rol": rol, "uid": int(uid),
               "iat": int(ahora), "exp": int(ahora) + VIGENCIA_S, "jti": secrets.token_hex(12), "fin": fin, "embed": bool(embed)}
    return firmar(secreto, payload)
