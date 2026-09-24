"""Acceso desde Odoo (SSO) y protecciones de datos: el token que firma el addon de Odoo lo acepta la plataforma,
con todas sus reglas (caducidad, base, repetición, rol tope, sin suplantar usuarios locales), y las peticiones que
cambian estado exigen mismo origen (CSRF)."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, sso
from app.config import settings

ADDON = Path(__file__).resolve().parents[1] / "odoo_addon" / "cbh_agentes_ia" / "sso_token.py"
spec = importlib.util.spec_from_file_location("sso_token_addon", ADDON)
sso_token = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sso_token)

SECRETO = "secreto-de-prueba-compartido-con-odoo"


@pytest.fixture(autouse=True)
def _sso_conf(monkeypatch):
    monkeypatch.setattr(settings, "SSO_SECRET", SECRETO)
    monkeypatch.setattr(settings, "SSO_DB", "cbhtest")
    monkeypatch.setattr(settings, "ODOO_URL", "https://cbhtest.odoo.com")
    yield


def _tok(rol="operacion", login="jlopez@cbh.mx", **kw):
    return sso_token.token_para(SECRETO, "cbhtest", login, "Juan López", login, rol, 42, **kw)


def test_token_del_addon_entra_y_asigna_rol(sim):
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/sso", params={"token": _tok()}, follow_redirects=False)
        assert r.status_code == 303 and "cbh_sesion" in r.headers.get("set-cookie", "")
        assert c.get("/").status_code == 200                    # ya con sesión
    u = db.usuario_por_token(r.cookies.get("cbh_sesion"))
    assert u and u["usuario"] == "jlopez@cbh.mx" and u["rol"] == "operacion" and u["origen"] == "odoo"
    # el rol se sincroniza en cada entrada (le quitaron Operación en Odoo)
    with TestClient(app) as c:
        c.get("/sso", params={"token": _tok(rol="consulta")}, follow_redirects=False)
    with db.conn() as con:
        assert con.execute("SELECT rol FROM usuarios WHERE usuario='jlopez@cbh.mx'").fetchone()["rol"] == "consulta"
    # un usuario de Odoo no entra por el formulario con contraseña
    assert db.verificar_credenciales("jlopez@cbh.mx", "cualquiera") is None


def test_token_rechazos(sim):
    # firma con otro secreto
    malo = sso_token.token_para("otro-secreto", "cbhtest", "x@x.mx", "X", "x@x.mx", "admin", 1)
    with pytest.raises(ValueError, match="secreto"):
        sso.verificar(malo)
    # caducado
    viejo = _tok(ahora=time.time() - 400)
    with pytest.raises(ValueError, match="caducó"):
        sso.verificar(viejo)
    # base distinta
    otra = sso_token.token_para(SECRETO, "produccion", "x@x.mx", "X", "x@x.mx", "admin", 1)
    with pytest.raises(ValueError, match="base"):
        sso.verificar(otra)
    # rol no permitido (condor jamás por SSO; sin grupo tampoco)
    for rol in ("condor", "", "superadmin"):
        with pytest.raises(ValueError):
            sso.verificar(sso_token.token_para(SECRETO, "cbhtest", "x@x.mx", "X", "x@x.mx", rol, 1))
    # repetición: el mismo enlace no se usa dos veces
    t = _tok()
    sso.verificar(t)
    with pytest.raises(ValueError, match="ya se usó"):
        sso.verificar(t)
    # un token de verificación no abre sesión
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/sso", params={"token": _tok(fin="verificar")}, follow_redirects=False)
        assert r.status_code == 403 and "cbh_sesion" not in r.headers.get("set-cookie", "")
        assert c.get("/sso/verificar", params={"token": _tok()}).status_code == 400
        assert c.get("/sso/verificar", params={"token": _tok(fin="verificar")}).json()["ok"] is True


def test_sso_no_suplanta_usuario_local(sim):
    db.crear_usuario("mlocal", "test12345678", "María local", "admin")
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/sso", params={"token": _tok(login="mlocal", rol="admin")}, follow_redirects=False)
        assert r.status_code == 403 and "cuenta local" in r.text
    with db.conn() as con:
        assert con.execute("SELECT origen FROM usuarios WHERE usuario='mlocal'").fetchone()["origen"] in ("local", None)


def test_sso_nunca_eleva_a_condor(sim):
    from app.main import app
    with TestClient(app) as c:
        c.get("/sso", params={"token": _tok(login="cond@cbh.mx", rol="admin")}, follow_redirects=False)
    with db.conn() as con:
        con.execute("UPDATE usuarios SET rol='condor' WHERE usuario='cond@cbh.mx'")   # Cóndor lo promovió a mano
    with TestClient(app) as c:
        c.get("/sso", params={"token": _tok(login="cond@cbh.mx", rol="consulta")}, follow_redirects=False)
    with db.conn() as con:
        assert con.execute("SELECT rol FROM usuarios WHERE usuario='cond@cbh.mx'").fetchone()["rol"] == "condor"   # no se degrada por SSO


def test_csrf_y_cabeceras(sim):
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        # misma página: permitido
        assert c.post("/api/conversaciones", headers={"sec-fetch-site": "same-origin"}).status_code == 200
        # desde otro sitio: rechazado aunque lleve la cookie
        assert c.post("/api/conversaciones", headers={"sec-fetch-site": "cross-site", "origin": "https://malo.example"}).status_code == 403
        assert c.post("/api/conversaciones", headers={"origin": "https://malo.example"}).status_code == 403
        r = c.get("/")
        assert "frame-ancestors" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"]


def test_limite_de_intentos_de_acceso(sim, monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "LOGIN_MAX_INTENTOS", 3)
    db.limpiar_intentos("u:nadie"); db.limpiar_intentos("ip:testclient")
    with TestClient(app) as c:
        for _ in range(3):
            assert c.post("/login", data={"usuario": "nadie", "password": "mal", "next": "/"}, follow_redirects=False).status_code == 401
        assert c.post("/login", data={"usuario": "nadie", "password": "mal", "next": "/"}, follow_redirects=False).status_code == 429
    db.limpiar_intentos("u:nadie"); db.limpiar_intentos("ip:testclient")


def test_respaldo_solo_condor_y_contrasena_minima(sim):
    from app.main import app
    db.crear_usuario("adm_seg", "test12345678", "Admin", "admin")
    with TestClient(app) as c:
        c.post("/login", data={"usuario": "adm_seg", "password": "test12345678", "next": "/"}, follow_redirects=False)
        assert c.get("/api/respaldo").status_code == 403
        assert c.post("/api/usuarios", json={"usuario": "corta", "password": "abc", "nombre": "x", "rol": "consulta"}).status_code == 400
