"""v1.3.11 · Copiloto robusto y económico: razonamiento de Claude 5 (bloques firmados), corte por max_tokens, respuesta
final garantizada, caché de prompts, esfuerzo por origen, historial recortado y pantallas sólo de administradores."""
from __future__ import annotations

import json

import pytest

from app import db
from app.config import settings
from app.llm import claude


# ── razonamiento firmado (thinking) ─────────────────────────────────────────
def test_sin_razonamiento_quita_bloques_de_turnos_anteriores():
    msgs = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "…", "signature": "abc"},
                                          {"type": "text", "text": "Hola, ¿en qué ayudo?"}]},
        {"role": "user", "content": "dame el consumo"},
        {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "xxx"}]},        # turno cortado: sólo razonamiento
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "…", "signature": "def"},
                                          {"type": "tool_use", "id": "t1", "name": "consultar_consumo", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
    ]
    limpio = claude.sin_razonamiento(msgs)
    tipos = [[b.get("type") for b in m["content"]] if isinstance(m["content"], list) else m["content"] for m in limpio]
    assert tipos == ["hola", ["text"], "dame el consumo", ["tool_use"], ["tool_result"]]
    assert not any(b.get("type") in ("thinking", "redacted_thinking") for m in limpio if isinstance(m["content"], list) for b in m["content"])


def test_historial_del_copiloto_sin_razonamiento_y_con_resultados_viejos_resumidos(sim):
    from app.agents import copiloto
    cid = db.nueva_conversacion("prueba-hist")
    grande = json.dumps([{"fila": i, "producto": "Sevoflurano 250 mL", "cantidad": 12.5} for i in range(200)])
    for turno in range(4):
        db.guardar_mensaje(cid, "user", f"pregunta {turno}", f"pregunta {turno}")
        db.guardar_mensaje(cid, "assistant", [{"type": "thinking", "thinking": "…", "signature": "s"},
                                              {"type": "tool_use", "id": f"t{turno}", "name": "consultar_consumo", "input": {}}], "")
        db.guardar_mensaje(cid, "user", [{"type": "tool_result", "tool_use_id": f"t{turno}", "content": grande}], "")
        db.guardar_mensaje(cid, "assistant", [{"type": "thinking", "thinking": "…", "signature": "s"},
                                              {"type": "text", "text": f"respuesta {turno}"}], f"respuesta {turno}")
    h = copiloto._historial(cid)
    assert not any(b.get("type") in ("thinking", "redacted_thinking") for m in h if isinstance(m["content"], list) for b in m["content"])
    resultados = [b for m in h if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"]
    assert len(resultados) == 4
    # los dos turnos más recientes conservan el resultado íntegro; los anteriores quedan resumidos en una línea
    assert all(len(r["content"]) < 300 and "resumido" in r["content"] for r in resultados[:2])
    assert all(len(r["content"]) > 3000 for r in resultados[2:])
    # las respuestas redactadas (lo que vio el usuario) se conservan completas
    assert [b["text"] for m in h if m["role"] == "assistant" for b in m["content"] if b.get("type") == "text"] == [f"respuesta {i}" for i in range(4)]


# ── cuerpo de la petición: caché, esfuerzo, razonamiento adaptativo ──────────
class _Resp:
    def __init__(self, status: int, cuerpo: dict | None = None, texto: str = ""):
        self.status_code = status
        self._cuerpo = cuerpo or {}
        self.text = texto or json.dumps(self._cuerpo)

    def json(self):
        return self._cuerpo


def _ok(contenido: list, stop: str = "end_turn", uso: dict | None = None) -> dict:
    return {"content": contenido, "stop_reason": stop,
            "usage": uso or {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


@pytest.fixture
def api(monkeypatch):
    """Captura las peticiones a la API y entrega respuestas guionizadas. Al terminar borra el uso registrado por las
    llamadas simuladas para no alterar las pruebas del presupuesto."""
    estado = {"peticiones": [], "respuestas": []}

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            estado["peticiones"].append({"headers": headers, "json": json})
            r = estado["respuestas"].pop(0)
            return r() if callable(r) else r

    monkeypatch.setattr(claude.httpx, "Client", _Cliente)
    monkeypatch.setattr(settings, "LLM_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-prueba")
    monkeypatch.setattr(settings, "ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setattr(settings, "ANTHROPIC_EFFORT", "low")
    monkeypatch.setattr(settings, "ANTHROPIC_EFFORT_INFORMES", "medium")
    monkeypatch.setattr(settings, "ANTHROPIC_CACHE", True)
    monkeypatch.setattr(db, "presupuesto_agotado", lambda: False)
    yield estado
    with db.conn() as con:
        con.execute("DELETE FROM uso_llm WHERE usuario IN ('', 'prueba', 'prueba-cache') AND origen IN ('copiloto', 'agente1')")


def test_peticion_lleva_cache_esfuerzo_y_razonamiento_adaptativo(sim, api):
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "listo"}])))
    tools = [{"name": "a", "input_schema": {"type": "object"}}, {"name": "b", "input_schema": {"type": "object"}}]
    claude.mensaje(["estable", "variable"], [{"role": "user", "content": "hola"}], tools=tools, origen="copiloto", cache_historial=True)
    p = api["peticiones"][0]["json"]
    h = api["peticiones"][0]["headers"]
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in p["system"][1]
    assert "cache_control" not in p["tools"][0] and p["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert p["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert p["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert h["anthropic-beta"] == claude._BETA_RAZONAMIENTO
    assert p["output_config"] == {"effort": "low"}
    assert p["max_tokens"] == settings.ANTHROPIC_MAX_TOKENS
    # una llamada suelta (informe) no marca el historial y usa el esfuerzo de informes
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "informe"}])))
    claude.mensaje("sistema", [{"role": "user", "content": "redacta"}], origen="agente1")
    p2 = api["peticiones"][1]["json"]
    assert p2["output_config"] == {"effort": "medium"} and isinstance(p2["messages"][0]["content"], str)
    # ningún cache_control heredado se duplica al reenviar historial
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "x"}])))
    hist = [{"role": "user", "content": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": "c"}]
    claude.mensaje("s", hist, cache_historial=True)
    p3 = api["peticiones"][2]["json"]
    marcas = sum(1 for m in p3["messages"] for b in m["content"] if isinstance(b, dict) and "cache_control" in b)
    assert marcas == 1 and p3["messages"][-1]["content"][-1]["cache_control"]


def test_si_la_api_rechaza_la_configuracion_opcional_se_reintenta_sin_ella(sim, api):
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "thinking.block_binding: unsupported"}}
    api["respuestas"].append(_Resp(400, err))
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "ok"}])))
    r = claude.mensaje("s", [{"role": "user", "content": "hola"}], tools=[{"name": "a", "input_schema": {"type": "object"}}], cache_historial=True)
    assert claude.texto_de(r) == "ok"
    p2 = api["peticiones"][1]["json"]
    # sólo se quita la parte rechazada (razonamiento + cabecera beta); esfuerzo y caché se conservan
    assert "thinking" not in p2 and "anthropic-beta" not in api["peticiones"][1]["headers"]
    assert p2["output_config"] == {"effort": "low"} and p2["system"][0]["cache_control"] and p2["tools"][-1]["cache_control"]
    # si además rechaza la caché, se quita la caché y se conserva el resto
    api["respuestas"].append(_Resp(400, {"type": "error", "error": {"message": "cache_control is not supported"}}))
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "ok2"}])))
    r = claude.mensaje("s", [{"role": "user", "content": "hola"}], tools=[{"name": "a", "input_schema": {"type": "object"}}], cache_historial=True)
    p4 = api["peticiones"][3]["json"]
    assert claude.texto_de(r) == "ok2" and "cache_control" not in p4["system"][0] and "cache_control" not in p4["tools"][-1] and p4["thinking"]


def test_uso_registra_cache_y_razonamiento_con_entrada_equivalente(sim, api):
    uso = {"input_tokens": 1000, "output_tokens": 300, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 20000,
           "output_tokens_details": {"thinking_tokens": 120}}
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "ok"}], uso=uso)))
    antes = db.uso_periodo()
    try:
        claude.mensaje("s", [{"role": "user", "content": "hola"}], origen="copiloto", usuario="prueba-cache")
        despues = db.uso_periodo()
        # equivalente: 1000 + 4000×1.25 + 20000×0.05 (Opus 5.5) = 7,000 en vez de 25,000
        assert despues["tokens_entrada"] - antes["tokens_entrada"] == 7000
        assert despues["tokens_salida"] - antes["tokens_salida"] == 300
        assert despues["cache_lectura"] - antes["cache_lectura"] == 20000 and despues["razonamiento"] - antes["razonamiento"] == 120
    finally:
        with db.conn() as con:                      # no dejar consumo ficticio para las pruebas del presupuesto
            con.execute("DELETE FROM uso_llm WHERE usuario=?", ("prueba-cache",))
    assert settings.cost_usd(1000, 300, "claude-opus-5-5", 4000, 20000) == pytest.approx(7000 / 1e6 * 4.0 + 300 / 1e6 * 20.0)
    assert settings.cache_lectura("claude-sonnet-5") == 0.1


# ── bucle de herramientas: cortes y respuesta final garantizada ──────────────
def test_corte_por_max_tokens_a_media_herramienta_reintenta_con_mas_espacio(sim, api, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_MAX_TOKENS", 8000)
    monkeypatch.setattr(settings, "ANTHROPIC_MAX_TOKENS_TOPE", 32000)
    api["respuestas"].append(_Resp(200, _ok([{"type": "thinking", "thinking": "…", "signature": "s"},
                                             {"type": "tool_use", "id": "t1", "name": "generar_excel", "input": {}}], stop="max_tokens")))
    api["respuestas"].append(_Resp(200, _ok([{"type": "tool_use", "id": "t2", "name": "consultar", "input": {"x": 1}}], stop="tool_use")))
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "Aquí está el resumen."}])))
    r = claude.bucle_herramientas("s", [{"role": "user", "content": "excel"}], [{"name": "consultar", "input_schema": {"type": "object"}}],
                                  lambda n, a: {"filas": [1, 2, 3]})
    assert r["texto"] == "Aquí está el resumen."
    pedidos = [p["json"]["max_tokens"] for p in api["peticiones"]]
    assert pedidos == [8000, 16000, 16000]            # el reintento dobla el espacio; el bloque incompleto no se reenvía
    assert not any(b.get("type") == "tool_use" and b["id"] == "t1" for m in api["peticiones"][1]["json"]["messages"] for b in (m["content"] if isinstance(m["content"], list) else []))
    assert r["herramientas"][0]["nombre"] == "consultar" and r["iteraciones"] == 2


def test_respuesta_sin_texto_se_pide_redactar_y_si_no_hay_texto_se_explica(sim, api):
    # 1) sólo razonamiento y fin de turno → se le pide redactar → responde
    api["respuestas"].append(_Resp(200, _ok([{"type": "thinking", "thinking": "…", "signature": "s"}])))
    api["respuestas"].append(_Resp(200, _ok([{"type": "text", "text": "Respuesta redactada."}])))
    r = claude.bucle_herramientas("s", [{"role": "user", "content": "hola"}], [], lambda n, a: {})
    assert r["texto"] == "Respuesta redactada."
    ultimo = api["peticiones"][1]["json"]["messages"][-1]
    assert ultimo["role"] == "user" and any("Redacta" in b.get("text", "") for b in ultimo["content"])
    # 2) nunca redacta → respuesta de respaldo con las herramientas usadas, nunca vacío
    api["respuestas"].append(_Resp(200, _ok([{"type": "tool_use", "id": "t1", "name": "consultar", "input": {}}], stop="tool_use")))
    api["respuestas"].append(_Resp(200, _ok([])))
    api["respuestas"].append(_Resp(200, _ok([])))
    api["respuestas"].append(_Resp(200, _ok([])))
    r = claude.bucle_herramientas("s", [{"role": "user", "content": "hola"}], [{"name": "consultar", "input_schema": {"type": "object"}}],
                                  lambda n, a: {"filas": [1]})
    assert r["texto"] and "No logré redactar" in r["texto"] and "consultar" in r["texto"]


def test_serializacion_compacta_de_resultados():
    s = claude._serializar({"filas": [{"a": 1, "b": None, "c": "", "d": [], "e": 0.14159265, "f": 1234.5678}], "nota": None})
    assert s == '{"filas":[{"a":1,"e":0.1416,"f":1234.57}]}'


# ── Configuración y Bitácora sólo para administradores ──────────────────────
def test_configuracion_y_bitacora_ocultas_y_prohibidas_para_operacion_y_consulta(sim):
    from fastapi.testclient import TestClient
    from app.main import app
    for rol in ("operacion", "consulta"):
        usuario = f"prueba-{rol}"
        with db.conn() as con:
            if not con.execute("SELECT 1 FROM usuarios WHERE usuario=?", (usuario,)).fetchone():
                db.crear_usuario(usuario, "clave-prueba-123", nombre=usuario, rol=rol, _con=con)
        with TestClient(app) as c:
            r = c.post("/login", data={"usuario": usuario, "password": "clave-prueba-123", "next": "/"}, follow_redirects=False)
            assert r.status_code == 303
            html = c.get("/").text
            assert 'href="/configuracion"' not in html and 'href="/bitacora"' not in html
            assert c.get("/configuracion", follow_redirects=False).status_code == 403
            assert c.get("/bitacora", follow_redirects=False).status_code == 403
            assert c.post("/api/config/probar-odoo").status_code == 403
            assert c.get("/api/config/memoria-consumo").status_code == 403
    with TestClient(app) as c:
        r = c.post("/login", data={"usuario": "admin", "password": "test1234", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        html = c.get("/").text
        assert 'href="/configuracion"' in html and 'href="/bitacora"' in html
        assert c.get("/bitacora").status_code == 200


def test_estado_plataforma_es_de_administradores(sim):
    from app.llm import tools
    with pytest.raises(PermissionError):
        tools.ejecutar("estado_plataforma", {}, "op", "operacion")
