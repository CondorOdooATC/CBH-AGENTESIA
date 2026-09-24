"""v1.3.7 · Configuración intuitiva: lista de preparación con semáforo, textos de ayuda, casos resumidos y copiloto amplio."""
from __future__ import annotations

from app import db
from app.agents import autonomia


def _login(c):
    r = c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_lista_de_preparacion_detecta_topes_incoherentes(sim):
    from app.main import _preparacion
    from app.odoo import schema, acciones as OA
    autonomia.set_politicas({"tope_importe": 1500.0, "importe_riesgo_alto": 50000.0}, "test")
    try:
        pts = _preparacion(schema.cargar(), autonomia.resumen(), db.uso_periodo(), OA.configuracion_avisos())
        claves = {p["clave"]: p for p in pts}
        # los nueve puntos existen y cada uno tiene un estado de semáforo válido
        assert {"odoo", "folios", "ia", "presupuesto", "sso", "autonomia", "avisos", "programacion", "entorno"} <= set(claves)
        assert all(p["estado"] in ("v", "a", "r") for p in pts)
        # importe máximo (1,500) < riesgo alto (50,000) → ámbar con explicación en lenguaje llano
        assert claves["autonomia"]["estado"] == "a" and "1,500" in claves["autonomia"]["detalle"] and "50,000" in claves["autonomia"]["detalle"]
        # modo demo → Odoo en ámbar con instrucción; módulo de folios detectado en el simulador → verde
        assert claves["odoo"]["estado"] == "a"
        assert claves["folios"]["estado"] == "v" and claves["folios"]["detalle"].startswith("Detectado")
        # sin API key en pruebas → rojo y dice quién lo configura
        assert claves["ia"]["estado"] == "r" and "Cóndor" in claves["ia"]["detalle"]
    finally:
        autonomia.set_politicas({"tope_importe": 150000.0, "importe_riesgo_alto": 50000.0}, "test")
    pts = _preparacion(schema.cargar(), autonomia.resumen(), db.uso_periodo(), OA.configuracion_avisos())
    assert {p["clave"]: p for p in pts}["autonomia"]["estado"] == "v"


def test_pagina_configuracion_habla_claro(sim):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        _login(c)
        html = c.get("/configuracion").text
        assert "Lista de preparación" in html and "de 9 listos" in html
        # etiquetas en lenguaje llano en lugar de nombres técnicos
        for etiqueta in ("Importe máximo por acción (MXN)", "Riesgo alto a partir de (MXN)", "Caducidad de una propuesta (días)",
                         "Tolerancia al re-verificar (%)", "Cuánto investiga la IA", "Ajustes avanzados de los agentes",
                         "Qué pueden hacer los agentes", "Lo que los agentes ya saben"):
            assert etiqueta in html, etiqueta
        # cada bloque trae su explicación
        assert html.count('class="ayuda"') >= 12
        # nada de datos de ejemplo en placeholders (hospitales, productos o proveedores inventados)
        assert "p. ej. Paquete básico" not in html


def test_casos_resumidos_con_ver_mas_y_copiloto_amplio(sim):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        _login(c)
        html = c.get("/casos").text
        if "Sin casos en este estado" not in html:
            assert "Ver más" in html and 'class="detalle-caso" hidden' in html
        html = c.get("/copiloto").text
        assert "chat-amplio" in html and "Ver ideas de preguntas" in html or "Ocultar ideas de preguntas" in html
        # ideas de preguntas: como máximo cuatro, en una fila compacta
        assert html.count('onclick="sugerir(this)"') <= 4
