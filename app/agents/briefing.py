"""Briefing ejecutivo diario: une los dos agentes, la cola de acciones y el consumo de la plataforma."""
from __future__ import annotations

from .. import db
from ..llm import claude, prompts
from .base import compacto, mxn
from . import autonomia


def generar(usuario: str = "", con_llm: bool = True) -> dict:
    a1 = db.get_ajuste("agente1_ultimo", {}) or {}
    a2 = db.get_ajuste("agente2_ultimo", {}) or {}
    pendientes = [a for e in autonomia.ESTADOS_PENDIENTES for a in db.acciones(estado=e, limite=500)]
    uso = db.uso_periodo()
    contexto = {
        "fecha": db.now(), "agente1": {k: a1.get(k) for k in ("corrida_id", "kpis", "agregados", "riesgos", "basculas", "fecha")},
        "agente1_top": (a1.get("top_hallazgos") or [])[:12],
        "agente2": {k: a2.get(k) for k in ("corrida_id", "kpis", "alertas", "compras", "transferencias", "caducidades", "fecha")},
        "acciones_pendientes": [{k: a.get(k) for k in ("id", "agente", "tipo", "titulo", "riesgo", "impacto")} for a in pendientes[:40]],
        "acciones_pendientes_total": len(pendientes),
        "acciones_conteo": db.resumen_acciones(), "autonomia": {"nivel": autonomia.nivel(), "nombre": autonomia.NIVELES[autonomia.nivel()]},
        "plataforma": {"tokens_mes_pct": uso["pct_entrada"], "costo_usd_mes": uso["costo_usd"]},
    }
    respaldo = _deterministico(a1, a2, pendientes)
    texto = respaldo
    if con_llm and claude.disponible():
        texto = claude.completar(prompts.SISTEMA_BRIEFING, f"Datos (JSON):\n{compacto(contexto)}\n\nRedacta el briefing.",
                                 origen="briefing", usuario=usuario, respaldo=respaldo)
    out = {"texto": texto, "fecha": db.now(), "agente1_corrida": a1.get("corrida_id"), "agente2_corrida": a2.get("corrida_id"),
           "pendientes": len(pendientes)}
    db.set_ajuste("ultimo_briefing", out)
    db.log("info", "briefing", "Briefing ejecutivo generado", f"pendientes={len(pendientes)}", usuario)
    return out


def _deterministico(a1: dict, a2: dict, pendientes: list[dict]) -> str:
    k1, k2 = a1.get("kpis") or {}, a2.get("kpis") or {}
    L = ["# Briefing ejecutivo", "", "## Lo que importa hoy"]
    if k1:
        L.append(f"- Consumo: {k1.get('criticos', 0)} hallazgos críticos y {k1.get('patrones', 0)} patrones; "
                 f"{mxn(k1.get('importe_riesgo', 0) + k1.get('importe_patrones', 0))} en riesgo. "
                 f"Mayor riesgo: {k1.get('top_riesgo_hospital') or '—'} / {k1.get('top_riesgo_auxiliar') or '—'}.")
    if k2:
        L.append(f"- Abasto: {k2.get('desabasto', 0) + k2.get('critico', 0)} ubicaciones en riesgo de desabasto, "
                 f"compra sugerida {mxn(k2.get('importe_compra', 0))}, caducidades en riesgo {mxn(k2.get('caducidad_riesgo', 0))}, "
                 f"capital inmovilizado {mxn(k2.get('valor_exceso', 0))}.")
    L.append(f"- Decisiones pendientes: {len(pendientes)} acciones esperan aprobación"
             + (f" ({sum(1 for a in pendientes if a['riesgo']=='alto')} de riesgo alto)." if pendientes else "."))
    if not k1 and not k2:
        L.append("- Aún no hay corridas de los agentes. Ejecuta el Agente 1 y el Agente 2 desde el panel.")
    L += ["", "## Decisiones pendientes"] + [f"- #{a['id']} · {a['titulo']} (riesgo {a['riesgo']})" for a in pendientes[:10]]
    return "\n".join(L)
