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


_BETA_RAZONAMIENTO = "thinking-binding-controls-2026-08-01"
_BLOQUES_RAZONAMIENTO = ("thinking", "redacted_thinking")


def _headers(beta: str = "") -> dict:
    h = {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": settings.ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if beta:
        h["anthropic-beta"] = beta
    return h


def _modelo_razona(modelo: str) -> bool:
    """Modelos Claude 5 (Opus 5 / 5.5, Sonnet 5, Fable, Mythos): razonan siempre antes de responder y devuelven
    bloques `thinking` firmados. Los Claude 4.x no aceptan la configuración de razonamiento adaptativo."""
    m = (modelo or "").lower()
    return any(p in m for p in ("opus-5", "sonnet-5", "haiku-5", "fable", "mythos"))


def _config_razonamiento(modelo: str) -> tuple[dict | None, str]:
    """(bloque `thinking` del cuerpo, cabecera beta). Con `drop_block` la API descarta en silencio un razonamiento
    firmado con otro prefijo (otro system, otras herramientas u otra conversación) en vez de responder 400."""
    if not _modelo_razona(modelo):
        return None, ""
    return {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}, _BETA_RAZONAMIENTO


def sin_razonamiento(messages: list[dict]) -> list[dict]:
    """Quita los bloques `thinking` de turnos anteriores (la API sólo los exige dentro del turno en curso, al
    devolver resultados de herramientas). Un razonamiento firmado en otra llamada, con otro system o cuando la
    cuenta aplica la vinculación estricta, hace que la API rechace la petición completa."""
    out = []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "assistant" and isinstance(c, list):
            c = [b for b in c if not (isinstance(b, dict) and b.get("type") in _BLOQUES_RAZONAMIENTO)]
            if not c:
                continue                            # el turno era sólo razonamiento (respuesta cortada): no aporta
        out.append({**m, "content": c})
    return out


_CACHE = {"type": "ephemeral"}
_ORIGENES_INFORME = ("agente1", "agente2", "planificador")     # redacción/razonamiento que lee la gente: esfuerzo «medium»


def _esfuerzo(origen: str) -> str:
    e = settings.ANTHROPIC_EFFORT_INFORMES if origen in _ORIGENES_INFORME else settings.ANTHROPIC_EFFORT
    return e if e in ("low", "medium", "high") else ""


def _rechaza_configuracion(texto: str) -> str:
    """Qué parte opcional de la petición rechazó la API (400): «razonamiento» (thinking / cabecera beta), «esfuerzo»
    (output_config.effort) o «cache» (cache_control). Vacío si el error es de otra cosa. Se reintenta sin esa parte."""
    t = (texto or "").lower()
    if any(k in t for k in ("thinking", "block_binding", "anthropic-beta", "beta")):
        return "razonamiento"
    if any(k in t for k in ("output_config", "effort")):
        return "esfuerzo"
    if "cache_control" in t or "cache" in t:
        return "cache"
    return ""


def _bloques_system(system: str | list, con_cache: bool) -> list[dict]:
    """El system va como lista de bloques: el primero (parte estable) lleva el punto de caché; los demás (fecha,
    usuario, contexto de pantalla) cambian por turno y quedan fuera de la caché."""
    bloques = [{"type": "text", "text": system}] if isinstance(system, str) else \
              [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in system if b]
    bloques = [b for b in bloques if (b.get("text") or "").strip()]
    for b in bloques:
        b.pop("cache_control", None)
    if con_cache and bloques:
        bloques[0]["cache_control"] = _CACHE
    return bloques


def _con_cache_mensajes(messages: list[dict], marcar_ultimo: bool) -> list[dict]:
    """Copia de los mensajes sin marcas de caché previas (la API admite 4 puntos como máximo) y, si se pide, con el
    último bloque del último mensaje marcado: en el bucle de herramientas cada llamada reutiliza todo lo anterior."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = [({k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b) for b in c]
        out.append({**m, "content": c})
    if marcar_ultimo and out:
        u = out[-1]
        if isinstance(u["content"], str):
            u["content"] = [{"type": "text", "text": u["content"], "cache_control": _CACHE}]
        elif isinstance(u["content"], list) and u["content"] and isinstance(u["content"][-1], dict):
            u["content"][-1] = {**u["content"][-1], "cache_control": _CACHE}
    return out


def _con_cache_tools(tools: list[dict]) -> list[dict]:
    out = [{k: v for k, v in t.items() if k != "cache_control"} for t in tools]
    out[-1]["cache_control"] = _CACHE
    return out


def mensaje(system: str | list, messages: list[dict], tools: list[dict] | None = None,
            max_tokens: int | None = None, modelo: str | None = None, origen: str = "copiloto",
            usuario: str = "", temperatura: float = 0.2, intentos: int = 4, cache_historial: bool = False) -> dict:
    """Una llamada a /v1/messages. Devuelve el JSON de respuesta.

    Ahorro de tokens (v1.3.11): caché de prompts en herramientas, system estable e historial (``cache_historial``,
    para bucles y chats: cada llamada relee lo ya enviado a 5–10 % del precio); esfuerzo del modelo por origen; y
    razonamiento adaptativo con ``drop_block`` para que un razonamiento firmado en otra llamada no invalide la petición.
    """
    if not settings.LLM_ENABLED:
        raise LLMDesactivado("El asistente de lenguaje no está configurado (falta ANTHROPIC_API_KEY).")
    if db.presupuesto_agotado():
        raise LLMDesactivado(db.mensaje_presupuesto_agotado())
    con_cache = bool(settings.ANTHROPIC_CACHE)
    cuerpo: dict[str, Any] = {
        "model": modelo or settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens or settings.ANTHROPIC_MAX_TOKENS,
        "system": _bloques_system(system, con_cache),
        "messages": _con_cache_mensajes(messages, con_cache and cache_historial),
    }
    # `temperature` está descontinuada en los modelos Claude 5 (la API responde 400 si se envía); se omite siempre
    if tools:
        cuerpo["tools"] = _con_cache_tools(tools) if con_cache else tools
    razonamiento, beta = _config_razonamiento(cuerpo["model"])
    if razonamiento:
        cuerpo["thinking"] = razonamiento
    esfuerzo = _esfuerzo(origen)
    if esfuerzo:
        cuerpo["output_config"] = {"effort": esfuerzo}
    ultimo: Exception | None = None
    quitadas: set[str] = set()
    i = 0
    while i < intentos:
        i += 1
        try:
            with httpx.Client(timeout=180) as c:
                r = c.post(API_URL, headers=_headers(beta), json=cuerpo)
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(2 * i)
                ultimo = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                continue
            parte = _rechaza_configuracion(r.text) if r.status_code == 400 else ""
            if parte and parte not in quitadas and len(quitadas) < 3:
                # la API de esta cuenta/modelo no acepta esa configuración opcional: se repite la llamada sin ella
                db.log("warn", "llm", f"La API rechazó la configuración opcional «{parte}»; se reintenta sin ella", r.text[:300])
                quitadas.add(parte)
                if parte == "razonamiento":
                    cuerpo.pop("thinking", None)
                    razonamiento, beta = None, ""
                elif parte == "esfuerzo":
                    cuerpo.pop("output_config", None)
                else:
                    cuerpo["system"] = _bloques_system(system, False)
                    cuerpo["messages"] = _con_cache_mensajes(messages, False)
                    if tools:
                        cuerpo["tools"] = tools
                i -= 1                                  # este reintento no consume un intento
                continue
            if r.status_code >= 400:
                _registrar_estado(False, origen, f"HTTP {r.status_code}: {r.text[:300]}", cuerpo["model"])
                raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            uso = data.get("usage", {}) or {}
            det = uso.get("output_tokens_details") or {}
            db.registrar_uso(cuerpo["model"], origen, int(uso.get("input_tokens", 0) or 0), int(uso.get("output_tokens", 0) or 0),
                             usuario, cache_escritura=int(uso.get("cache_creation_input_tokens", 0) or 0),
                             cache_lectura=int(uso.get("cache_read_input_tokens", 0) or 0),
                             razonamiento=int(det.get("thinking_tokens", 0) or 0))
            _registrar_estado(True, origen, modelo=cuerpo["model"])
            return data
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            ultimo = e
            time.sleep(2 * i)
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


_NUDGE_CORTE = ("Tu respuesta anterior se cortó por longitud y se descartó. Responde de forma más breve. Si estabas generando "
                "un Excel con muchas filas, NO pases las filas por el chat: usa `excel_desde_consulta` (guarda todas las filas "
                "directamente) y luego resume en pocas líneas.")
_NUDGE_VACIO = ("No enviaste texto para el usuario. Redacta ahora tu respuesta final, breve y con los datos que ya obtuviste "
                "de las herramientas.")


def _texto(contenido: list) -> str:
    return "\n".join(b.get("text", "") for b in contenido if isinstance(b, dict) and b.get("type") == "text").strip()


def _anexar_usuario(historial: list[dict], texto: str) -> None:
    """Añade una instrucción del sistema como bloque de texto del último mensaje de usuario (o como mensaje nuevo)."""
    if historial and historial[-1]["role"] == "user":
        c = historial[-1]["content"]
        if isinstance(c, str):
            historial[-1]["content"] = [{"type": "text", "text": c}, {"type": "text", "text": texto}]
        else:
            historial[-1]["content"] = list(c) + [{"type": "text", "text": texto}]
    else:
        historial.append({"role": "user", "content": texto})


def _respuesta_de_respaldo(usadas: list[dict]) -> str:
    """Nunca dejar al usuario sin texto: si el modelo agotó sus intentos sin redactar, se le explica qué pasó."""
    hechas = ", ".join(f"{u['nombre']} ({u['resumen']})" for u in usadas[-4:]) if usadas else "ninguna"
    return ("No logré redactar la respuesta en este intento (el modelo terminó sin texto). Herramientas ejecutadas: "
            f"{hechas}. Vuelve a preguntar de forma más acotada o, si pediste un Excel grande, pídelo como «Excel de …» "
            "para que se genere directamente desde la consulta.")


def bucle_herramientas(system: str | list, messages: list[dict], tools: list[dict],
                       ejecutar: Callable[[str, dict], Any], origen: str = "copiloto", usuario: str = "",
                       max_iteraciones: int = 12, modelo: str | None = None,
                       al_evento: Callable[[dict], None] | None = None) -> dict:
    """Bucle agéntico estándar: llama a Claude, ejecuta las herramientas que pida,
    devuelve los resultados y repite hasta que responda con texto final.

    ``ejecutar(nombre, argumentos)`` debe devolver algo serializable a JSON.
    ``al_evento`` recibe {"tipo": "herramienta"|"texto", ...} para streaming a la UI.
    Devuelve {"texto": str, "mensajes": [...], "herramientas": [...], "iteraciones": n}.

    Robustez (v1.3.11): si la respuesta se corta por ``max_tokens`` a media herramienta, se reintenta con más espacio
    (hasta ANTHROPIC_MAX_TOKENS_TOPE) y, si vuelve a cortarse, se le pide al modelo una salida más corta; si termina
    sin texto, se le pide redactarlo; si aun así no hay texto, se devuelve una explicación en lugar de «(sin respuesta)».
    """
    historial = list(messages)
    usadas: list[dict] = []
    texto_final = ""
    max_tokens = settings.ANTHROPIC_MAX_TOKENS
    tope = settings.ANTHROPIC_MAX_TOKENS_TOPE
    empujones = 0                                   # instrucciones correctivas (corte o respuesta vacía); máximo 2
    it = 0
    while it < max_iteraciones:
        it += 1
        resp = mensaje(system, historial, tools=tools, origen=origen, usuario=usuario, modelo=modelo,
                       max_tokens=max_tokens, cache_historial=True)
        contenido = resp.get("content", []) or []
        llamadas = [b for b in contenido if b.get("type") == "tool_use"]
        if resp.get("stop_reason") == "max_tokens":
            # se cortó: si fue a media herramienta el bloque viene incompleto y no sirve; con texto parcial, se conserva
            if llamadas or not _texto(contenido):
                if max_tokens < tope:
                    max_tokens = min(tope, max_tokens * 2)          # mismo prefijo: la caché absorbe el reintento
                    it -= 1
                    continue
                if empujones < 2:
                    empujones += 1
                    _anexar_usuario(historial, _NUDGE_CORTE)
                    continue
            texto_final = _texto(contenido)
            historial.append({"role": "assistant", "content": [b for b in contenido if b.get("type") != "tool_use"] or contenido})
            break
        historial.append({"role": "assistant", "content": contenido})
        texto_final = _texto(contenido)
        if resp.get("stop_reason") != "tool_use" or not llamadas:
            if not texto_final and empujones < 2:
                empujones += 1
                historial.pop()                     # un turno sólo de razonamiento no aporta al hilo
                _anexar_usuario(historial, _NUDGE_VACIO)
                continue
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
    if not texto_final:
        texto_final = _respuesta_de_respaldo(usadas)
        db.log("warn", "llm", "El modelo terminó sin texto; se devolvió una respuesta de respaldo",
               f"origen={origen} iteraciones={it} herramientas={[u['nombre'] for u in usadas]}", usuario)
    return {"texto": texto_final, "mensajes": historial, "herramientas": usadas, "iteraciones": it}


def _serializar(out: Any, limite: int = 24_000) -> str:
    """JSON compacto (sin espacios ni claves vacías): el resultado de cada herramienta vuelve al modelo como entrada."""
    try:
        s = json.dumps(_compactar(out), ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        s = str(out)
    if len(s) > limite:
        s = s[:limite] + f"\n…[recortado: {len(s) - limite} caracteres más]"
    return s


def _compactar(v: Any):
    """Quita claves con None/""/[]/{} y redondea flotantes largos: mismo contenido útil, menos tokens."""
    if isinstance(v, dict):
        return {k: _compactar(x) for k, x in v.items() if x is not None and x != "" and x != [] and x != {}}
    if isinstance(v, (list, tuple)):
        return [_compactar(x) for x in v]
    if isinstance(v, float):
        return round(v, 4) if abs(v) < 1 else round(v, 2)
    return v


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
