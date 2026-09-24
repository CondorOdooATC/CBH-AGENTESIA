"""Pruebas de las correcciones de confiabilidad: doble aprobación, revalidación, idempotencia,
reversión de reglas, roles en herramientas, propiedad de conversaciones, seguimiento."""
from __future__ import annotations

import pytest

from app import db
from app.agents import autonomia
from app.llm import tools


def _tx(sim, cantidad=5, origen=507, destino=505, importe=190.0):
    return autonomia.proponer("demanda", "transferencia_interna", "t",
                              {"producto_id": 1004, "cantidad": cantidad, "origen_id": origen, "destino_id": destino,
                               "origen": "CEDIS-LR/Stock", "destino": "HGZ67/Stock"},
                              impacto={"cantidad": cantidad, "importe": importe}, sincronizar=False)


def test_doble_aprobacion_riesgo_alto(sim):
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": True})
    r = autonomia.proponer("demanda", "solicitud_compra", "Compra grande", {"producto_id": 1004, "cantidad": 100},
                           impacto={"cantidad": 100, "importe": 80000}, sincronizar=False)
    assert r["riesgo"] == "alto"
    assert autonomia.aprobar(r["id"], "luis", "operacion")["estado"] == "aprobada_parcial"
    assert autonomia.aprobar(r["id"], "luis", "admin")["estado"] == "aprobada_parcial"      # misma persona: no
    assert autonomia.aprobar(r["id"], "pedro", "operacion")["estado"] == "aprobada_parcial"  # no admin: no
    assert autonomia.aprobar(r["id"], "ana", "admin")["estado"] == "ejecutada"


def test_revalidacion_detecta_cambio_de_existencia(sim):
    # HGZ17 tiene ~2 unidades de fentanilo: pedir 50 debe pasar a requiere_revision con cantidad ajustada
    r = autonomia.proponer("demanda", "transferencia_interna", "t",
                           {"producto_id": 1005, "cantidad": 50, "origen_id": 503, "destino_id": 505, "origen": "HGZ17/Stock", "destino": "HGZ67/Stock"},
                           impacto={"cantidad": 50, "importe": 1100}, sincronizar=False)
    a = autonomia.aprobar(r["id"], "ana", "admin")
    assert a["estado"] == "requiere_revision"
    acc = db.accion(r["id"])
    assert acc["payload"]["cantidad"] < 50 and "requiere nueva aprobación" in acc["revalidacion"]


def test_idempotencia_y_transicion_atomica(sim):
    r = _tx(sim)
    n0 = len(sim.tablas["stock.picking"])
    assert autonomia.aprobar(r["id"], "ana", "admin")["estado"] == "ejecutada"
    assert autonomia.aprobar(r["id"], "ana", "admin")["estado"] == "ejecutada"   # segunda vez: no cambia nada
    assert len(sim.tablas["stock.picking"]) == n0 + 1
    # ejecutar directo sobre una acción ya ejecutada tampoco duplica
    assert autonomia.ejecutar(r["id"])["estado"] == "ejecutada"
    assert len(sim.tablas["stock.picking"]) == n0 + 1


def test_revertir_regla_restaura_valores(sim):
    sim.tablas["stock.warehouse.orderpoint"].append({"id": 888, "name": "OP/888", "product_id": [1005, "Fentanilo"],
                                                     "location_id": [503, "HGZ17/Stock"], "product_min_qty": 10.0,
                                                     "product_max_qty": 30.0, "qty_multiple": 2.0})
    r = autonomia.proponer("demanda", "regla_reabastecimiento", "Regla",
                           {"producto_id": 1005, "ubicacion_id": 503, "minimo": 20, "maximo": 80, "ubicacion": "HGZ17/Stock"},
                           impacto={"importe": 500}, sincronizar=False)
    assert autonomia.aprobar(r["id"], "ana", "admin")["estado"] == "ejecutada"
    op = next(o for o in sim.tablas["stock.warehouse.orderpoint"] if o["id"] == 888)
    assert (op["product_min_qty"], op["product_max_qty"]) == (20.0, 80.0)
    assert autonomia.revertir(r["id"], "ana")["estado"] == "revertida"
    op = next(o for o in sim.tablas["stock.warehouse.orderpoint"] if o["id"] == 888)
    assert (op["product_min_qty"], op["product_max_qty"], op["qty_multiple"]) == (10.0, 30.0, 2.0)


def test_efecto_declarado(sim):
    r = _tx(sim)
    e = db.accion(r["id"])["efecto"]
    assert "transferencia interna" in e and "borrador" in e.lower()


def test_roles_en_herramientas(sim):
    with pytest.raises(PermissionError):
        tools.ejecutar("ejecutar_agente", {"agente": "consumo"}, "lector", "consulta")
    with pytest.raises(PermissionError):
        tools.ejecutar("proponer_accion", {"tipo": "alerta", "titulo": "x", "payload": {}}, "lector", "consulta")
    assert tools.ejecutar("consultar_existencias", {"filtro_texto": "fentanilo"}, "lector", "consulta")["total_filas"] > 0


def test_conversaciones_privadas(sim):
    from fastapi.testclient import TestClient
    from app.main import app
    db.crear_usuario("otro", "test1234", "Otro", "consulta")
    with TestClient(app) as c:
        c.post("/login", data={"usuario": "otro", "password": "test1234", "next": "/"}, follow_redirects=False)
        cid = c.post("/api/conversaciones").json()["id"]
    with TestClient(app) as c2:
        c2.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        db.crear_usuario("tercero", "test1234", "Tercero", "consulta")
        # sólo condor puede tocar el modelo de lenguaje
        db.crear_usuario("adm2", "test1234", "Admin cliente", "admin")
    with TestClient(app) as c4:
        c4.post("/login", data={"usuario": "adm2", "password": "test1234", "next": "/"}, follow_redirects=False)
        assert c4.post("/api/config/agente/consumo", json={"modelo_investigacion": "x"}).status_code == 403
        assert c4.post("/api/usuarios", json={"usuario": "otro_condor", "password": "x", "rol": "condor"}).status_code == 403
        assert c4.post("/api/config/agente/consumo", json={"umbral_z": 2.7}).status_code == 200
    with TestClient(app) as c3:
        c3.post("/login", data={"usuario": "tercero", "password": "test1234", "next": "/"}, follow_redirects=False)
        assert c3.get(f"/copiloto?c={cid}").status_code == 403
        assert c3.post("/api/chat", json={"conversacion_id": cid, "texto": "hola"}).status_code == 403


def test_seguimiento_marca_concluidas(sim):
    r = _tx(sim)
    autonomia.aprobar(r["id"], "ana", "admin")
    a = db.accion(r["id"])
    for p in sim.tablas["stock.picking"]:
        if p["id"] == a["odoo_id"]:
            p["state"] = "done"
    res = autonomia.verificar_ejecutadas()
    assert res["concluidas"] >= 1 and db.accion(r["id"])["estado"] == "concluida"


def test_nivel_maximo_es_2(sim):
    assert autonomia.set_nivel(3) == 2
    autonomia.set_nivel(1)
