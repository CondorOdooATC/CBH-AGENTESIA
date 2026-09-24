"""Cliente de la API de Claude (Messages) con bucle de herramientas, control de
presupuesto y bitácora de consumo. Implementado sobre httpx para no depender de
versiones del SDK.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from .. import db
from ..config import settings

API_URL = "https://api.anthropic.com/v1/messages"


class LLMError(RuntimeError):
    pass


class LLMDesactivado(LLMError):
    """No hay API key, o el presupuesto mensual se agotó."""


def disponible() -> bool:
    return settings.LLM_ENABLED and not db.presupuesto_agotado()


def _registrar_estado(ok: bool, origen: str, error: str = "", modelo: str = "") -> None:
    """Estado de la última llamada a Claude, visible en la interfaz: si la IA falló, la plataforma lo dice en vez de
    entregar en silencio contenido determinista."""
    try:
        actual = db.get_ajuste("llm_estado", {}) or {}
        if ok and actual.get("ok", True):
            return                                  # nada que actualizar (evita una escritura por llamada)
        db.set_ajuste("llm_estado", {"ok": ok, "fecha": db.now(), "origen": origen, "error": error[:400], "modelo": modelo})
    except Exception:  # noqa: BLE001
        pass


def estado() -> dict:
    return db.get_ajuste("llm_estado", {}) or {"ok": True}


def _headers() -> dict:
    return {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": settings.ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def mensaje(system: str, messages: list[dict], tools: list[dict] | None = None,
            max_tokens: int | None = None, modelo: str | None = None, origen: str = "copiloto",
            usuario: str = "", temperatura: float = 0.2, intentos: int = 4) -> dict:
    """Una llamada a /v1/messages. Devuelve el JSON de respuesta."""
    if not settings.LLM_ENABLED:
        raise LLMDesactivado("El asistente de lenguaje no está configurado (falta ANTHROPIC_API_KEY).")
    if db.presupuesto_agotado():
        raise LLMDesactivado(db.mensaje_presupuesto_agotado())
    cuerpo: dict[str, Any] = {
        "model": modelo or settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens or settings.ANTHROPIC_MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    # `temperature` está descontinuada en los modelos Claude 5 (la API responde 400 si se envía); se omite siempre
    if tools:
        cuerpo["tools"] = tools
    ultimo: Exception | None = None
    for i in range(intentos):
        try:
            with httpx.Client(timeout=180) as c:
                r = c.post(API_URL, headers=_headers(), json=cuerpo)
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(2 * (i + 1))
                ultimo = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                continue
            if r.status_code >= 400:
                _registrar_estado(False, origen, f"HTTP {r.status_code}: {r.text[:300]}", cuerpo["model"])
                raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            uso = data.get("usage", {})
            db.registrar_uso(cuerpo["model"], origen, int(uso.get("input_tokens", 0)),
                             int(uso.get("output_tokens", 0)), usuario)
            _registrar_estado(True, origen, modelo=cuerpo["model"])
            return data
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            ultimo = e
            time.sleep(2 * (i + 1))
    _registrar_estado(False, origen, f"Sin respuesta de la API de Claude: {ultimo}", cuerpo["model"])
    raise LLMError(f"No se pudo contactar a la API de Claude: {ultimo}")


def texto_de(respuesta: dict) -> str:
    return "\n".join(b.get("text", "") for b in respuesta.get("content", []) if b.get("type") == "text").strip()


def completar(system: str, prompt: str, origen: str = "agente", usuario: str = "",
              max_tokens: int | None = None, modelo: str | None = None, respaldo: str = "") -> str:
    """Texto simple. Si el LLM no está disponible devuelve ``respaldo`` en lugar de fallar."""
    try:
        r = mensaje(system, [{"role": "user", "content": prompt}], max_tokens=max_tokens,
                    modelo=modelo, origen=origen, usuario=usuario)
        return texto_de(r) or respaldo
    except LLMDesactivado as e:
        db.log("warn", "llm", "LLM no disponible", str(e))
        return respaldo
    except LLMError as e:
        db.log("error", "llm", "Error de la API de Claude", str(e))
        return respaldo


def completar_json(system: str, prompt: str, origen: str = "agente", usuario: str = "",
                   modelo: str | None = None) -> dict | list | None:
    """Pide JSON y lo parsea con tolerancia (quita fences, busca el primer bloque)."""
    txt = completar(system + "\nResponde ÚNICAMENTE con JSON válido, sin comentarios ni texto adicional.",
                    prompt, origen=origen, usuario=usuario, modelo=modelo, respaldo="")
    if not txt:
        return None
    t = txt.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    for abre, cierra in (("{", "}"), ("[", "]")):
        i, j = t.find(abre), t.rfind(cierra)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def bucle_herramientas(system: str, messages: list[dict], tools: list[dict],
                       ejecutar: Callable[[str, dict], Any], origen: str = "copiloto", usuario: str = "",
                       max_iteraciones: int = 12, modelo: str | None = None,
                       al_evento: Callable[[dict], None] | None = None) -> dict:
    """Bucle agéntico estándar: llama a Claude, ejecuta las herramientas que pida,
    devuelve los resultados y repite hasta que responda con texto final.

    ``ejecutar(nombre, argumentos)`` debe devolver algo serializable a JSON.
    ``al_evento`` recibe {"tipo": "herramienta"|"texto", ...} para streaming a la UI.
    Devuelve {"texto": str, "mensajes": [...], "herramientas": [...], "iteraciones": n}.
    """
    historial = list(messages)
    usadas: list[dict] = []
    texto_final = ""
    for it in range(max_iteraciones):
        resp = mensaje(system, historial, tools=tools, origen=origen, usuario=usuario, modelo=modelo)
        contenido = resp.get("content", [])
        historial.append({"role": "assistant", "content": contenido})
        texto_final = "\n".join(b.get("text", "") for b in contenido if b.get("type") == "text").strip()
        llamadas = [b for b in contenido if b.get("type") == "tool_use"]
        if resp.get("stop_reason") != "tool_use" or not llamadas:
            break
        resultados = []
        for ll in llamadas:
            nombre, args = ll["name"], ll.get("input", {}) or {}
            if al_evento:
                al_evento({"tipo": "herramienta", "nombre": nombre, "argumentos": args})
            t0 = time.time()
            try:
                out = ejecutar(nombre, args)
                ok = True
            except Exception as e:  # noqa: BLE001 — el error se devuelve al modelo para que se recupere
                out, ok = {"error": str(e)[:1500]}, False
            dur = round(time.time() - t0, 2)
            usadas.append({"nombre": nombre, "argumentos": args, "ok": ok, "segundos": dur,
                           "resumen": _resumen_resultado(out)})
            resultados.append({"type": "tool_result", "tool_use_id": ll["id"],
                               "content": _serializar(out), "is_error": not ok})
        historial.append({"role": "user", "content": resultados})
    return {"texto": texto_final, "mensajes": historial, "herramientas": usadas, "iteraciones": it + 1}


def _serializar(out: Any, limite: int = 30_000) -> str:
    try:
        s = json.dumps(out, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(out)
    if len(s) > limite:
        s = s[:limite] + f"\n…[recortado: {len(s) - limite} caracteres más]"
    return s


def _resumen_resultado(out: Any) -> str:
    if isinstance(out, dict):
        if "error" in out:
            return f"error: {out['error'][:120]}"
        if "filas" in out and isinstance(out["filas"], list):
            return f"{len(out['filas'])} filas"
        if "archivo" in out:
            return f"archivo {out['archivo']}"
        return f"{len(out)} claves"
    if isinstance(out, list):
        return f"{len(out)} elementos"
    return str(out)[:120]
