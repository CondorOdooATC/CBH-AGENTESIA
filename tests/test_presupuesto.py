"""Presupuesto mensual de IA: Cóndor fija el paquete; al acercarse se avisa; al agotarse los agentes no corren, el chat
responde que hay que comprar más tokens y la interfaz lo dice en todas las pantallas."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.agents import consumo, copiloto
from app.config import settings


def test_tope_de_tokens_detiene_agentes_y_chat(sim, monkeypatch):
    monkeypatch.setattr(settings, "LLM_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "clave-de-prueba")
    db.set_ajuste("presupuesto_ia", {"tokens_entrada": 1000, "tokens_salida": 100, "aviso_pct": 80, "paquete": "Prueba"})
    try:
        assert db.uso_periodo()["agotado"] is False
        db.registrar_uso("claude-sonnet-5", "prueba", 850, 10, "t")          # 85 % → aviso, todavía corre
        u = db.uso_periodo()
        assert u["aviso"] and not u["agotado"] and u["pct_max"] == 85.0
        db.registrar_uso("claude-sonnet-5", "prueba", 200, 10, "t")          # supera el paquete
        assert db.presupuesto_agotado()
        msg = db.mensaje_presupuesto_agotado()
        assert "compren más tokens" in msg and "Cóndor" in msg
        # los agentes no arrancan sin IA cuando el presupuesto está agotado
        try:
            consumo.ejecutar(dias=30, usuario="t", con_llm=True)
            raise AssertionError("debió detenerse")
        except RuntimeError as e:
            assert "compren más tokens" in str(e)
        assert not consumo._CANDADO.locked()
        # el chat responde el mensaje, no una respuesta determinista
        cid = db.nueva_conversacion("t")
        r = copiloto.responder(cid, "¿qué pasó hoy?", "t", "admin")
        assert "compren más tokens" in r["texto"]
        # la interfaz lo dice en cualquier pantalla y Cóndor puede ampliar el paquete
        from app.main import app
        db.crear_usuario("condor_t", "test12345678", "Cóndor", "condor")
        with TestClient(app) as c:
            c.post("/login", data={"usuario": "condor_t", "password": "test12345678", "next": "/"}, follow_redirects=False)
            assert "Presupuesto mensual de IA agotado" in c.get("/casos").text
            r = c.post("/api/presupuesto-ia", json={"tokens_entrada": 5_000_000, "tokens_salida": 500_000, "aviso_pct": 80, "paquete": "Ampliado"})
            assert r.status_code == 200 and r.json()["agotado"] is False
            assert "Presupuesto mensual de IA agotado" not in c.get("/casos").text
            # un admin del cliente no puede cambiarlo
        db.crear_usuario("adm_pr", "test12345678", "Admin", "admin")
        with TestClient(app) as c:
            c.post("/login", data={"usuario": "adm_pr", "password": "test12345678", "next": "/"}, follow_redirects=False)
            assert c.post("/api/presupuesto-ia", json={"tokens_entrada": 1, "tokens_salida": 1}).status_code == 403
    finally:
        db.set_ajuste("presupuesto_ia", {})
        with db.conn() as con:
            con.execute("DELETE FROM uso_llm WHERE origen='prueba'")
