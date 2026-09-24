"""Pruebas end-to-end sobre el Odoo simulado (anomalías sembradas con respuesta conocida)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app import db
from app.odoo import queries, schema
from app.ml import anomalias as A, pronostico as P
from app.agents import autonomia, consumo, demanda, briefing
from app.llm import tools, claude
from app.reports import builders


# ── mapeo ───────────────────────────────────────────────────────────────────
def test_descubrimiento_encuentra_modelos_custom(sim):
    m = schema.cargar()
    assert m["entidades"]["consumo"]["modelo"] == "cbh.operacion.medica.line"
    assert m["entidades"]["consumo"]["campos"]["cantidad"] == "cantidad"
    assert m["entidades"]["consumo"]["campos"]["peso_inicial"] == "peso_inicial"
    assert not m["usa_respaldo"]


def test_respaldo_stock_move_line(sim):
    """Si se pierde el modelo custom, los agentes siguen operando sobre stock.move.line."""
    original = schema.cargar()
    try:
        schema.sobreescribir("consumo", "stock.move.line", {})
        m = schema.cargar()
        m["entidades"]["consumo"]["campos"] = {}
        schema.guardar(m)
        df = queries.consumo(dias=30)
        assert len(df) > 1000 and df["origen_datos"].iloc[0] == "stock.move.line"
    finally:
        schema.guardar(original)


# ── Agente 1 ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def deteccion(sim):
    df = queries.consumo(dias=180)
    return A.detectar(df), df


def test_anomalias_sembradas_detectadas(sim, deteccion):
    res, _ = deteccion
    h = res["hallazgos"]
    ids = set(h["linea_id"].dropna().astype(int))
    sem = sim.anomalias_sembradas
    assert len(set(sem["consumo_sin_cirugia"]) & ids) >= 8
    assert len(set(sem["bascula_imposible"]) & ids) == 4
    assert len(set(sem["bascula_discrepancia"]) & ids) >= 8
    assert len(set(sem["duplicado"]) & ids) == 5
    assert len(set(sem["excede_envase"]) & ids) == 3
    assert len(set(sem["lote_caducado"]) & ids) == 3
    assert len(set(sem["tasa_clinica"]) & ids) >= 3
    # lote viajero: sólo la mitad (las líneas en HGZ 33, unidad ajena al lote) deben marcarse
    assert 2 <= len(set(sem["lote_viajero"]) & ids) <= 4


def test_precision_razonable(sim, deteccion):
    res, df = deteccion
    h = res["hallazgos"]
    assert len(h) < 0.01 * len(df), "demasiados hallazgos: el motor es ruidoso"
    assert (h["severidad"] == "critica").sum() >= 20


def test_patrones_agregados(sim, deteccion):
    res, _ = deteccion
    ag = res["agregados"]
    reglas = set(zip(ag["regla"], ag["clave"]))
    assert ("R11_ACTOR_DESVIADO", "Roberto Cadena") in reglas
    assert any(r == "R10_CAMBIO_NIVEL" and "LR-QX2" in c for r, c in reglas)
    top_aux = res["riesgos"]["auxiliar"][0]["clave"]
    assert top_aux == "Roberto Cadena"
    assert res["riesgos"]["hospital"][0]["clave"] == "HGZ 17 Monterrey"


def test_historico(sim, deteccion):
    res, _ = deteccion
    hist = res["historico"]
    assert len(hist["mensual"]) >= 6 and hist["tendencias_producto"]
    assert set(hist["estacionalidad_semana"]) == {"Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"}


def test_agente1_corrida_y_aprendizaje(sim):
    r = consumo.ejecutar(dias=120, usuario="test", con_llm=False)
    assert r["kpis"]["hallazgos"] > 0 and r["reporte"]["archivo"].endswith(".xlsx")
    assert db.ultima_corrida("consumo")["estado"] == "ok"
    an = (db.anomalias(corrida_id=r["corrida_id"], limite=1) or db.anomalias(estado="nueva", limite=1))[0]
    r_fb = consumo.retroalimentar(an["id"], "justificada", "cirugía larga documentada", "test")
    assert r_fb["delta_umbral"] > 0, "justificar debe RELAJAR el umbral (sumar)"
    perf = db.perfiles("producto")
    assert any(v["ajuste_umbral"] > 0 for v in perf.values())
    # repetir la misma clasificación no vuelve a mover el umbral
    assert consumo.retroalimentar(an["id"], "justificada", "", "test")["delta_umbral"] == 0
    # confirmar deshace lo anterior y endurece
    assert consumo.retroalimentar(an["id"], "confirmada", "", "test")["delta_umbral"] < 0
    # una huella justificada no vuelve a reportarse
    r2 = consumo.ejecutar(dias=120, usuario="test", con_llm=False, generar_excel=False, proponer_acciones=False)
    huellas = {a["huella"] for a in db.anomalias(corrida_id=r2["corrida_id"], limite=5000)}
    assert an["huella"] not in huellas


# ── Agente 2 ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pron(sim):
    df = queries.consumo(dias=540)
    ex = queries.existencias()
    return P.pronosticar(df, ex, P.ConfigPronostico(lead_times=queries.lead_times(),
                                                    regiones={"LR": "CDMX", "HGZ": "MTY", "CEDIS-MTY": "MTY", "CEDIS-LR": "CDMX"}))


def test_pronostico_detecta_desabasto_sembrado(sim, pron):
    r = pron["resurtido"]
    fila = r[(r["producto_id"] == 1005) & (r["almacen"] == "HGZ17/Stock")].iloc[0]
    assert fila["criticidad"] in ("critico", "desabasto")
    assert fila["sugerido"] > 0
    red = r[(r["nivel"] == "red")]
    assert len(red) >= 10 and (red["sugerido"] >= 0).all()


def test_rebalanceo_prefiere_misma_region(sim, pron):
    rb = pron["rebalanceo"]
    assert not rb.empty
    lr = rb[rb["destino"].str.startswith("LR-")]
    # la misma región va primero; otra región sólo complementa cuando el origen regional ya cedió todo lo que podía
    assert (lr["origen"].str.contains("LR")).mean() >= 0.8
    for pid, g in lr.groupby("producto_id"):
        misma = g["origen"].str.contains("LR")
        if not misma.all():
            cedido = g[misma]
            assert cedido.empty or (cedido["cantidad"] >= cedido["disponible_origen"] - 1e-6).any(), \
                f"producto {pid}: se cruzó de región sin agotar el CEDIS de la misma región"


def test_backtest_wape_razonable(sim, pron):
    r = pron["resurtido"]
    loc = r[(r["nivel"] == "local") & r["mape"].notna()]
    assert loc["mape"].median() < 40


def test_caducidades_y_abc(sim, pron):
    assert not pron["caducidades"].empty and (pron["caducidades"]["en_riesgo"] >= 0).all()
    abc = pron["abc_xyz"]
    assert set(abc["abc"]) <= {"A", "B", "C"} and (abc["abc"] == "A").sum() >= 3


def test_agente2_corrida_propone_acciones(sim):
    autonomia.set_nivel(1)
    r = demanda.ejecutar(usuario="test", con_llm=False)
    assert r["kpis"]["combinaciones"] > 100
    tipos = {a.get("estado") for a in r["acciones"]}
    assert "propuesta" in tipos
    assert db.resumen_acciones().get("propuesta", 0) > 0


# ── Autonomía ───────────────────────────────────────────────────────────────
def test_flujo_aprobacion_ejecuta_y_revierte(sim):
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": False})
    n0 = len(sim.tablas["stock.picking"])
    r = autonomia.proponer("demanda", "transferencia_interna", "t", {"producto_id": 1004, "cantidad": 10, "origen_id": 507, "destino_id": 505},
                           impacto={"cantidad": 10, "importe": 380}, sincronizar=False)
    assert r["estado"] == "propuesta"
    a = autonomia.aprobar(r["id"], "tester", "admin")
    assert a["estado"] == "ejecutada" and a["odoo"]["modelo"] == "stock.picking"
    assert len(sim.tablas["stock.picking"]) == n0 + 1
    assert autonomia.revertir(r["id"], "tester")["estado"] == "revertida"


def test_politicas_bloquean(sim):
    r = autonomia.proponer("demanda", "transferencia_interna", "t", {"producto_id": 1004, "cantidad": 999999, "origen_id": 507, "destino_id": 505},
                           impacto={"cantidad": 999999}, sincronizar=False)
    assert r["estado"] == "bloqueada"
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": False})
    r2 = autonomia.proponer("demanda", "solicitud_compra", "t", {"producto_id": 1004, "cantidad": 10}, impacto={"importe": 80000}, sincronizar=False)
    assert r2["riesgo"] == "alto"
    assert autonomia.aprobar(r2["id"], "op", "operacion")["estado"] == "propuesta"  # sólo admin
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": True})


def test_nivel_cero_no_propone(sim):
    autonomia.set_nivel(0)
    try:
        assert autonomia.proponer("consumo", "alerta", "x", {}, sincronizar=False)["estado"] == "omitida"
    finally:
        autonomia.set_nivel(1)


# ── Herramientas del copiloto y bucle agéntico ──────────────────────────────
def test_herramientas(sim):
    r = tools.ejecutar("consultar_consumo", {"dias": 30, "agrupar_por": ["hospital"]}, "t", "admin")
    assert r["total_filas"] == 6
    r = tools.ejecutar("excel_desde_consulta", {"fuente": "consumo", "titulo": "Kardex", "parametros": {"dias": 30, "agrupar_por": ["producto", "subalmacen"]}}, "t", "admin")
    assert r["archivo"].endswith(".xlsx") and r["filas"] > 50
    r = tools.ejecutar("buscar", {"tipo": "ubicacion", "texto": "HGZ17"}, "t", "admin")
    assert r["filas"][0]["id"] == 503
    r = tools.ejecutar("proponer_accion", {"tipo": "transferencia_interna", "titulo": "x", "payload": {"producto": "Fentanilo", "cantidad": 5, "origen": "CEDIS-MTY/Stock", "destino": "HGZ17/Stock"}}, "t", "admin")
    assert r["estado"] in ("propuesta", "actualizada", "vigente") and r["id"]
    a = db.accion(r["id"])
    assert a["payload"]["unidad"] and a["payload"]["producto"] and "Fentanilo" in a["titulo"] and a["clave"].startswith("transferencia_interna|1005")


def test_bucle_herramientas_con_llm_simulado(sim, monkeypatch):
    """Simula dos turnos de Claude: primero pide una herramienta, luego responde con texto."""
    llamadas = {"n": 0}

    def falso_mensaje(system, messages, tools=None, **kw):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Consulto…"},
                {"type": "tool_use", "id": "tu_1", "name": "consultar_existencias", "input": {"filtro_texto": "fentanilo", "agrupar_por": ["ubicacion"]}}]}
        ultimo = messages[-1]["content"]
        assert ultimo[0]["type"] == "tool_result" and "HGZ17" in ultimo[0]["content"]
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Fentanilo en HGZ17: crítico."}]}

    monkeypatch.setattr(claude, "mensaje", falso_mensaje)
    r = claude.bucle_herramientas("s", [{"role": "user", "content": "¿cuánto fentanilo hay?"}], tools.HERRAMIENTAS,
                                  lambda n, a: tools.ejecutar(n, a, "t", "admin"))
    assert r["texto"].startswith("Fentanilo") and r["herramientas"][0]["ok"] and r["iteraciones"] == 2


# ── Reportes y briefing ─────────────────────────────────────────────────────
def test_excel_libre_y_briefing(sim):
    r = builders.reporte_libre("Prueba", [{"nombre": "Datos", "columnas": ["a", "b"], "filas": [[1, 2.5], [3, 4.0]], "totales": ["b"]}])
    import openpyxl
    wb = openpyxl.load_workbook(r["ruta"])
    assert wb.sheetnames == ["Portada", "Acerca de", "Datos"]
    b = briefing.generar("test", con_llm=False)
    assert "Briefing" in b["texto"] and b["pendientes"] >= 0


# ── API HTTP ────────────────────────────────────────────────────────────────
def test_api_completa(sim):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/health").json()["ok"]
        assert c.get("/").status_code in (307, 200)  # redirige a login
        r = c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        for p in ("/", "/agentes/consumo", "/agentes/demanda", "/hallazgos", "/acciones", "/copiloto", "/reportes", "/configuracion", "/bitacora"):
            assert c.get(p).status_code == 200, p
        e = c.get("/api/estado").json()
        assert e["odoo"]["ok"] and "autonomia" in e
        a = c.get("/api/acciones?estado=propuesta").json()["acciones"]
        if a:
            aid = a[0]["id"]
            assert c.post(f"/api/acciones/{aid}/rechazar", json={"nota": "prueba"}).json()["estado"] == "rechazada"
        ch = c.post("/api/chat", json={"texto": "estado"}).json()
        assert "conversacion_id" in ch and ch["texto"]
        assert c.post("/api/aprendizaje", json={"ambito": "hospital", "clave": "HGZ La Raza", "nota": "sábados normales"}).json()["id"]
        rep = c.get("/api/reportes").json()["reportes"]
        if not rep:
            builders.reporte_libre("Prueba API", [{"nombre": "Datos", "columnas": ["a"], "filas": [[1]]}])
            rep = c.get("/api/reportes").json()["reportes"]
        assert c.get(f"/reportes/{rep[0]['id']}/descargar").status_code == 200
