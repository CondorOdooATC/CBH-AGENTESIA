"""Agente investigador: convierte un hallazgo estadístico en un EXPEDIENTE razonado.

No se limita a narrar lo que dice el motor: abre el folio, revisa el historial del auxiliar y del
médico frente a sus pares, lee las lecturas de báscula, localiza el lote en existencias y en otras
unidades, revisa el contexto del día, busca casos similares ya resueltos y las notas de aprendizaje,
y con todo eso plantea hipótesis con probabilidad, evidencia a favor y en contra, una conclusión con
nivel de confianza, el impacto económico y la acción recomendada con responsable.

Con Claude disponible el razonamiento es un bucle de herramientas (ReAct). Sin Claude, se arma un
expediente determinista con la misma evidencia y las hipótesis típicas de cada regla.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import numpy as np
import pandas as pd

from .. import db
from ..config import settings
from ..llm import claude, prompts
from ..odoo.client import OdooError, get_client
from .base import compacto, contexto_aprendizaje

# ── prompt ──────────────────────────────────────────────────────────────────
SISTEMA_INVESTIGADOR = f"""
Eres el Agente Investigador de CBH · Agentes de IA (Ingeniería Cóndor). Recibes un hallazgo del motor
estadístico y tu trabajo es INVESTIGARLO con las herramientas antes de concluir, como lo haría un auditor
operativo experto en anestesia hospitalaria e inventarios médicos.

{prompts.CONTEXTO_CBH}

Método obligatorio:
1. Abre el folio y revisa TODAS sus líneas (¿la cantidad es coherente con la duración y el procedimiento?).
2. Compara al auxiliar y al médico con sus pares y con su propio historial (¿es un caso aislado o un patrón?).
3. Si hay báscula, concilia gramos ↔ mililitros con la densidad del producto; considera cambio de frasco,
   recarga, tara o captura manual antes de hablar de merma.
4. Si hay lote, verifica dónde tiene existencias y dónde se ha consumido.
5. Revisa el contexto del día (urgencias, jornada extraordinaria) y los casos similares ya resueltos y las
   notas de aprendizaje: si algo ya se justificó antes, dilo.
6. Termina SIEMPRE llamando a `concluir_expediente` con el expediente completo. No acuses a personas: describe
   la diferencia no conciliada, las hipótesis y qué verificar. Separa con claridad HECHOS (evidencia con cifras de Odoo),
   HIPÓTESIS (ordenadas por plausibilidad, cada una con evidencia a favor y en contra y cómo verificarla) y DATOS FALTANTES
   (lo que no está en Odoo). No inventes probabilidades numéricas. La confianza refleja cuánta evidencia tienes, no la
   gravedad del caso. Si el histórico previo es insuficiente para comparar, dilo en vez de comparar.
7. Las ACLARACIONES que aparezcan (explicaciones aportadas por personas) son declaraciones con autor, fecha y alcance:
   cítalas como tales en el expediente («declarado por…»), nunca como hechos verificados, y di qué verificaría cada una.
Usa un máximo de 8 llamadas a herramientas. Sé concreto y cuantitativo. Nunca escribas "None" ni "nan": si falta un dato, di "sin dato".
""".strip()

HERRAMIENTAS_INVESTIGACION: list[dict] = [
    {"name": "ver_folio", "description": "Cabecera y todas las líneas de consumo de un folio.",
     "input_schema": {"type": "object", "properties": {"folio": {"type": "string"}}, "required": ["folio"]}},
    {"name": "historial_actor", "description": "Historial de un auxiliar o médico para un producto: cirugías, tasa mL/min "
                                               "mediana/p25/p75, comparación con pares del hospital, tendencia reciente, "
                                               "folios sin médico y consumos nocturnos.",
     "input_schema": {"type": "object", "properties": {"tipo": {"type": "string", "enum": ["auxiliar", "medico"]},
                                                       "nombre": {"type": "string"}, "producto": {"type": "string"},
                                                       "dias": {"type": "integer"}}, "required": ["tipo", "nombre"]}},
    {"name": "lecturas_bascula", "description": "Lecturas de báscula (peso inicial/final, gramos, mL implícitos por densidad) de "
                                                "un folio, y la tasa de discrepancias del auxiliar responsable.",
     "input_schema": {"type": "object", "properties": {"folio": {"type": "string"}, "producto": {"type": "string"}}, "required": ["folio"]}},
    {"name": "existencias_lote", "description": "Dónde hay existencia de un lote hoy (ubicación, cantidad, caducidad).",
     "input_schema": {"type": "object", "properties": {"lote": {"type": "string"}}, "required": ["lote"]}},
    {"name": "historial_lote", "description": "Consumo de un lote por unidad médica en los últimos días.",
     "input_schema": {"type": "object", "properties": {"lote": {"type": "string"}, "dias": {"type": "integer"}}, "required": ["lote"]}},
    {"name": "contexto_dia", "description": "Qué pasó ese día en la unidad médica: folios, productos, horas, folios sin médico.",
     "input_schema": {"type": "object", "properties": {"hospital": {"type": "string"}, "fecha": {"type": "string", "description": "YYYY-MM-DD"}},
                      "required": ["hospital", "fecha"]}},
    {"name": "comparar_periodos", "description": "Últimos 30 días vs. 90 previos para una entidad (hospital, subalmacen, auxiliar, medico) y producto.",
     "input_schema": {"type": "object", "properties": {"tipo": {"type": "string", "enum": ["hospital", "subalmacen", "auxiliar", "medico"]},
                                                       "nombre": {"type": "string"}, "producto": {"type": "string"}}, "required": ["tipo", "nombre"]}},
    {"name": "casos_similares", "description": "Casos previos (preferentemente resueltos) con el mismo producto/hospital/auxiliar/médico/lote y su resolución.",
     "input_schema": {"type": "object", "properties": {"producto": {"type": "string"}, "hospital": {"type": "string"},
                                                       "auxiliar": {"type": "string"}, "medico": {"type": "string"}, "lote": {"type": "string"}}, "required": []}},
    {"name": "notas_aprendizaje", "description": "Notas de aprendizaje y excepciones registradas por el cliente para una entidad.",
     "input_schema": {"type": "object", "properties": {"clave": {"type": "string"}}, "required": []}},
    {"name": "concluir_expediente", "description": "Registra el expediente final. Llámala exactamente una vez al terminar.",
     "input_schema": {"type": "object", "properties": {
         "que_paso": {"type": "string"}, "evidencia": {"type": "array", "items": {"type": "string"}},
         "hipotesis": {"type": "array", "description": "Ordenadas de más a menos plausible. NO son probabilidades: sólo un orden con evidencia.",
                       "items": {"type": "object", "properties": {
             "hipotesis": {"type": "string"}, "plausibilidad": {"type": "string", "enum": ["alta", "media", "baja"]},
             "a_favor": {"type": "array", "items": {"type": "string"}}, "en_contra": {"type": "array", "items": {"type": "string"}},
             "como_verificar": {"type": "string"}},
             "required": ["hipotesis", "plausibilidad", "a_favor", "en_contra"]}},
         "conclusion": {"type": "string"}, "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
         "impacto_mxn": {"type": "number"}, "riesgo_sanitario": {"type": "boolean"}, "riesgo_facturacion": {"type": "boolean"},
         "accion_recomendada": {"type": "string"}, "responsable": {"type": "string"}, "plazo": {"type": "string"},
         "preguntas_pendientes": {"type": "array", "items": {"type": "string"}},
         "datos_faltantes": {"type": "array", "items": {"type": "string"}, "description": "Datos que NO están en Odoo y harían falta para concluir"},
         "documentos_faltantes": {"type": "array", "items": {"type": "string"}}},
         "required": ["que_paso", "evidencia", "hipotesis", "conclusion", "confianza", "accion_recomendada", "responsable", "datos_faltantes"]}},
]

HIPOTESIS_POR_REGLA = {  # (hipótesis, cómo verificarla) — sin probabilidades: se ordenan por evidencia del caso
    "R01_EXCEDE_ENVASE": [("Error de captura (unidad o decimal)", "Comparar con la bitácora de quirófano y la lectura de báscula"),
                          ("Se registraron varios frascos en una sola línea", "Revisar lotes y pesajes del folio"),
                          ("Consumo real anómalo", "Confirmar con el anestesiólogo el procedimiento y la técnica")],
    "R02_BASCULA_IMPOSIBLE": [("Cambio de frasco o recarga sin registrar", "Revisar si hubo un frasco nuevo abierto en ese turno"),
                              ("Lectura de báscula mal asociada al folio", "Cotejar hora de la lectura con hora de la cirugía"),
                              ("Error de tara o calibración", "Revisar bitácora de calibración de la báscula")],
    "R03_BASCULA_DISCREPANCIA": [("Captura manual menor que el consumo real (merma no registrada)", "Conciliar existencia física del frasco/lote"),
                                 ("Desecho o fuga no documentados", "Preguntar por purgas/derrames en el turno"),
                                 ("Lectura asociada a otro folio", "Cotejar secuencia de pesajes del día")],
    "R04_SIN_CIRUGIA": [("Folio incompleto: falta capturar médico/paciente", "Pedir al auxiliar completar el folio con evidencia"),
                        ("Consumo sin respaldo de cirugía", "Verificar programación quirúrgica y bitácora del quirófano"),
                        ("Prueba de equipo o purga documentable", "Solicitar el registro de mantenimiento/purga")],
    "R06_TASA_CLINICA": [("Error de captura de cantidad o duración", "Comparar duración con hora de inicio/fin del folio"),
                         ("Técnica de alto flujo o cirugía más larga de lo registrado", "Confirmar técnica con el anestesiólogo"),
                         ("Consumo real fuera de norma", "Revisar historial del médico en procedimientos similares")],
    "R07_LOTE_VIAJERO": [("Traslado entre unidades sin registrar", "Buscar transferencia física del lote"),
                         ("Lote mal seleccionado al capturar", "Verificar lote físico en el frasco usado"),
                         ("Frasco movido sin control", "Revisar existencias del lote en ambas unidades")],
    "R08_DUPLICADO": [("Captura duplicada (riesgo de facturación doble al IMSS)", "Verificar si hay dos procedimientos distintos con el mismo paciente y hora"),
                      ("Dos procedimientos reales idénticos", "Confirmar con la programación quirúrgica")],
    "R09_LOTE_CADUCADO": [("Lote caducado en piso (riesgo sanitario)", "Inspección física inmediata y retiro"),
                          ("Lote mal seleccionado al capturar", "Cotejar lote físico del frasco")],
    "R10_CAMBIO_NIVEL": [("Cambio operativo real (más cirugías, nuevo médico, nueva técnica)", "Comparar número y tipo de cirugías del periodo"),
                         ("Cambio en la forma de capturar", "Revisar si cambió el auxiliar o el proceso de captura"),
                         ("Merma sostenida", "Conciliar existencias físicas del sub-almacén con lo registrado")],
    "R11_ACTOR_DESVIADO": [("Mezcla de casos más complejos o técnica distinta", "Comparar tipos de cirugía y duraciones con los pares"),
                           ("Práctica de captura distinta (redondeo)", "Revisar la distribución de cantidades capturadas"),
                           ("Diferencia no conciliada que puede ser merma", "Conciliación física de frascos del turno")],
    "R12_FRECUENCIA_ATIPICA": [("Jornada extraordinaria o cobertura de un compañero", "Confirmar con la jefatura del turno"),
                               ("Captura en lote de folios atrasados", "Revisar fechas de creación vs. fechas de cirugía")],
    "z": [("Cirugía más larga o compleja de lo habitual", "Cotejar duración y procedimiento en el folio"),
          ("Error de captura", "Comparar con báscula y bitácora"),
          ("Consumo real anómalo", "Revisar historial del médico/auxiliar")],
}


# ── contexto de investigación (datos de la corrida en memoria) ──────────────
class Contexto:
    def __init__(self, df: pd.DataFrame, densidades: dict[int, float] | None = None) -> None:
        self.df = df.copy()
        self.df["fecha"] = pd.to_datetime(self.df["fecha"], errors="coerce")
        self.df["dia"] = self.df["fecha"].dt.date.astype(str)
        self.densidades = densidades or {}
        self.trazas: list[dict] = []

    # herramientas -----------------------------------------------------------
    def ver_folio(self, folio: str) -> dict:
        g = self.df[self.df["folio"].astype(str) == str(folio)]
        if g.empty:
            return {"error": f"No hay líneas del folio {folio} en el periodo analizado."}
        cab = g.iloc[0]
        return {"folio": folio, "fecha": str(cab["fecha"]), "hospital": cab["hospital"], "medico": cab["medico"] or "(sin médico)",
                "auxiliar": cab["auxiliar"], "subalmacen": cab["subalmacen"], "duracion_min": _num(cab["duracion_min"]),
                "lineas": [{"producto": r["producto"], "cantidad": _num(r["cantidad"]), "unidad": r["unidad"], "lote": r["lote"],
                            "importe": _num(r["importe"]), "peso_inicial": _num(r["peso_inicial"]), "peso_final": _num(r["peso_final"])}
                           for _, r in g.iterrows()],
                "importe_total": round(float(g["importe"].sum()), 2)}

    def historial_actor(self, tipo: str, nombre: str, producto: str | None = None, dias: int = 90) -> dict:
        col = "auxiliar" if tipo == "auxiliar" else "medico"
        d = self.df[self.df[col].astype(str) == str(nombre)]
        if d.empty:
            return {"error": f"Sin registros de {tipo} {nombre}."}
        hoy = self.df["fecha"].max()
        d = d[d["fecha"] > hoy - pd.Timedelta(days=dias)]
        hosp = d["hospital"].mode().iloc[0] if len(d) else ""
        out: dict[str, Any] = {"tipo": tipo, "nombre": nombre, "hospital": hosp, "dias": dias,
                               "folios": int(d["folio"].nunique()), "importe": round(float(d["importe"].sum()), 2),
                               "folios_sin_medico": int(d[d["medico"].astype(str).str.strip() == ""]["folio"].nunique()),
                               "consumos_nocturnos_o_finde": int(((d["fecha"].dt.hour >= 22) | (d["fecha"].dt.hour < 6) | (d["fecha"].dt.dayofweek >= 5)).sum())}
        if producto:
            dp = d[d["producto"].astype(str).str.contains(producto, case=False, regex=False)]
            if not dp.empty:
                tasa = (dp["cantidad"] / dp["duracion_min"]).replace([np.inf, -np.inf], np.nan).dropna()
                pares = self.df[(self.df["hospital"] == hosp) & (self.df[col].astype(str) != str(nombre))
                                & self.df["producto"].astype(str).str.contains(producto, case=False, regex=False)
                                & (self.df["fecha"] > hoy - pd.Timedelta(days=dias))]
                tasa_p = (pares["cantidad"] / pares["duracion_min"]).replace([np.inf, -np.inf], np.nan).dropna()
                rec = dp[dp["fecha"] > hoy - pd.Timedelta(days=28)]
                prev = dp[(dp["fecha"] <= hoy - pd.Timedelta(days=28))]
                out["producto"] = {
                    "cirugias": int(len(dp)), "cantidad_mediana": _num(dp["cantidad"].median()),
                    "tasa_ml_min": {"mediana": _num(tasa.median()), "p25": _num(tasa.quantile(0.25)), "p75": _num(tasa.quantile(0.75))} if len(tasa) else None,
                    "pares_hospital": {"n_personas": int(pares[col].nunique()), "tasa_mediana": _num(tasa_p.median()) if len(tasa_p) else None,
                                       "cantidad_mediana": _num(pares["cantidad"].median()) if len(pares) else None},
                    "ultimas_4_semanas_vs_previas": {"cantidad_media_reciente": _num(rec["cantidad"].mean()) if len(rec) else None,
                                                     "cantidad_media_previa": _num(prev["cantidad"].mean()) if len(prev) else None},
                    "tipos_cirugia_top": dp["producto"].head(0).tolist(),
                }
        return out

    def lecturas_bascula(self, folio: str, producto: str | None = None) -> dict:
        g = self.df[self.df["folio"].astype(str) == str(folio)]
        if producto:
            g = g[g["producto"].astype(str).str.contains(producto, case=False, regex=False)]
        g = g[g["peso_inicial"].notna()]
        if g.empty:
            return {"folio": folio, "lecturas": [], "nota": "Sin lecturas de báscula para este folio/producto."}
        lect = []
        for _, r in g.iterrows():
            dens = self.densidades.get(int(r["producto_id"]) if pd.notna(r["producto_id"]) else -1, 1.5)
            g_bascula = float(r["peso_inicial"]) - float(r["peso_final"])
            ml = g_bascula / dens
            lect.append({"producto": r["producto"], "peso_inicial_g": _num(r["peso_inicial"]), "peso_final_g": _num(r["peso_final"]),
                         "gramos_bascula": round(g_bascula, 1), "densidad_g_ml": dens, "ml_implicitos": round(ml, 1),
                         "ml_capturados": _num(r["cantidad"]), "diferencia_pct": round(100 * (ml - float(r["cantidad"])) / float(r["cantidad"]), 1) if r["cantidad"] else None,
                         "lectura_inconsistente": bool(float(r["peso_final"]) > float(r["peso_inicial"]))})
        aux = g.iloc[0]["auxiliar"]
        da = self.df[(self.df["auxiliar"] == aux) & self.df["peso_inicial"].notna() & (self.df["cantidad"] > 0)]
        tasa_disc = None
        if len(da):
            dens_a = da["producto_id"].map(lambda p: self.densidades.get(int(p) if pd.notna(p) else -1, 1.5))
            ml_a = (da["peso_inicial"] - da["peso_final"]) / dens_a
            disc = ((ml_a - da["cantidad"]).abs() / da["cantidad"] > 0.15).mean()
            tasa_disc = round(100 * float(disc), 1)
        return {"folio": folio, "auxiliar": aux, "lecturas": lect, "pct_discrepancias_del_auxiliar": tasa_disc,
                "pesajes_del_auxiliar": int(len(da))}

    def existencias_lote(self, lote: str) -> dict:
        try:
            rows = get_client().search_read("stock.quant", [["lot_id.name", "=", lote]] if False else [["lot_id", "ilike", lote]],
                                            ["product_id", "location_id", "quantity", "reserved_quantity", "expiration_date"], limite=50)
        except OdooError as e:
            return {"error": str(e)}
        return {"lote": lote, "existencias": [{"producto": r["product_id"][1] if r.get("product_id") else "", "ubicacion": r["location_id"][1] if r.get("location_id") else "",
                                              "cantidad": r.get("quantity"), "reservado": r.get("reserved_quantity"), "caducidad": r.get("expiration_date")}
                                             for r in rows]}

    def historial_lote(self, lote: str, dias: int = 120) -> dict:
        d = self.df[self.df["lote"].astype(str) == str(lote)]
        hoy = self.df["fecha"].max()
        d = d[d["fecha"] > hoy - pd.Timedelta(days=dias)]
        if d.empty:
            return {"lote": lote, "consumo": []}
        g = d.groupby("hospital").agg(lineas=("cantidad", "size"), cantidad=("cantidad", "sum"), primera=("fecha", "min"), ultima=("fecha", "max")).reset_index()
        return {"lote": lote, "producto": d["producto"].iloc[0], "consumo": [{"hospital": r["hospital"], "lineas": int(r["lineas"]),
                 "cantidad": round(float(r["cantidad"]), 1), "desde": str(r["primera"].date()), "hasta": str(r["ultima"].date())} for _, r in g.iterrows()],
                "unidad_habitual": g.sort_values("lineas", ascending=False)["hospital"].iloc[0]}

    def contexto_dia(self, hospital: str, fecha: str) -> dict:
        d = self.df[(self.df["hospital"].astype(str) == str(hospital)) & (self.df["dia"] == str(fecha)[:10])]
        if d.empty:
            return {"hospital": hospital, "fecha": fecha, "folios": 0}
        tipico = self.df[self.df["hospital"].astype(str) == str(hospital)].groupby("dia")["folio"].nunique()
        return {"hospital": hospital, "fecha": fecha, "folios": int(d["folio"].nunique()), "folios_tipicos_por_dia": _num(tipico.median()),
                "folios_sin_medico": int(d[d["medico"].astype(str).str.strip() == ""]["folio"].nunique()),
                "horas": sorted(set(int(h) for h in d["fecha"].dt.hour.dropna())),
                "auxiliares": sorted(set(d["auxiliar"].astype(str)) - {""}), "medicos": sorted(set(d["medico"].astype(str)) - {""}),
                "productos_top": d.groupby("producto")["cantidad"].sum().sort_values(ascending=False).head(6).round(1).to_dict()}

    def comparar_periodos(self, tipo: str, nombre: str, producto: str | None = None) -> dict:
        col = tipo
        d = self.df[self.df[col].astype(str) == str(nombre)]
        if producto:
            d = d[d["producto"].astype(str).str.contains(producto, case=False, regex=False)]
        hoy = self.df["fecha"].max()
        rec = d[d["fecha"] > hoy - pd.Timedelta(days=30)]
        prev = d[(d["fecha"] <= hoy - pd.Timedelta(days=30)) & (d["fecha"] > hoy - pd.Timedelta(days=120))]
        def res(x):
            tasa = (x["cantidad"] / x["duracion_min"]).replace([np.inf, -np.inf], np.nan).dropna()
            return {"lineas": int(len(x)), "folios": int(x["folio"].nunique()), "cantidad_total": round(float(x["cantidad"].sum()), 1),
                    "cantidad_por_folio": round(float(x["cantidad"].sum() / max(x["folio"].nunique(), 1)), 2),
                    "tasa_mediana": _num(tasa.median()) if len(tasa) else None, "importe": round(float(x["importe"].sum()), 2)}
        return {"tipo": tipo, "nombre": nombre, "producto": producto, "ultimos_30": res(rec), "previos_90_por_30_dias": {k: (round(v / 3, 2) if isinstance(v, (int, float)) and k in ("lineas", "folios", "cantidad_total", "importe") else v) for k, v in res(prev).items()}}

    def casos_similares(self, **entidades) -> dict:
        cs = db.casos_similares(entidades, limite=5)
        return {"casos": [{"id": c["id"], "titulo": c["titulo"], "estado": c["estado"], "conclusion": c.get("conclusion"),
                           "resolucion": c.get("resolucion"), "fecha": c["creado_en"][:10]} for c in cs]}

    def notas_aprendizaje(self, clave: str | None = None) -> dict:
        notas = db.aprendizaje(limite=100)
        if clave:
            notas = [n for n in notas if clave.lower() in (n["clave"] or "").lower() or clave.lower() in n["nota"].lower()]
        return {"notas": [{"ambito": n["ambito"], "clave": n["clave"], "nota": n["nota"]} for n in notas[:15]]}

    def despachar(self, nombre: str, args: dict, salida: dict) -> Any:
        if nombre == "concluir_expediente":
            salida["expediente"] = args
            return {"ok": True, "mensaje": "Expediente registrado. Responde únicamente: listo."}
        fn = getattr(self, nombre, None)
        if not fn:
            raise ValueError(f"Herramienta desconocida: {nombre}")
        res = fn(**args)
        self.trazas.append({"herramienta": nombre, "argumentos": args, "resumen": _resumen(res)})
        return res


def _num(v):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return v


def _resumen(res: Any) -> str:
    s = json.dumps(res, ensure_ascii=False, default=str)
    return s[:160]


# ── investigación de un objetivo (hallazgo o patrón) ────────────────────────
def investigar(objetivo: dict, ctx: Contexto, con_llm: bool = True, usuario: str = "") -> dict:
    """Devuelve {"expediente": {...}, "trazas": [...], "modo": "claude:<modelo>"|"determinista"}."""
    ctx.trazas = []
    cfg1 = db.get_ajuste("agente1_config", {}) or {}
    modelo = cfg1.get("modelo_investigacion") or settings.ANTHROPIC_MODEL_FAST
    max_llamadas = max(3, int(cfg1.get("max_llamadas_investigador") or 10))   # cada llamada es un viaje a Claude
    if con_llm and claude.disponible():
        salida: dict = {}
        aclar = db.aclaraciones_para({k: objetivo.get(k) for k in ("producto", "hospital", "auxiliar", "medico", "lote") if objetivo.get(k)})
        prompt = (f"Notas de aprendizaje vigentes:\n{contexto_aprendizaje(['agente', 'producto', 'hospital', 'medico', 'auxiliar', 'unidad'], 30)}\n\n"
                  + (f"ACLARACIONES DECLARADAS POR PERSONAS (no verificadas):\n{compacto([{k: a[k] for k in ('usuario', 'creado_en', 'alcance', 'texto', 'verificada')} for a in aclar], 3000)}\n\n" if aclar else "")
                  + f"HALLAZGO A INVESTIGAR (JSON):\n{compacto(objetivo, 6000)}\n\nInvestiga y concluye.")
        try:
            sistema = SISTEMA_INVESTIGADOR + (f"\n\nLÍMITE DE ESTA INVESTIGACIÓN: como máximo {max_llamadas} llamadas a herramientas; "
                                              "prioriza abrir el folio, el historial del actor y la báscula, y llama concluir_expediente antes de agotarlas.")
            r = claude.bucle_herramientas(sistema, [{"role": "user", "content": prompt}], HERRAMIENTAS_INVESTIGACION,
                                          lambda n, a: ctx.despachar(n, a, salida), origen="investigador", usuario=usuario,
                                          max_iteraciones=max_llamadas + 1, modelo=modelo)
            if salida.get("expediente"):
                exp = _normalizar(salida["expediente"], objetivo)
                return {"expediente": exp, "trazas": ctx.trazas, "modo": f"claude:{modelo}", "texto": r["texto"]}
            db.log("warn", "investigador", "Claude no llamó a concluir_expediente; uso expediente determinista", objetivo.get("titulo", ""))
        except claude.LLMError as e:
            db.log("error", "investigador", "Falla de Claude en investigación", str(e))
    exp = expediente_determinista(objetivo, ctx)
    return {"expediente": exp, "trazas": ctx.trazas, "modo": "determinista", "texto": ""}


def _normalizar(e: dict, objetivo: dict) -> dict:
    e = dict(e)
    orden = {"alta": 0, "media": 1, "baja": 2}
    hip = []
    for h in (e.get("hipotesis") or []):
        h = dict(h)
        h.pop("probabilidad", None)                      # nunca mostramos porcentajes: no son probabilidades válidas
        h["plausibilidad"] = h.get("plausibilidad") if h.get("plausibilidad") in orden else "media"
        h.setdefault("a_favor", []); h.setdefault("en_contra", []); h.setdefault("como_verificar", "")
        hip.append(h)
    e["hipotesis"] = sorted(hip, key=lambda h: orden[h["plausibilidad"]])
    e.setdefault("impacto_mxn", float(objetivo.get("importe_riesgo") or 0))
    e.setdefault("evidencia", [])
    e.setdefault("preguntas_pendientes", [])
    e.setdefault("datos_faltantes", [])
    e.setdefault("documentos_faltantes", [])
    e.setdefault("severidad", objetivo.get("severidad", "media"))
    e["aclaraciones"] = [{"id": a["id"], "usuario": a["usuario"], "fecha": a["creado_en"][:16], "alcance": a["alcance"], "texto": a["texto"],
                          "verificada": bool(a.get("verificada"))}
                         for a in db.aclaraciones_para({k: objetivo.get(k) for k in ("producto", "hospital", "auxiliar", "medico", "lote") if objetivo.get(k)})]
    # limpieza de valores técnicos que el modelo pudiera colar
    for k in ("evidencia", "preguntas_pendientes", "datos_faltantes", "documentos_faltantes"):
        e[k] = [str(x).replace(" None", " sin dato").replace("nan ", "sin dato ") for x in (e.get(k) or []) if x]
    return e


def _v(x, unidad: str = "", dec: int = 1) -> str:
    """Valor legible: nunca 'None' ni 'nan' en un expediente."""
    try:
        if x is None or (isinstance(x, float) and x != x):
            return "sin dato"
        if isinstance(x, (int, float)):
            txt = f"{x:,.{dec}f}".rstrip("0").rstrip(".") if isinstance(x, float) else f"{x:,}"
            return f"{txt} {unidad}".strip()
        return str(x)
    except Exception:  # noqa: BLE001
        return "sin dato"


def expediente_determinista(o: dict, ctx: Contexto) -> dict:
    """Misma evidencia, sin razonamiento libre: hipótesis típicas de la regla; la confianza depende de la cantidad
    de evidencia cuantitativa (nunca de la severidad); la evidencia no repite el 'qué pasó'; una comparación sin
    histórico suficiente se dice tal cual; las aclaraciones de personas se listan aparte como declaraciones."""
    evidencia: list[str] = []
    reglas = o.get("metodos") or ([o.get("regla")] if o.get("regla") else [])
    _s: dict = {}
    usar = lambda _h, **args: ctx.despachar(_h, args, _s)   # deja traza de cada consulta  # noqa: E731
    folio = o.get("folio")
    if folio:
        f = usar("ver_folio", folio=folio)
        if "error" not in f:
            evidencia.append(f"Folio {folio} del {str(f.get('fecha') or '')[:16]} en {_v(f.get('hospital'))}: {len(f.get('lineas') or [])} líneas, "
                             f"médico {_v(f.get('medico')) if f.get('medico') else 'no registrado'}, auxiliar {_v(f.get('auxiliar')) if f.get('auxiliar') else 'no registrado'}, "
                             f"duración {_v(f.get('duracion_min'), 'min', 0)}, importe ${float(f.get('importe_total') or 0):,.2f}.")
            b = usar("lecturas_bascula", folio=folio, producto=o.get("producto"))
            for l in b.get("lecturas", [])[:2]:
                evidencia.append(f"Báscula: {l['gramos_bascula']} g ≈ {l['ml_implicitos']} mL (densidad {l['densidad_g_ml']}) vs. {l['ml_capturados']} capturados"
                                 + (f" ({l['diferencia_pct']:+.0f} %)" if l.get("diferencia_pct") is not None else "") + ".")
            if b.get("pct_discrepancias_del_auxiliar") is not None:
                evidencia.append(f"El auxiliar {b['auxiliar']} presenta discrepancias de báscula en {b['pct_discrepancias_del_auxiliar']} % de sus {b['pesajes_del_auxiliar']} pesajes.")
    actor_tipo = "auxiliar" if o.get("auxiliar") else ("medico" if o.get("medico") else None)
    if actor_tipo or o.get("dimension") in ("auxiliar", "medico"):
        tipo = o.get("dimension") if o.get("dimension") in ("auxiliar", "medico") else actor_tipo
        nombre = o.get("actor") or o.get(tipo)
        if nombre:
            h = usar("historial_actor", tipo=tipo, nombre=nombre, producto=o.get("producto"))
            if "error" not in h:
                evidencia.append(f"{tipo.capitalize()} {nombre}: {_v(h.get('folios'), '', 0)} folios en {_v(h.get('dias'), '', 0)} días, "
                                 f"{_v(h.get('folios_sin_medico'), '', 0)} sin médico, {_v(h.get('consumos_nocturnos_o_finde'), '', 0)} consumos nocturnos/fin de semana.")
                p = h.get("producto")
                if p and p.get("tasa_ml_min") and (p.get("pares_hospital") or {}).get("tasa_mediana"):
                    evidencia.append(f"Tasa mediana {_v(p['tasa_ml_min'].get('mediana'), 'mL/min', 3)} vs. {_v(p['pares_hospital']['tasa_mediana'], 'mL/min', 3)} de "
                                     f"{_v(p['pares_hospital'].get('n_personas'), '', 0)} pares en {_v(h.get('hospital'))}.")
    historico_insuficiente = False
    if o.get("tipo") == "patron" and o.get("dimension") and o.get("clave"):
        cp = usar("comparar_periodos", tipo=o["dimension"], nombre=o["clave"], producto=o.get("producto"))
        u, pv = cp["ultimos_30"], cp["previos_90_por_30_dias"]
        if (pv.get("folios") or 0) * 3 < 5:
            historico_insuficiente = True
            evidencia.append(f"{o['dimension'].capitalize()} {o['clave']} · {o.get('producto')}: últimos 30 días {_v(u['folios'], '', 0)} folios, "
                             f"{_v(u['cantidad_por_folio'], '', 2)} por folio, tasa mediana {_v(u['tasa_mediana'], 'mL/min', 3)}. "
                             f"Sin histórico previo suficiente para comparar ({_v((pv.get('folios') or 0) * 3, '', 0)} folios en los 90 días anteriores).")
        else:
            evidencia.append(f"{o['dimension'].capitalize()} {o['clave']} · {o.get('producto')}: últimos 30 días {_v(u['folios'], '', 0)} folios, "
                             f"{_v(u['cantidad_por_folio'], '', 2)} por folio, tasa mediana {_v(u['tasa_mediana'], 'mL/min', 3)} vs. previos (por 30 días) "
                             f"{_v(pv['folios'], '', 1)} folios, {_v(pv['cantidad_por_folio'], '', 2)} por folio, tasa {_v(pv['tasa_mediana'], 'mL/min', 3)}.")
        if o["dimension"] in ("auxiliar", "medico"):
            h = usar("historial_actor", tipo=o["dimension"], nombre=o["clave"], producto=o.get("producto"))
            p = h.get("producto") if isinstance(h, dict) else None
            if p and p.get("tasa_ml_min"):
                pares = p.get("pares_hospital") or {}
                vs = (f" vs. {_v(pares.get('tasa_mediana'), 'mL/min', 3)} de {_v(pares.get('n_personas'), '', 0)} pares" if pares.get("tasa_mediana") is not None
                      else " (sin pares comparables en el periodo)")
                evidencia.append(f"{o['clave']}: tasa mediana {_v(p['tasa_ml_min'].get('mediana'), 'mL/min', 3)} (p25 {_v(p['tasa_ml_min'].get('p25'), '', 3)}, "
                                 f"p75 {_v(p['tasa_ml_min'].get('p75'), '', 3)}){vs}; {_v(h.get('folios_sin_medico'), '', 0)} folios sin médico; "
                                 f"{_v(h.get('consumos_nocturnos_o_finde'), '', 0)} consumos nocturnos/fin de semana en {_v(h.get('dias'), '', 0)} días.")
    if o.get("lote"):
        hl = usar("historial_lote", lote=o["lote"])
        if hl.get("consumo"):
            evidencia.append(f"Lote {o['lote']}: consumido en " + ", ".join(f"{c['hospital']} ({c['lineas']} líneas)" for c in hl["consumo"]) + f"; unidad habitual {hl['unidad_habitual']}.")
    if o.get("hospital") and o.get("fecha"):
        c = usar("contexto_dia", hospital=o["hospital"], fecha=str(o["fecha"])[:10])
        if c.get("folios"):
            evidencia.append(f"Ese día {o['hospital']} registró {c['folios']} folios (típico {c['folios_tipicos_por_dia']}), {c['folios_sin_medico']} sin médico.")
    sim = usar("casos_similares", **{k: o.get(k) for k in ("producto", "hospital", "auxiliar", "medico", "lote") if o.get(k)}).get("casos", [])
    for cs in sim:
        if cs.get("resolucion"):
            evidencia.append(f"Caso similar #{cs['id']} ({cs['estado']}): {cs['resolucion'][:140]}")
    # hipótesis por verificar (sin porcentajes): a favor / en contra se derivan de la evidencia numérica disponible
    hip: list[dict] = []
    vistos = set()
    señales = {"bascula_ok": any("Báscula:" in x and ("-" in x.split("(")[-1] or "+0" in x) for x in evidencia),
               "bascula_mal": any("Báscula:" in x and "%" in x and abs(_pct_en(x)) > 15 for x in evidencia),
               "sin_medico": any("sin médico" in x and not x.strip().startswith("0 sin") for x in evidencia),
               "dia_atipico": any("registró" in x and "típico" in x for x in evidencia),
               "lote_ajeno": any("unidad habitual" in x for x in evidencia)}
    for r in reglas:
        clave = r if r in HIPOTESIS_POR_REGLA else ("z" if str(r).startswith("z_") else None)
        for h, como in HIPOTESIS_POR_REGLA.get(clave, []):
            if h in vistos:
                continue
            vistos.add(h)
            a, c = [], []
            if "captura" in h.lower() and señales["bascula_mal"]:
                a.append("La báscula no coincide con lo capturado")
            if "captura" in h.lower() and señales["bascula_ok"]:
                c.append("La báscula coincide con lo capturado")
            if "merma" in h.lower() and señales["bascula_mal"]:
                a.append("Salió del frasco más de lo capturado")
            if "merma" in h.lower() and señales["bascula_ok"]:
                c.append("La báscula respalda la cantidad capturada")
            if "cirugía" in h.lower() and señales["dia_atipico"]:
                a.append("El día tuvo carga atípica en la unidad")
            if "Folio incompleto" in h and señales["sin_medico"]:
                a.append("El auxiliar tiene otros folios sin médico")
            if "Traslado" in h and señales["lote_ajeno"]:
                a.append("El lote se consume en otra unidad habitualmente")
            hip.append({"hipotesis": h, "plausibilidad": "media", "a_favor": a, "en_contra": c, "como_verificar": como})
    # orden: más evidencia a favor primero
    hip.sort(key=lambda h_: (-(len(h_["a_favor"]) - len(h_["en_contra"]))))
    for i, h_ in enumerate(hip):
        h_["plausibilidad"] = "alta" if (h_["a_favor"] and not h_["en_contra"]) else ("baja" if h_["en_contra"] and not h_["a_favor"] else "media")
    datos_faltantes = ["Bitácora física de quirófano del folio", "Confirmación del auxiliar/médico sobre el procedimiento"]
    if not any("Báscula:" in x for x in evidencia):
        datos_faltantes.append("Lecturas de báscula del folio (no registradas)")
    if o.get("lote") and not señales["lote_ajeno"]:
        datos_faltantes.append("Existencia física actual del lote en la unidad")
    # sin repetir el "qué pasó" ni frases idénticas
    que_paso = (o.get("motivo") or " | ".join(o.get("motivos") or []))[:600]
    vistas, dedup = set(), []
    for x in evidencia:
        k = x.strip().lower()
        if k in vistas or (que_paso and k == que_paso.strip().lower()):
            continue
        vistas.add(k); dedup.append(x)
    evidencia = dedup
    # aclaraciones aportadas por personas: declaradas, con alcance y trazabilidad; NO son hechos verificados
    entidades_o = {k: o.get(k) for k in ("producto", "hospital", "auxiliar", "medico", "lote") if o.get(k)}
    aclar = [{"id": a["id"], "usuario": a["usuario"], "fecha": a["creado_en"][:16], "alcance": a["alcance"], "texto": a["texto"],
              "verificada": bool(a.get("verificada"))} for a in db.aclaraciones_para(entidades_o)]
    n_cuant = sum(1 for x in evidencia if any(ch.isdigit() for ch in x) and "sin dato" not in x)
    confianza = "media" if (n_cuant >= 4 and not historico_insuficiente) else "baja"     # depende de la evidencia, no de la severidad
    sev = o.get("severidad", "media")
    responsable = "Contabilidad / Facturación" if any(r in ("R08_DUPLICADO",) for r in reglas) else \
        ("Calidad / Responsable sanitario" if "R09_LOTE_CADUCADO" in reglas else "Dirección de Operaciones (jefatura de la unidad)")
    return {
        "que_paso": que_paso,
        "evidencia": evidencia or ["Sin evidencia adicional disponible en el periodo analizado."],
        "hipotesis": hip or [{"hipotesis": "Diferencia no conciliada", "plausibilidad": "media", "a_favor": [], "en_contra": [],
                              "como_verificar": "Conciliar contra báscula y existencias físicas"}],
        "aclaraciones": aclar,
        "historico_insuficiente": historico_insuficiente,
        "conclusion": ("Diferencia no conciliada que requiere verificación en sitio; el expediente se armó sin razonamiento del "
                       "modelo de lenguaje (modo determinista)." + (" Hay aclaraciones declaradas por personas que aún no se verifican." if aclar else "")),
        "confianza": confianza,
        "severidad": sev,
        "impacto_mxn": float(o.get("importe_riesgo") or 0), "riesgo_sanitario": "R09_LOTE_CADUCADO" in reglas,
        "riesgo_facturacion": any(r in ("R08_DUPLICADO", "R04_SIN_CIRUGIA", "R01_EXCEDE_ENVASE") for r in reglas),
        "accion_recomendada": "Verificar el folio con el auxiliar y el médico; conciliar contra báscula y existencias físicas; "
                              "documentar la justificación o registrar el ajuste.",
        "responsable": responsable, "plazo": "5 días hábiles" if sev == "critica" else "10 días hábiles",
        "preguntas_pendientes": ["¿Existe evidencia física (frasco, bitácora de quirófano) que respalde la cantidad?"],
        "datos_faltantes": datos_faltantes, "documentos_faltantes": [],
    }


def _pct_en(texto: str) -> float:
    import re as _re
    m = _re.search(r"\(([+-]?\d+)\s*%", texto)
    return float(m.group(1)) if m else 0.0


# ── acciones en Odoo derivadas de un caso (siempre a la cola de aprobación) ──
def proponer_acciones_caso(caso_id: int, o: dict, e: dict, corrida_id: int, usuario: str = "",
                           respaldo: bool | None = None) -> list[dict]:
    """Qué conviene ejecutar en Odoo según la regla del caso: cuarentena/desecho de lotes caducados, ticket de Helpdesk
    para conciliación o facturación, conteo físico cuando hay diferencia sostenida en un sub-almacén.
    ``respaldo``: la corrida se hizo sobre movimientos de inventario (sin folio médico); si no se indica, se consulta el mapeo."""
    from . import autonomia
    from ..odoo import acciones as OA, schema as _schema
    if autonomia.nivel() == 0:
        return []
    if respaldo is None:
        respaldo = _schema.en_respaldo()
    reglas = set(o.get("metodos") or ([o.get("regla")] if o.get("regla") else []))
    out: list[dict] = []
    titulo_base = f"Caso #{caso_id} · {o.get('titulo', '')}"[:110]
    desc = f"{e.get('que_paso', '')}\n\nAcción recomendada: {e.get('accion_recomendada', '')}\nResponsable: {e.get('responsable', '')}"
    usar_helpdesk = bool(autonomia.politicas().get("usar_helpdesk"))
    def _proponer(tipo, titulo, payload, motivo, impacto=None):
        r = autonomia.proponer("consumo", tipo, titulo, payload, motivo=motivo, impacto=impacto or {}, corrida_id=corrida_id, usuario=usuario)
        if r.get("id"):
            db.actualizar_accion(r["id"], caso_id=caso_id)
            out.append(r)
        return r
    def _avisar(equipo: str, motivo: str, prioridad: str = "2", plazo_dias: int = 5):
        """Seguimiento humano del caso: aviso a las personas del equipo en Odoo (actividad + notificación). El cliente no
        usa Helpdesk; si la política «usar_helpdesk» está activa se crea un ticket en su lugar."""
        if usar_helpdesk:
            return _proponer("ticket_helpdesk", f"Ticket {OA.EQUIPOS_AVISO.get(equipo, {}).get('nombre', equipo)} · {titulo_base}",
                             {"titulo_ticket": titulo_base, "descripcion": desc, "equipo": OA.EQUIPOS_AVISO.get(equipo, {}).get("nombre", equipo),
                              "prioridad": prioridad}, motivo)
        try:
            dest = OA.destinatarios_equipo(equipo)
        except Exception as ex:  # noqa: BLE001
            dest = {"equipo": equipo, "nombre": OA.EQUIPOS_AVISO.get(equipo, {}).get("nombre", equipo), "personas": [], "avisos": [str(ex)[:200]]}
        nombres = [p["nombre"] for p in dest["personas"]]
        payload = {"equipo": dest["equipo"], "equipo_nombre": dest["nombre"], "asunto": titulo_base, "cuerpo": desc, "plazo_dias": plazo_dias,
                   "caso_id": caso_id, "producto_id": int(o["producto_id"]) if o.get("producto_id") else None, "producto": o.get("producto"),
                   "n_destinatarios": len(nombres), "destinatarios": nombres,
                   "destinatarios_texto": ", ".join(nombres[:4]) + (f" y {len(nombres) - 4} más" if len(nombres) > 4 else "") if nombres else "nadie todavía"}
        # el aviso cuelga del folio (registro real de Odoo) cuando se conoce; en respaldo, de la entrega de almacén
        folio_id = o.get("folio_id")
        folio_modelo = o.get("folio_modelo") or ("stock.picking" if respaldo else _schema.modelo("folio"))
        try:
            if folio_modelo and folio_id not in (None, "", False) and not (isinstance(folio_id, float) and folio_id != folio_id):
                payload.update({"modelo": folio_modelo, "res_id": int(float(folio_id))})
        except (TypeError, ValueError):
            pass
        aviso_extra = (" ⚠ " + " ".join(dest.get("avisos") or [])) if not nombres else ""
        return _proponer("aviso_equipo", f"Avisar a {dest['nombre']} · {titulo_base}"[:140], payload, motivo + aviso_extra)
    # lote caducado → cuarentena del lote en su ubicación + ticket a Calidad
    if "R09_LOTE_CADUCADO" in reglas and o.get("lote") and o.get("producto_id"):
        try:
            u = OA.ubicacion_por_nombre(str(o.get("subalmacen") or o.get("almacen") or ""))
            if u:
                _proponer("cuarentena_lote", f"Cuarentena del lote {o['lote']} ({o.get('producto')}) en {u['complete_name']}",
                          {"producto_id": int(o["producto_id"]), "lote": o["lote"], "cantidad": float(o.get("cantidad") or 1), "origen_id": u["id"],
                           "origen": u["complete_name"]},
                          "Lote caducado consumido: bloquear el remanente hasta inspección física.", {"cantidad": float(o.get("cantidad") or 1)})
        except Exception as ex:  # noqa: BLE001
            db.log("warn", "investigador", "No se pudo proponer cuarentena", str(ex))
        _avisar("calidad", "Riesgo sanitario: lote caducado en piso; inspección física del remanente.", "3", plazo_dias=2)
    # duplicado o sin cirugía → aviso a Contabilidad/Facturación (retener el folio de la facturación al IMSS hasta conciliar).
    # Sólo tiene sentido con folios médicos reales: en modo de respaldo (movimientos de inventario) no hay folio que
    # facturar al IMSS y el aviso sería ruido.
    if reglas & {"R08_DUPLICADO", "R04_SIN_CIRUGIA", "R01_EXCEDE_ENVASE"} and not respaldo:
        _avisar("facturacion", f"Retener el folio {o.get('folio') or ''} de la facturación al IMSS hasta conciliar.".replace("  ", " "), "2", plazo_dias=5)
    # patrón sostenido en sub-almacén / actor → conteo físico del producto en esa ubicación + ticket Operaciones
    if reglas & {"R10_CAMBIO_NIVEL", "R11_ACTOR_DESVIADO", "R03_BASCULA_DISCREPANCIA"} and o.get("producto_id"):
        try:
            loc = str(o.get("subalmacen") or (o.get("clave") if o.get("dimension") == "subalmacen" else "") or "")
            u = OA.ubicacion_por_nombre(loc) if loc else None
            if u:
                _proponer("solicitar_conteo", f"Conteo físico de {o.get('producto')} en {u['complete_name']}",
                          {"producto_id": int(o["producto_id"]), "ubicacion_id": u["id"], "ubicacion": u["complete_name"]},
                          "Diferencia no conciliada sostenida: conciliar existencia física antes de concluir.")
        except Exception as ex:  # noqa: BLE001
            db.log("warn", "investigador", "No se pudo proponer conteo", str(ex))
        _avisar("operaciones", "Verificación en sitio con la jefatura de la unidad.", "2", plazo_dias=5)
    return out


# ── orquestación por corrida ────────────────────────────────────────────────
def investigar_corrida(res: dict, df: pd.DataFrame, corrida_id: int, usuario: str = "", con_llm: bool = True,
                       max_casos: int | None = None, progreso: Callable | None = None, densidades: dict | None = None) -> list[dict]:
    """Elige los objetivos de mayor valor (patrones críticos/altos + hallazgos críticos por importe), investiga cada uno
    y guarda los expedientes como casos. Un hallazgo ya investigado (misma huella) no se vuelve a investigar."""
    progreso = progreso or (lambda *a: None)
    cfg1 = db.get_ajuste("agente1_config", {}) or {}
    max_casos = int(max_casos or cfg1.get("max_casos_investigar") or 8)   # expedientes con Claude por corrida (Configuración)
    ctx = Contexto(df, densidades)
    objetivos: list[dict] = []
    ag, h = res.get("agregados"), res.get("hallazgos")
    if ag is not None and not ag.empty:
        for _, p in ag[ag["severidad"].isin(["critica", "alta"])].head(6).iterrows():
            o = {k: (v.item() if hasattr(v, "item") else v) for k, v in p.to_dict().items()}
            o["tipo"] = "patron"
            o["titulo"] = f"{o.get('producto') or ''} · {o.get('clave')} · {o.get('regla')}".strip(" ·")
            o["huella"] = f"patron|{o.get('regla')}|{o.get('producto_id')}|{o.get('clave')}"
            objetivos.append(o)
    if h is not None and not h.empty:
        crit = h[h["severidad"].isin(["critica", "alta"])].sort_values("importe_riesgo", ascending=False)
        for _, x in crit.head(max(12, max_casos * 4)).iterrows():      # cupo amplio: los ya investigados se descartan después
            o = {k: (v.item() if hasattr(v, "item") else v) for k, v in x.to_dict().items()}
            o["tipo"] = "anomalia"
            o["fecha"] = str(o.get("fecha"))
            o["titulo"] = f"{o.get('producto')} · {o.get('folio')} · {o.get('hospital')}"
            objetivos.append(o)
    # un hallazgo ya investigado (misma huella) no se repite: el cupo de la corrida se usa en casos NUEVOS
    objetivos = [o for o in objetivos if not (o.get("huella") and db.caso_por_huella(o["huella"]))][:max_casos]
    casos_out: list[dict] = []
    # Las investigaciones son independientes (cada una es su propio bucle de herramientas con Claude): se corren en
    # paralelo para que el tiempo de la corrida no crezca con el número de expedientes. La persistencia va en serie.
    import copy
    from concurrent.futures import ThreadPoolExecutor
    hilos = max(1, min(int(cfg1.get("investigaciones_paralelas") or 4), len(objetivos) or 1))
    progreso(f"Investigando {len(objetivos)} casos con el Investigador", f"{hilos} en paralelo · folio, historial, báscula, lote, casos similares")

    def _uno(o):
        return investigar(o, copy.copy(ctx), con_llm=con_llm, usuario=usuario)

    if hilos > 1 and con_llm and claude.disponible():
        with ThreadPoolExecutor(max_workers=hilos) as ex:
            resultados = list(ex.map(_uno, objetivos))
    else:
        resultados = []
        for i, o in enumerate(objetivos, 1):
            progreso(f"Investigando caso {i}/{len(objetivos)}: {o['titulo'][:70]}", "abriendo folio, historial, báscula, lote, casos similares")
            resultados.append(_uno(o))
    for o, r in zip(objetivos, resultados):
        e = r["expediente"]
        entidades = {k: o.get(k) for k in ("producto", "hospital", "auxiliar", "medico", "lote", "folio", "subalmacen") if o.get(k)}
        if o["tipo"] == "patron":
            entidades[o.get("dimension", "clave")] = o.get("clave")
        cid = db.guardar_caso({
            "corrida_id": corrida_id, "agente": "consumo", "tipo": o["tipo"], "titulo": o["titulo"], "severidad": o.get("severidad"),
            "entidades": entidades, "referencias": {"linea_id": o.get("linea_id"), "huella": o.get("huella"), "regla": o.get("regla"),
                                                    "metodos": o.get("metodos")},
            "expediente": e, "conclusion": e.get("conclusion"), "confianza": e.get("confianza"),
            "impacto_mxn": float(e.get("impacto_mxn") or 0), "accion_recomendada": e.get("accion_recomendada"),
            "responsable": e.get("responsable"), "investigado_con": r["modo"], "herramientas": r["trazas"],
            "huella": o.get("huella"),
        })
        acciones_caso = (proponer_acciones_caso(cid, o, e, corrida_id, usuario, respaldo=bool(res.get("modo_respaldo")))
                         if o.get("severidad") in ("critica", "alta") else [])
        casos_out.append({"id": cid, "titulo": o["titulo"], "severidad": o.get("severidad"), "confianza": e.get("confianza"),
                          "acciones": [a.get("id") for a in acciones_caso],
                          "conclusion": e.get("conclusion"), "impacto_mxn": e.get("impacto_mxn"), "modo": r["modo"],
                          "hipotesis_principal": (e.get("hipotesis") or [{}])[0].get("hipotesis"),
                          "plausibilidad_principal": (e.get("hipotesis") or [{}])[0].get("plausibilidad"),
                          "accion_recomendada": e.get("accion_recomendada"), "responsable": e.get("responsable"),
                          "herramientas": len(r["trazas"])})
    db.log("info", "investigador", f"{len(casos_out)} expedientes creados en la corrida #{corrida_id}",
           f"modo={'claude' if (con_llm and claude.disponible()) else 'determinista'}", usuario)
    return casos_out
