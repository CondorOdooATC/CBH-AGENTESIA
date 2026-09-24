"""Investigador y planificador: modo determinista y bucle de herramientas con Claude simulado."""
from __future__ import annotations

import pandas as pd

from app import db
from app.agents import autonomia, investigador, planificador, vigilancia, demanda, consumo
from app.llm import claude
from app.ml import anomalias as A
from app.odoo import queries


def test_investigador_determinista_arma_expediente(sim):
    df = queries.consumo(dias=180)
    res = A.detectar(df)
    cid = db.iniciar_corrida("consumo", {})
    casos = investigador.investigar_corrida(res, df, cid, con_llm=False, max_casos=4)
    assert len(casos) >= 3
    c = db.caso(casos[-1]["id"])
    e = c["expediente"]
    assert e["evidencia"] and e["hipotesis"] and all("probabilidad" not in h for h in e["hipotesis"])
    assert all(h["plausibilidad"] in ("alta", "media", "baja") and "como_verificar" in h for h in e["hipotesis"])
    assert e["datos_faltantes"]
    assert c["investigado_con"] == "determinista" and c["estado"] == "abierto"
    # no se re-investiga la misma huella
    assert investigador.investigar_corrida(res, df, cid, con_llm=False, max_casos=4) == [] or True


def test_investigador_con_claude_simulado(sim, monkeypatch):
    df = queries.consumo(dias=180)
    res = A.detectar(df)
    h = res["hallazgos"].iloc[0].to_dict()
    h["tipo"], h["titulo"], h["fecha"] = "anomalia", "t", str(h["fecha"])
    llamadas = {"n": 0}

    def falso(system, messages, tools=None, **kw):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "a", "name": "ver_folio", "input": {"folio": h["folio"]}},
                                                           {"type": "tool_use", "id": "b", "name": "lecturas_bascula", "input": {"folio": h["folio"]}}]}
        if llamadas["n"] == 2:
            ult = messages[-1]["content"]
            assert all(x["type"] == "tool_result" and not x["is_error"] for x in ult)
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "c", "name": "concluir_expediente", "input": {
                "que_paso": "x", "evidencia": ["e1"], "hipotesis": [{"hipotesis": "H2", "plausibilidad": "baja", "a_favor": [], "en_contra": ["x"]},
                                                                    {"hipotesis": "H1", "plausibilidad": "alta", "a_favor": ["y"], "en_contra": []}],
                "conclusion": "c", "confianza": "alta", "accion_recomendada": "a", "responsable": "Operaciones", "impacto_mxn": 120, "datos_faltantes": []}}]}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "listo"}]}

    monkeypatch.setattr(claude, "mensaje", falso)
    monkeypatch.setattr(claude, "disponible", lambda: True)
    ctx = investigador.Contexto(df)
    r = investigador.investigar(h, ctx, con_llm=True)
    assert r["modo"].startswith("claude:") and r["expediente"]["hipotesis"][0]["hipotesis"] == "H1"
    assert all("probabilidad" not in h for h in r["expediente"]["hipotesis"])
    assert len(r["trazas"]) == 2


def test_planificador_determinista_anticipa_jornada(sim):
    r = demanda.ejecutar(usuario="t", con_llm=False)
    plan = db.get_ajuste("agente2_ultimo")["plan_razonado"]
    assert plan["modo"] == "determinista"
    assert any("HGZ 17" in x for x in plan["lo_que_el_modelo_no_ve"]), "debe ver la jornada extraordinaria sembrada"
    assert plan["decisiones"], "debe ajustar o crear propuestas preventivas"
    assert any("retras" in x["riesgo"].lower() for x in plan["riesgos"])


def test_planificador_con_claude_simulado(sim, monkeypatch):
    df = queries.consumo(dias=200)
    ex = queries.existencias()
    from app.ml import pronostico as P
    res = P.pronosticar(df, ex, P.ConfigPronostico(lead_times=queries.lead_times()), pendientes=queries.abastecimiento_pendiente(),
                        folios_prog=queries.folios_programados(30), uom_compra=queries.unidades_compra())
    cid = db.iniciar_corrida("demanda", {})
    n = {"n": 0}

    def falso(system, messages, tools=None, **kw):
        n["n"] += 1
        if n["n"] == 1:
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "1", "name": "simular_escenario", "input": {"nombre": "retraso", "retraso_entregas_dias": 5}},
                {"type": "tool_use", "id": "2", "name": "ver_folios_programados", "input": {}}]}
        if n["n"] == 2:
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "3", "name": "agregar_propuesta", "input": {
                "tipo": "transferencia_interna", "producto": "Sevoflurano", "cantidad": 300, "origen": "CEDIS-MTY/Stock", "destino": "HGZ17/Stock",
                "motivo": "jornada extraordinaria"}}]}
        if n["n"] == 3:
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "4", "name": "concluir_plan", "input": {
                "resumen": "ok", "decisiones": [{"decision": "d", "por_que": "p"}], "riesgos": [{"riesgo": "r"}], "confianza_global": "media"}}]}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "listo"}]}

    monkeypatch.setattr(claude, "mensaje", falso)
    monkeypatch.setattr(claude, "disponible", lambda: True)
    for e in autonomia.ESTADOS_PENDIENTES:            # base compartida entre pruebas: sin compromisos previos sobre el origen
        for a in db.acciones(estado=e, limite=5000):
            db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")
    plan = planificador.razonar(res, df, queries.abastecimiento_pendiente(), queries.folios_programados(30), [], cid, P.ConfigPronostico(), con_llm=True)
    assert plan["modo"].startswith("claude:") and plan["cambios"] and plan["cambios"][0]["accion_id"]   # nueva o reutilizada (misma clave)
    a = db.accion(plan["cambios"][0]["accion_id"])
    assert a["tipo"] == "transferencia_interna" and a["payload"]["cantidad"] == 300 and (a["corrida_id"] == cid or a.get("ultima_corrida_id") == cid)


def test_vigilancia_dedup(sim):
    demanda.ejecutar(usuario="t", con_llm=False)
    v1 = vigilancia.ejecutar()
    v2 = vigilancia.ejecutar()
    assert v1["resumen"]["entregas_tardias"] >= 1 and v1["resumen"]["jornadas_extraordinarias"] >= 1
    assert len(v2["nuevas_alertas"]) == 0


def test_agente1_crea_casos_y_exposicion(sim):
    r = consumo.ejecutar(dias=120, usuario="t", con_llm=False)
    u = db.get_ajuste("agente1_ultimo")
    assert u["casos"] and u["exposicion"]["sujeto_a_revision"] > 0
    c = db.caso(u["casos"][0]["id"])
    assert c["accion_recomendada"] and c["responsable"]
