"""Modo de respaldo: cuando no existe el módulo de folios de CBH y el consumo se lee de ``stock.move.line``,
las reglas que dependen de folio médico (R04 sin cirugía, R08 duplicado) no se evalúan, no se proponen tickets de
facturación al IMSS, el importe se estima a costo estándar (impacto ≠ $0) y la interfaz avisa del modo."""
from __future__ import annotations

import pandas as pd

from app import db
from app.agents import autonomia, consumo
from app.ml import anomalias as A
from app.odoo import queries, schema


def _forzar_respaldo(monkeypatch, sim):
    d, h = queries._rango(None, None, 120)
    df = queries._consumo_respaldo(sim, d, h, None, None, 50_000)
    monkeypatch.setattr(queries, "consumo", lambda *a, **k: df)
    return df


def test_reglas_desactivadas_no_marcan():
    cfg = A.ConfigAnomalias(reglas_desactivadas={"R08_DUPLICADO"})
    fecha = pd.Timestamp("2026-08-03 10:00")
    base = {"fecha": fecha, "folio": "F1", "hospital": "H", "subalmacen": "H/Q1", "medico": "Dr", "auxiliar": "Aux",
            "producto": "Sevoflurano 250 mL", "producto_id": 1, "lote": "L1", "cantidad": 40.0, "unidad": "mL", "importe": 100.0,
            "duracion_min": 90, "peso_inicial": None, "peso_final": None, "caducidad": None, "almacen": "H", "ubicacion": "H/Q1",
            "id": 0, "consumo_peso": None}
    filas = [dict(base, id=i, folio=f"F{i}", fecha=fecha + pd.Timedelta(days=i % 40)) for i in range(60)]
    filas.append(dict(base, id=999, folio="F0"))           # duplicado exacto de la fila 0 → R08 (desactivada)
    res = A.detectar(pd.DataFrame(filas), cfg)
    metodos = {m for ms in res["hallazgos"]["metodos"] for m in ms} if not res["hallazgos"].empty else set()
    assert "R08_DUPLICADO" not in metodos
    # con la regla activa sí se marca
    res2 = A.detectar(pd.DataFrame(filas), A.ConfigAnomalias())
    metodos2 = {m for ms in res2["hallazgos"]["metodos"] for m in ms} if not res2["hallazgos"].empty else set()
    assert "R08_DUPLICADO" in metodos2


def test_corrida_en_respaldo_sin_r04_r08_ni_tickets_de_facturacion(sim, monkeypatch):
    df = _forzar_respaldo(monkeypatch, sim)
    assert consumo.modo_respaldo(df) is True
    assert float(df["importe"].sum()) > 0 and bool(df["importe_estimado"].all())   # valorado a costo estándar
    for e in autonomia.ESTADOS_PENDIENTES:
        for a in db.acciones(estado=e, agente="consumo", limite=5000):
            db.transicion_accion(a["id"], e, "rechazada", resultado="limpieza de prueba")
    r = consumo.ejecutar(dias=120, usuario="test", con_llm=False, generar_excel=False)
    ultimo = db.get_ajuste("agente1_ultimo", {})
    assert ultimo["modo_respaldo"] is True and ultimo["origen_datos"] == "stock.move.line"
    assert {"R04_SIN_CIRUGIA", "R08_DUPLICADO"} <= set(ultimo["reglas_desactivadas"])
    hall = db.anomalias(corrida_id=r["corrida_id"], limite=5000)
    for h in hall:
        assert not ({"R04_SIN_CIRUGIA", "R08_DUPLICADO"} & set(h.get("metodos") or []))
    pendientes = [a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, agente="consumo", limite=5000)]
    assert not any("Facturación" in (a.get("titulo") or "") for a in pendientes)
    assert "Modo de respaldo" in ultimo["informe"]


def test_aviso_de_respaldo_en_pantalla(sim, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as M
    from app.main import app
    monkeypatch.setattr(schema, "en_respaldo", lambda mapeo=None: True)
    monkeypatch.setattr(M, "DEMO", False)
    monkeypatch.setattr(M.settings, "ODOO_URL", "https://cbhtest.odoo.com")
    monkeypatch.setattr(M.settings, "ODOO_DB", "cbhtest")
    monkeypatch.setattr(M.settings, "ODOO_USER", "agente.ia@i-condor.com")
    monkeypatch.setattr(M.settings, "ODOO_API_KEY", "x")
    with TestClient(app) as c:
        c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        r = c.get("/acciones")
        assert r.status_code == 200 and "Módulo de folios no detectado" in r.text and "Re-descubrir" in r.text
    monkeypatch.setattr(schema, "en_respaldo", lambda mapeo=None: False)
    with TestClient(app) as c:
        c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        assert "Módulo de folios no detectado" not in c.get("/acciones").text


def test_deteccion_automatica_del_modulo_de_folios_con_otro_nombre(sim):
    """El módulo del cliente se llama distinto a todos los candidatos: la plataforma lo encuentra sola por la forma de sus
    modelos (línea con producto + cantidad + lote + báscula; cabecera con hospital, médico, paciente y líneas)."""
    import copy
    from app.odoo.simulado import OdooSimulado
    s2 = OdooSimulado()
    # renombrar el módulo custom del simulador a nombres que NO están en la lista de candidatos
    s2.tablas["cbticket.operacion"] = s2.tablas.pop("cbh.operacion.medica")
    s2.tablas["cbticket.operacion.insumo"] = s2.tablas.pop("cbh.operacion.medica.line")
    for l in s2.tablas["cbticket.operacion.insumo"]:
        if "operacion_id" in l:
            l["insumo_de_id"] = l.pop("operacion_id")          # el campo de enlace también cambia de nombre
    s2.tablas["ir.model"] = [{"id": i + 1, "model": m, "name": m, "modules": ("cbticket" if m.startswith("cbticket") else "base"),
                              "transient": False, "state": "base"} for i, m in enumerate(s2.tablas)]
    det = schema.detectar_modelos_cbh(s2)
    assert det["consumo"] == "cbticket.operacion.insumo" and det["folio"] == "cbticket.operacion", det
    m = schema.descubrir(s2, guardar_resultado=False)
    assert m["entidades"]["consumo"]["modelo"] == "cbticket.operacion.insumo" and m["entidades"]["consumo"]["confianza"] == "detectada"
    assert m["entidades"]["folio"]["modelo"] == "cbticket.operacion"
    campos = m["entidades"]["consumo"]["campos"]
    assert campos["producto"] == "product_id" and campos["folio_id"] == "insumo_de_id"      # por relación, aunque el nombre cambió
    assert schema.en_respaldo(m) is False
