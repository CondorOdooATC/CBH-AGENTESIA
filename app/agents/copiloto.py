"""Copiloto conversacional: chat con herramientas sobre Odoo, agentes, Excel y acciones."""
from __future__ import annotations

from .. import db
from ..config import settings
from ..llm import claude, prompts, tools
from .base import contexto_aprendizaje


_PAGINAS_ADMIN = ("configuracion", "bitacora")      # pantallas sólo de administradores: su contexto no se abre a otros roles


def contexto_para(contexto: dict | None, rol: str = "consulta") -> tuple[str, str]:
    """(nombre del interlocutor, bloque de contexto) para conversar con un agente o sobre un caso concreto."""
    from .base import compacto
    if not contexto:
        return "Copiloto", ""
    if contexto.get("pagina") in _PAGINAS_ADMIN and rol not in ("admin", "condor"):
        return "Copiloto", ""
    if contexto.get("caso_id"):
        c = db.caso(int(contexto["caso_id"]))
        if c:
            return (f"Agente Investigador · caso #{c['id']}",
                    f"ESTÁS CONVERSANDO SOBRE EL CASO #{c['id']} «{c['titulo']}». Expediente completo (JSON):\n{compacto(c, 12000)}\n"
                    "Responde como el investigador que lo armó, USANDO su evidencia y sus herramientas (ver_caso, consultar_consumo, "
                    "consultar_existencias, investigar_hallazgo): qué hechos hay, qué hipótesis quedan por verificar, qué dato falta y qué haría "
                    "primero. Si el usuario aporta una explicación (p. ej. «el médico confirma que fue una cirugía de 5 horas»), NO la conviertas "
                    "en hecho verificado ni en regla global: regístrala con `registrar_aclaracion` (alcance «caso» salvo que el usuario diga que "
                    "aplica a un actor, a un producto en ese hospital o a todo el hospital), di explícitamente que queda como declaración de esa "
                    "persona, explica qué verificaría para confirmarla y cómo cambiaría la plausibilidad de las hipótesis. Sólo ofrece "
                    "`resolver_caso` cuando la persona lo pida; mantén separadas la severidad (gravedad) y la confianza (cuánta evidencia hay).")
    if contexto.get("agente") == "consumo":
        a1 = db.get_ajuste("agente1_ultimo", {}) or {}
        ctx = {k: a1.get(k) for k in ("corrida_id", "fecha", "kpis", "exposicion", "agregados", "casos", "basculas",
                                       "modo_respaldo", "origen_datos", "reglas_desactivadas")}
        ctx["top_hallazgos"] = (a1.get("top_hallazgos") or [])[:15]
        nota = ("\nOJO: la corrida fue en MODO DE RESPALDO sobre movimientos de inventario (stock.move.line): no hay folio médico, médico, "
                "auxiliar, báscula ni importe; el «folio» es el nombre de la entrega y el «hospital» la ubicación destino. No hables de "
                "facturación al IMSS, cirugías ni duplicados; dilo si te preguntan por qué los datos se ven así." if a1.get("modo_respaldo") else "")
        return ("Agente · Control de consumo",
                f"ERES EL AGENTE DE CONTROL DE CONSUMO hablando con el usuario. Tu última corrida (JSON):\n{compacto(ctx, 14000)}\n"
                "Habla en primera persona de lo que encontraste, con cifras; usa las herramientas para profundizar (consultar_consumo, "
                "listar_casos, ver_caso, investigar_hallazgo) y para aprender de lo que te digan (registrar_aprendizaje, clasificar_hallazgo)." + nota)
    if contexto.get("agente") == "demanda":
        a2 = db.get_ajuste("agente2_ultimo", {}) or {}
        ctx = {k: a2.get(k) for k in ("corrida_id", "fecha", "kpis", "plan_razonado", "autoevaluacion", "retrasadas")}
        ctx["alertas"] = (a2.get("alertas") or [])[:20]
        ctx["compras"] = (a2.get("compras") or [])[:15]
        return ("Agente · Abasto y demanda",
                f"ERES EL AGENTE DE ABASTO Y DEMANDA hablando con el usuario. Tu último plan (JSON):\n{compacto(ctx, 14000)}\n"
                "Habla en primera persona de lo que anticipas y por qué; si el usuario pregunta '¿y si…?', responde con escenarios "
                "cuantificados (usa listar_resurtido, consultar_existencias, consultar_consumo); propón acciones con proponer_accion "
                "y registra lo que aprendas con registrar_aprendizaje.")
    if contexto.get("pagina"):
        pag = str(contexto["pagina"])
        bloque = _contexto_pagina(pag)
        nombres = {"hoy": "Agentes CBH · panorama de hoy", "decisiones": "Agentes CBH · decisiones", "casos": "Agente Investigador · casos",
                   "hallazgos": "Agente · Control de consumo (hallazgos)", "excel": "Copiloto · Excel", "configuracion": "Copiloto · configuración",
                   "bitacora": "Copiloto · bitácora", "copiloto": "Copiloto"}
        return nombres.get(pag, "Copiloto"), bloque
    return "Copiloto", ""


def _contexto_pagina(pagina: str) -> str:
    """Lo que la persona tiene en pantalla, para que el chat hable de eso (todas las pantallas conversan)."""
    from .base import compacto
    from . import autonomia
    if pagina == "hoy":
        a1 = db.get_ajuste("agente1_ultimo", {}) or {}
        a2 = db.get_ajuste("agente2_ultimo", {}) or {}
        ctx = {"consumo": {k: a1.get(k) for k in ("fecha", "kpis", "exposicion")}, "abasto": {k: a2.get(k) for k in ("fecha", "kpis")},
               "decisiones_pendientes": db.resumen_acciones(), "casos_abiertos": [{k: c.get(k) for k in ("id", "titulo", "severidad", "confianza")} for c in db.casos(estado="abierto", limite=8)]}
        return ("EL USUARIO ESTÁ EN LA PANTALLA «HOY» (panorama). Resumen (JSON):\n" + compacto(ctx, 8000) +
                "\nHabla en nombre de los dos agentes: qué es lo más urgente hoy, qué decisiones esperan, qué casos importan. Usa herramientas para detalles.")
    if pagina == "decisiones":
        pend = [a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, limite=60)]
        ctx = [{k: a.get(k) for k in ("id", "tipo", "titulo", "riesgo", "estado", "efecto", "motivo", "fecha_requerida", "version")} | {"impacto": a.get("impacto")} for a in pend[:40]]
        return ("EL USUARIO ESTÁ EN «DECISIONES» (cola de aprobación). Pendientes (JSON):\n" + compacto(ctx, 12000) +
                "\nExplica cada propuesta con sus cifras (antes/después, disponibilidad del origen, si la compra llega a tiempo), qué pasa si se aprueba o "
                "rechaza y en qué orden conviene decidir. Puedes proponer o ajustar con proponer_accion; nunca apruebes tú: eso lo hace la persona.")
    if pagina in ("casos", "hallazgos"):
        cs = [{k: c.get(k) for k in ("id", "titulo", "severidad", "confianza", "estado", "impacto_mxn")} for c in db.casos(limite=25)]
        return ("EL USUARIO ESTÁ EN «" + pagina.upper() + "». Casos recientes (JSON):\n" + compacto(cs, 8000) +
                "\nEres el Agente Investigador: ayuda a priorizar (severidad y confianza por separado), abre un caso con ver_caso, investiga con investigar_hallazgo "
                "y registra explicaciones de personas con registrar_aclaracion (declaraciones, no hechos).")
    if pagina == "excel":
        rep = [{k: r.get(k) for k in ("id", "titulo", "archivo", "creado_en", "filas")} for r in db.reportes(20)]
        return ("EL USUARIO ESTÁ EN «EXCEL». Reportes recientes (JSON):\n" + compacto(rep, 4000) +
                "\nGenera Excels a la medida con excel_desde_consulta (consumo, existencias, resurtido, hallazgos, acciones…) y explica qué contiene cada uno.")
    if pagina == "configuracion":
        return ("EL USUARIO ESTÁ EN «CONFIGURACIÓN». Explica niveles de autonomía, políticas, mapeo de campos y roles. No reveles claves ni secretos; "
                "la configuración del modelo de lenguaje sólo la cambia Ingeniería Cóndor. Usa estado_plataforma si hace falta.")
    if pagina == "bitacora":
        with db.conn() as con:
            rows = [dict(r) for r in con.execute("SELECT nivel, origen, mensaje, detalle, usuario, creado_en FROM bitacora ORDER BY id DESC LIMIT 40")]
        return ("EL USUARIO ESTÁ EN «BITÁCORA». Últimos eventos (JSON):\n" + compacto(rows, 8000) +
                "\nExplica qué pasó, qué falló y qué hacer; no inventes eventos que no estén aquí.")
    return ""


def responder(conversacion_id: int, texto: str, usuario: str, rol: str = "consulta",
              al_evento=None, contexto: dict | None = None) -> dict:
    db.guardar_mensaje(conversacion_id, "user", texto, texto)
    historial = _historial(conversacion_id)
    nombre, bloque = contexto_para(contexto, rol)
    if not claude.disponible():
        respuesta = db.mensaje_presupuesto_agotado() if (settings.LLM_ENABLED and db.presupuesto_agotado()) else _sin_llm(texto, usuario, rol, contexto)
        db.guardar_mensaje(conversacion_id, "assistant", respuesta, respuesta)
        return {"texto": respuesta, "herramientas": [], "interlocutor": nombre}
    # system en dos bloques: el estable (instrucciones del copiloto) se sirve desde la caché de prompts; el variable
    # (usuario, fecha, notas de aprendizaje, pantalla) cambia por turno y es pequeño
    system = [prompts.SISTEMA_COPILOTO,
              f"Usuario actual: {usuario} (rol {rol}). Fecha/hora: {db.now()}.\n"
              f"Notas de aprendizaje vigentes:\n{contexto_aprendizaje(limite=40)}" + (f"\n\n{bloque}" if bloque else "")]
    try:
        r = claude.bucle_herramientas(system, historial, tools.HERRAMIENTAS,
                                      lambda n, a: tools.ejecutar(n, a, usuario, rol),
                                      origen="copiloto", usuario=usuario, al_evento=al_evento)
    except claude.LLMError as e:
        msg = f"No pude completar la consulta con el asistente de lenguaje: {e}"
        db.guardar_mensaje(conversacion_id, "assistant", msg, msg)
        return {"texto": msg, "herramientas": []}
    # guardar sólo los turnos nuevos (a partir del último user), sin los bloques de razonamiento del modelo: están
    # firmados para esta llamada y la API los rechaza si se reenvían con otro system o desde otra conversación
    nuevos = claude.sin_razonamiento(r["mensajes"][len(historial):])
    for m in nuevos:
        db.guardar_mensaje(conversacion_id, m["role"], m["content"],
                           r["texto"] if m["role"] == "assistant" and m is nuevos[-1] else "")
    _titular(conversacion_id, texto)
    return {"texto": r["texto"], "herramientas": r["herramientas"], "iteraciones": r["iteraciones"], "interlocutor": nombre}


_TURNOS_CON_RESULTADOS = 2       # turnos recientes cuyos resultados de herramientas se reenvían completos al modelo


def _historial(conversacion_id: int, max_turnos: int = 12) -> list[dict]:
    """Historial que se reenvía a Claude en cada turno. Ahorro de tokens (v1.3.11): sin bloques de razonamiento de
    turnos anteriores, últimos 12 intercambios, y los resultados de herramientas de turnos viejos se sustituyen por
    un resumen de una línea (la respuesta redactada, que es lo que el usuario vio, se conserva íntegra)."""
    msgs = db.mensajes(conversacion_id, limite=400)
    out = []
    for m in msgs:
        c = m["contenido"]
        if m["rol"] == "user" and isinstance(c, str):
            out.append({"role": "user", "content": c})
        elif m["rol"] in ("user", "assistant"):
            out.append({"role": m["rol"], "content": c})
    out = claude.sin_razonamiento(out)
    # recortar manteniendo pares coherentes
    if len(out) > max_turnos * 2:
        out = out[-max_turnos * 2:]
        while out and out[0]["role"] != "user":
            out.pop(0)
    _resumir_resultados_viejos(out)
    # limpiar tool_result huérfanos al inicio
    while out and isinstance(out[0].get("content"), list) and any(b.get("type") == "tool_result" for b in out[0]["content"]):
        out.pop(0)
    # un assistant con tool_use debe ir seguido de sus tool_result; si no, se descarta (turno interrumpido)
    limpio: list[dict] = []
    for i, m in enumerate(out):
        if m["role"] == "assistant" and isinstance(m.get("content"), list) and any(b.get("type") == "tool_use" for b in m["content"]):
            sig = out[i + 1] if i + 1 < len(out) else None
            if not (sig and sig["role"] == "user" and isinstance(sig.get("content"), list)
                    and any(b.get("type") == "tool_result" for b in sig["content"])):
                continue
        limpio.append(m)
    return limpio


def _resumir_resultados_viejos(out: list[dict]) -> None:
    """Los tool_result de turnos anteriores a los últimos _TURNOS_CON_RESULTADOS pasan a una línea: su contenido ya se
    reflejó en la respuesta redactada y reenviarlo entero cada turno multiplica la entrada."""
    posiciones = [i for i, m in enumerate(out) if m["role"] == "user" and isinstance(m.get("content"), str)]
    if len(posiciones) <= _TURNOS_CON_RESULTADOS:
        return
    corte = posiciones[-_TURNOS_CON_RESULTADOS]
    for m in out[:corte]:
        if m["role"] == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), str) and len(b["content"]) > 300:
                    b["content"] = b["content"][:200] + " …[resultado anterior resumido; vuelve a consultar si lo necesitas]"


def _titular(conversacion_id: int, texto: str) -> None:
    with db.conn() as con:
        r = con.execute("SELECT titulo FROM conversaciones WHERE id=?", (conversacion_id,)).fetchone()
        if r and r["titulo"] in ("Nueva conversación", None, ""):
            con.execute("UPDATE conversaciones SET titulo=? WHERE id=?", (texto[:60], conversacion_id))


def _parece_aclaracion(texto: str) -> bool:
    t = texto.lower()
    return any(k in t for k in ("confirma", "confirmó", "me dijo", "fue una", "fue un ", "se usó", "se uso", "sí se", "si se ", "justific",
                                "la razón", "la razon", "porque", "explic", "aclar")) and "?" not in t


def _sin_llm(texto: str, usuario: str, rol: str, contexto: dict | None = None) -> str:
    """Modo básico sin Claude: comandos directos para que la plataforma siga siendo útil."""
    t = texto.lower()
    if contexto and contexto.get("caso_id"):
        c = db.caso(int(contexto["caso_id"]))
        if c:
            e = c.get("expediente") or {}
            hip = e.get("hipotesis", [])
            # una explicación aportada por la persona se registra como ACLARACIÓN declarada (no como hecho ni regla)
            if _parece_aclaracion(texto):
                aid = db.agregar_aclaracion(c["id"], usuario, texto, "caso", c.get("entidades") or {}, fuente="chat")
                db.log("info", "casos", f"Aclaración #{aid} registrada en el caso #{c['id']}", texto[:200], usuario)
                verif = "; ".join(h.get("como_verificar", "") for h in hip[:2] if h.get("como_verificar"))
                return (f"Registré tu explicación como **aclaración declarada por {usuario}** (alcance: este caso, aclaración #{aid}). "
                        "No la convierto en hecho verificado ni en regla general: queda en el expediente con tu nombre y fecha.\n\n"
                        f"Para verificarla haría falta: {verif or 'bitácora de quirófano y confirmación del médico'}.\n\n"
                        "Si después de verificarla quieres cerrar el caso, usa **Resolver** con la justificación; si aplica a más casos "
                        "(un médico, un producto en este hospital), regístrala con ese alcance en «Aclaraciones» del caso.")
            if any(k in t for k in ("primero", "por dónde", "por donde", "empiezo")):
                h0 = hip[0] if hip else {}
                return (f"Empezaría por la hipótesis más plausible: **{h0.get('hipotesis', 'diferencia no conciliada')}** → {h0.get('como_verificar', '')}. "
                        f"Datos que faltan: {'; '.join(e.get('datos_faltantes', [])) or 'ninguno'}. Responsable: {c.get('responsable')}.")
            if "pesa" in t or "evidencia" in t or "hecho" in t:
                return "**Hechos con más peso (evidencia en Odoo):**\n" + "\n".join(f"- {x}" for x in e.get("evidencia", [])[:4]) + \
                       f"\n\nSeveridad **{e.get('severidad', c.get('severidad'))}** (gravedad del importe/riesgo) y confianza **{c.get('confianza')}** (cuánta evidencia hay) se evalúan por separado."
            if "contabilidad" in t or "factur" in t:
                return ("A Contabilidad le pediría: el folio con sus líneas y el importe facturable, confirmación de si ya se facturó al IMSS, "
                        "y que retenga el ajuste hasta conciliar con báscula y existencias. Puedo abrir un ticket de Helpdesk como propuesta (requiere aprobación).")
            return (f"**Caso #{c['id']}** (modo determinista, sin Claude)\n\n**Hechos:**\n" + "\n".join(f"- {x}" for x in e.get("evidencia", [])[:6])
                    + "\n\n**Hipótesis por verificar:**\n" + "\n".join(f"- [{h.get('plausibilidad')}] {h.get('hipotesis')} — {h.get('como_verificar', '')}" for h in hip)
                    + "\n\n**Datos faltantes:** " + "; ".join(e.get("datos_faltantes", []))
                    + ("\n\n**Aclaraciones declaradas (no verificadas):** " + "; ".join(f"{a['usuario']}: {a['texto'][:120]}" for a in e.get("aclaraciones", [])) if e.get("aclaraciones") else "")
                    + f"\n\n**Acción:** {c.get('accion_recomendada')} ({c.get('responsable')})")
    if contexto and contexto.get("pagina"):
        from . import autonomia
        pag = contexto["pagina"]
        if pag == "decisiones":
            pend = [a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, limite=200)]
            num = [x for x in t.replace("#", " ").split() if x.isdigit()]
            if num:
                a = db.accion(int(num[0]))
                if a:
                    i = a.get("impacto") or {}
                    return (f"**#{a['id']} · {a['titulo']}**\n\n- Efecto: {a.get('efecto')}\n- Motivo: {a.get('motivo')}\n- Riesgo {a.get('riesgo')} · versión {a.get('version')}"
                            + (f"\n- Cobertura destino antes/después: {i.get('cobertura_destino_antes')} → {i.get('cobertura_destino_despues')} días" if i.get("cobertura_destino_antes") is not None else "")
                            + (f"\n- Origen: {i.get('stock_origen')} en existencia, reserva {i.get('reserva_origen')}, puede ceder {i.get('disponible_origen')}" if i.get("disponible_origen") is not None else "")
                            + (f"\n- ⚠ La compra llegaría el {i.get('fecha_llegada_estimada')}, después del quiebre ({i.get('fecha_quiebre')})" if i.get("llega_a_tiempo") is False else "")
                            + (f"\n- Importe {i.get('importe'):,.0f} MXN" if i.get("importe") else ""))
            urg = sorted(pend, key=lambda a: str(a.get("fecha_requerida") or "9999"))[:5]
            return ("Sin Claude te doy lo esencial (modo determinista). Pendientes más urgentes:\n" +
                    "\n".join(f"- #{a['id']} {a['titulo']} · riesgo {a['riesgo']}" + (f" · requerida {a['fecha_requerida'][:10]}" if a.get("fecha_requerida") else "") for a in urg)
                    + "\n\nPregunta por un número (#id) para ver su efecto, motivo y cifras.")
        if pag == "hoy":
            a1 = db.get_ajuste("agente1_ultimo", {}) or {}; a2 = db.get_ajuste("agente2_ultimo", {}) or {}
            return (f"Panorama (modo determinista):\n\n- Consumo: {a1.get('kpis')}\n- Abasto: {a2.get('kpis')}\n- Decisiones: {db.resumen_acciones()}"
                    "\n\nEntra a cada agente para conversar sobre su corrida.")
        if pag in ("casos", "hallazgos"):
            cs = db.casos(estado="abierto", limite=8)
            return "Casos abiertos (modo determinista):\n" + "\n".join(f"- #{c['id']} {c['titulo']} · severidad {c['severidad']} · confianza {c['confianza']}" for c in cs) + "\n\nAbre un caso para conversar con el Investigador sobre él."
        return ("El asistente de lenguaje no está configurado. Aun así puedes usar los botones de esta pantalla o escribir «estado», "
                "«ejecutar agente 1» o «ejecutar agente 2».")
    if contexto and contexto.get("agente") == "consumo":
        a1 = db.get_ajuste("agente1_ultimo", {}) or {}
        return "Sin Claude sólo puedo mostrarte mi último informe:\n\n" + (a1.get("informe") or "Aún no he corrido.")
    if contexto and contexto.get("agente") == "demanda":
        a2 = db.get_ajuste("agente2_ultimo", {}) or {}
        return "Sin Claude sólo puedo mostrarte mi último plan:\n\n" + (a2.get("informe") or "Aún no he corrido.")
    if "agente 1" in t or "anomal" in t or "consumo" in t and "ejecut" in t:
        r = tools.ejecutar("ejecutar_agente", {"agente": "consumo"}, usuario, rol)
        return f"Ejecuté el Agente 1. {r['kpis']}\n\nExcel: {r['reporte']['url'] if r.get('reporte') else '—'}\n\n{r['informe']}"
    if "agente 2" in t or "pronost" in t or "resurt" in t:
        r = tools.ejecutar("ejecutar_agente", {"agente": "demanda"}, usuario, rol)
        return f"Ejecuté el Agente 2. {r['kpis']}\n\nExcel: {r['reporte']['url'] if r.get('reporte') else '—'}\n\n{r['informe']}"
    if "estado" in t or "status" in t:
        return f"```\n{tools.ejecutar('estado_plataforma', {}, usuario, rol)}\n```"
    return ("El asistente de lenguaje no está configurado (falta ANTHROPIC_API_KEY o se agotó el presupuesto). "
            "Aun así puedes escribir «ejecutar agente 1», «ejecutar agente 2» o «estado», o usar los botones del panel.")
