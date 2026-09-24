"""Autenticación por cookie de sesión y control de roles."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from . import db

COOKIE = "cbh_sesion"
ROLES = {"condor": 4, "admin": 3, "operacion": 2, "consulta": 1}
# condor = equipo de Ingeniería Cóndor: único rol que puede cambiar la configuración del modelo de lenguaje y crear otros condor


def usuario_actual(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE) or request.headers.get("X-Token", "")
    return db.usuario_por_token(token)


def requiere_usuario(request: Request) -> dict:
    u = usuario_actual(request)
    if not u:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión requerida")
        raise HTTPException(status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": f"/login?next={request.url.path}"})
    return u


def requiere_rol(minimo: str):
    def _dep(u: dict = Depends(requiere_usuario)) -> dict:
        if ROLES.get(u["rol"], 0) < ROLES[minimo]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Se requiere rol {minimo}")
        return u
    return _dep


def redirigir_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=307)
