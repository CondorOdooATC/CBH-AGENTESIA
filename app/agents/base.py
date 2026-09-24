"""Utilidades compartidas por los agentes."""
from __future__ import annotations

import json
import time
from typing import Any

import pandas as pd

from .. import db


def contexto_aprendizaje(ambitos: list[str] | None = None, limite: int = 60) -> str:
    """Notas de aprendizaje activas, para inyectarlas al LLM y respetarlas."""
    notas = db.aprendizaje(limite=limite)
    if ambitos:
        notas = [n for n in notas if n["ambito"] in ambitos or n["ambito"] == "global"]
    if not notas:
        return "Sin notas de aprendizaje registradas."
    return "\n".join(f"- [{n['ambito']}{(' · ' + n['clave']) if n['clave'] else ''}] {n['nota']}" for n in notas)


def compacto(obj: Any, limite: int = 45_000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=_default)
    return s if len(s) <= limite else s[:limite] + "…"


def _default(o):
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if hasattr(o, "item"):
        try:
            return o.item()
        except (ValueError, AttributeError):
            pass
    return str(o)


def mxn(v: float | None) -> str:
    try:
        return f"${float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def df_registros(df: pd.DataFrame, n: int | None = None, cols: list[str] | None = None) -> list[dict]:
    if df is None or df.empty:
        return []
    d = df[cols] if cols else df
    d = d.head(n) if n else d
    out = []
    for r in d.to_dict("records"):
        out.append({k: (None if (isinstance(v, float) and pd.isna(v)) else
                        (v.isoformat() if isinstance(v, pd.Timestamp) else v)) for k, v in r.items()})
    return out


class Cronometro:
    """Envuelve el callback de progreso para medir cuánto tarda cada paso de una corrida (lectura de Odoo, motor,
    investigación con Claude, informe, Excel…). Así el tiempo total deja de ser una caja negra."""

    def __init__(self, progreso=None):
        self._progreso = progreso or (lambda *a: None)
        self.pasos: list[dict] = []
        self._actual: str | None = None
        self._t = time.time()

    def __call__(self, titulo: str, detalle: str = ""):
        ahora = time.time()
        if self._actual is not None:
            self.pasos.append({"paso": self._actual, "segundos": round(ahora - self._t, 1)})
        self._actual, self._t = titulo, ahora
        self._progreso(titulo, detalle)

    def cerrar(self) -> list[dict]:
        if self._actual is not None:
            self.pasos.append({"paso": self._actual, "segundos": round(time.time() - self._t, 1)})
            self._actual = None
        return self.pasos

    def resumen(self, top: int = 5) -> str:
        pasos = sorted(self.pasos, key=lambda x: -x["segundos"])[:top]
        return " · ".join(f"{p['paso'][:48]}: {p['segundos']:g} s" for p in pasos)
