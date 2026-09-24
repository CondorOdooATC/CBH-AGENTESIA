"""Avisos a personas en Odoo (el cliente no usa Helpdesk): los destinatarios salen de los grupos de Odoo del equipo más los
administradores de Agentes de IA y los logins configurados; al ejecutar se crea una actividad «por hacer» por persona y
una nota en el chatter que las notifica; el seguimiento cuenta cuántas atendieron; se puede retirar; nunca se avisa
al usuario técnico; sin destinatarios la propuesta lo dice y la ejecución no inventa."""
from __future__ import annotations

import json

from app import db
from app.agents import autonomia, investigador
from app.odoo import acciones as OA


def _limpiar(agente=None):
    for e in autonomia.ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, agente=agente, limite=5000):
            db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")


def test_destinatarios_por_grupo_y_logins(sim, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ODOO_USER", "agente.ia@i-condor.com")
    db.set_ajuste("avisos", {})
    d = OA.destinatarios_equipo("facturacion", sim)
    logins = {p["login"] for p in d["personas"]}
    # contabilidad (Laura) + administradores de Agentes de IA (Roberto, Ana); nunca el usuario técnico
    assert logins == {"lcontreras@cbh.mx", "rsalas@cbh.mx", "avillasenor@cbh.mx"}
    assert "agente.ia@i-condor.com" not in logins
    # sin administradores: sólo contabilidad
    db.set_ajuste("avisos", {"incluir_admin_agentes": False})
    assert {p["login"] for p in OA.destinatarios_equipo("facturacion", sim)["personas"]} == {"lcontreras@cbh.mx"}
    # un equipo sin grupo en la base y sin logins → nadie, con explicación
    d2 = OA.destinatarios_equipo("direccion", sim)
    assert d2["personas"] == [] and any("Ningún usuario" in x for x in d2["avisos"])
    # logins configurados se suman
    db.set_ajuste("avisos", {"incluir_admin_agentes": False, "equipos": {"direccion": {"logins": ["avillasenor@cbh.mx"]}}})
    assert [p["login"] for p in OA.destinatarios_equipo("direccion", sim)["personas"]] == ["avillasenor@cbh.mx"]
    db.set_ajuste("avisos", {})


def test_aviso_se_propone_ejecuta_verifica_y_retira(sim):
    db.set_ajuste("avisos", {})
    _limpiar()
    antes_act = len(sim.tablas["mail.activity"]); antes_msg = len(sim.mensajes)
    caso = {"metodos": ["R08_DUPLICADO"], "titulo": "Sevoflurano · FOL-1 · HGZ 17", "producto_id": 1001, "producto": "Sevoflurano",
            "folio": "FOL-1", "folio_id": sim.tablas["cbh.operacion.medica"][0]["id"], "severidad": "alta"}
    corrida = db.iniciar_corrida("consumo", {}, "test", "test")
    cid = db.guardar_caso({"corrida_id": corrida, "agente": "consumo", "tipo": "anomalia", "titulo": caso["titulo"], "severidad": "alta",
                           "entidades": {}, "referencias": {}, "expediente": {}, "conclusion": "", "confianza": "media", "impacto_mxn": 0,
                           "accion_recomendada": "", "responsable": "", "investigado_con": "test", "herramientas": [], "huella": "t-aviso"})
    props = investigador.proponer_acciones_caso(cid, caso, {"que_paso": "Línea duplicada", "accion_recomendada": "Conciliar",
                                                            "responsable": "Contabilidad"}, corrida_id=corrida, usuario="test", respaldo=False)
    avisos = [db.accion(p["id"]) for p in props if db.accion(p["id"])["tipo"] == "aviso_equipo"]
    assert len(avisos) == 1 and not any(db.accion(p["id"])["tipo"] == "ticket_helpdesk" for p in props)   # sin Helpdesk
    a = avisos[0]
    assert a["riesgo"] == "bajo" and a["payload"]["equipo"] == "facturacion" and a["payload"]["n_destinatarios"] == 3
    assert "3 persona(s)" in a["efecto"] and "Laura Contreras" in a["efecto"]
    assert a["payload"]["modelo"] == "cbh.operacion.medica" and a["payload"]["res_id"] == caso["folio_id"]
    # aprobar → ejecuta: una actividad por persona sobre el folio + nota en el chatter que las menciona
    r = autonomia.aprobar(a["id"], "operador", "operacion")
    assert r["estado"] == "ejecutada", r
    nuevas = sim.tablas["mail.activity"][antes_act:]
    assert len(nuevas) == 3 and {x["user_id"][0] for x in nuevas} == {11, 12, 13}
    def _id(v):  # el simulador convierte *_id enteros a [id, nombre]
        return v[0] if isinstance(v, (list, tuple)) else v
    assert all(x["res_model"] == "cbh.operacion.medica" and _id(x["res_id"]) == caso["folio_id"] for x in nuevas)
    msg = sim.mensajes[antes_msg:]
    assert len(msg) == 1 and set(msg[0]["partner_ids"]) == {801, 802, 803}
    a = db.accion(a["id"])
    assert a["odoo_modelo"] == "mail.activity" and "3 persona(s)" in a["odoo_ref"]
    # seguimiento: nadie ha atendido → sigue ejecutada; una persona marca hecha → 1/3; todas → concluida
    autonomia.verificar_ejecutadas()
    assert db.accion(a["id"])["estado"] == "ejecutada"
    sim.unlink("mail.activity", [nuevas[0]["id"]])
    autonomia.verificar_ejecutadas()
    a2 = db.accion(a["id"])
    assert a2["estado"] == "ejecutada" and "1/3" in (a2.get("estado_odoo") or "")
    sim.unlink("mail.activity", [nuevas[1]["id"], nuevas[2]["id"]])
    autonomia.verificar_ejecutadas()
    assert db.accion(a["id"])["estado"] == "concluida"


def test_aviso_idempotente_y_reversible(sim):
    db.set_ajuste("avisos", {})
    _limpiar()
    r = autonomia.proponer("copiloto", "aviso_equipo", "Avisar a Calidad · prueba", {"equipo": "calidad", "equipo_nombre": "Calidad",
                           "asunto": "Inspeccionar lote", "cuerpo": "Revisar remanente", "plazo_dias": 2, "producto_id": 1002, "n_destinatarios": 3,
                           "destinatarios": ["x"], "destinatarios_texto": "x"}, motivo="prueba", sincronizar=False)
    a = db.accion(r["id"])
    n0 = len(sim.tablas["mail.activity"])
    assert autonomia.aprobar(a["id"], "operador", "operacion")["estado"] == "ejecutada"
    creadas = sim.tablas["mail.activity"][n0:]
    # calidad = responsables de inventario (Roberto) + administradores de Agentes de IA (Roberto, Ana) → 2 personas, sin repetir
    assert len(creadas) == 2 and all(x["res_model"] == "product.template" for x in creadas)     # sin folio: cuelga del producto
    assert {x["user_id"][0] for x in creadas} == {12, 13}
    # la misma referencia no vuelve a crear actividades (respuesta perdida / reintento)
    ref = db.accion(a["id"])["referencia"]
    otra = OA.enviar_aviso_equipo("calidad", "Inspeccionar lote", "Revisar remanente", referencia=ref, cli=sim)
    assert otra["estado"] == "existente" and len(sim.tablas["mail.activity"]) == n0 + 2
    # retirar el aviso: se eliminan las actividades que siguen vivas
    sim.unlink("mail.activity", [creadas[0]["id"]])
    rv = autonomia.revertir(a["id"], "admin")
    assert rv["estado"] == "revertida" and rv["odoo"]["retiradas"] == 1 and rv["odoo"]["ya_atendidas"] == 1
    assert len(sim.tablas["mail.activity"]) == n0


def test_aviso_sin_destinatarios_no_se_ejecuta_a_ciegas(sim):
    db.set_ajuste("avisos", {"incluir_admin_agentes": False})
    _limpiar()
    r = autonomia.proponer("copiloto", "aviso_equipo", "Avisar a Dirección · prueba", {"equipo": "direccion", "equipo_nombre": "Dirección",
                           "asunto": "x", "cuerpo": "y", "plazo_dias": 5, "n_destinatarios": 0, "destinatarios": [], "destinatarios_texto": "nadie todavía"},
                           motivo="prueba", sincronizar=False)
    res = autonomia.aprobar(r["id"], "operador", "operacion")
    assert res["estado"] == "error" and "destinatarios" in (res.get("error") or "")
    db.set_ajuste("avisos", {})


def test_api_avisos_configuracion(sim):
    from fastapi.testclient import TestClient
    from app.main import app
    db.set_ajuste("avisos", {})
    with TestClient(app) as c:
        c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        r = c.post("/api/avisos", json={"equipos": {"facturacion": {"logins": "contabilidad@cbh.mx, Otra@cbh.mx"}}, "incluir_admin_agentes": False})
        assert r.status_code == 200 and r.json()["equipos"]["facturacion"]["logins"] == ["contabilidad@cbh.mx", "otra@cbh.mx"]
        d = c.get("/api/avisos/destinatarios").json()
        assert d["facturacion"]["personas"] and all(p["login"] != "agente.ia@i-condor.com" for p in d["facturacion"]["personas"])
        assert "Avisos a personas" in c.get("/configuracion").text
    db.set_ajuste("avisos", {})
    json.dumps(OA.configuracion_avisos())      # serializable
