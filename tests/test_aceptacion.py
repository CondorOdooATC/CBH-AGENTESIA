"""Los seis casos de aceptación de la revisión v1.1 → v1.2."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app import db
from app.agents import autonomia, investigador, planificador
from app.ml import anomalias as A, pronostico as P
from app.odoo import acciones as OA, queries
from app.odoo.client import OdooError


# 1 · Una recepción tardía no oculta un faltante anterior
def test_1_recepcion_tardia_no_oculta_faltante():
    f = np.full(30, 10.0)
    manana = P.proyectar_saldo(10, f, [(1, 300)], None, 20)
    tarde = P.proyectar_saldo(10, f, [(25, 300)], None, 20)
    assert manana["dia_quiebre"] is None and manana["cobertura_dias"] == 30
    # cobertura hasta agotar: 10 piezas cubren exactamente 1 día de 10/día; el quiebre (saldo negativo al cierre) es el día 2
    assert tarde["dia_quiebre"] == 2 and tarde["cobertura_dias"] == 1.0 and tarde["faltante_para_seguridad"] >= 250
    assert tarde["dia_necesario"] == 1   # la entrada debe estar el día anterior al quiebre
    assert P.criticidad_local(tarde, tarde["faltante_para_seguridad"], 10, 7, 7, 10, 120) == "critico"
    assert P.criticidad_local(manana, manana["faltante_para_seguridad"], 10, 7, 7, 10, 120) in ("ok", "reordenar")


def test_4c_eventos_fuera_de_ventana_no_se_adelantan():
    """300 piezas, 10 diarias durante 30 días, recepción de 100 el día 35: el saldo final es 0, no 100."""
    f = np.full(30, 10.0)
    p = P.proyectar_saldo(300, f, [(35, 100)], None, 0)
    assert p["saldo_final"] == 0 and p["dia_quiebre"] is None and p["eventos_fuera_de_ventana"] == 1
    # una salida fuera de la ventana tampoco se descuenta; una entrada en el día 30 sí se cuenta
    assert P.proyectar_saldo(300, f, None, [(31, 100)], 0)["saldo_final"] == 0
    assert P.proyectar_saldo(300, f, [(30, 100)], None, 0)["saldo_final"] == 100
    # cobertura fraccional: 25 piezas a 10/día = 2.5 días
    assert P.proyectar_saldo(25, f, None, None, 0)["cobertura_dias"] == 2.5


# 2 · Una compra y su recepción se cuentan una sola vez; 3 · frascos, mL y piezas se concilian
def test_2_3_compra_una_vez_y_unidades(sim):
    pen = queries.abastecimiento_pendiente()
    sevo = pen[pen["producto_id"] == 1001]
    assert len(sevo) == 1 and sevo.iloc[0]["tipo"] == "compra"          # la recepción CEDIS/IN/00210 no se suma aparte
    assert sevo.iloc[0]["cantidad_doc"] == 12 and sevo.iloc[0]["unidad_doc"] == "Frasco 250 mL" and sevo.iloc[0]["cantidad"] == 3000.0
    jer = pen[pen["producto_id"] == 1014].iloc[0]
    assert jer["cantidad_doc"] == 15 and jer["cantidad"] == 1500.0 and jer["retrasada"]
    uc = queries.unidades_compra()
    assert uc[1001]["ratio"] == 250.0 and uc[1014]["ratio"] == 100.0
    # la compra sugerida se expresa también en unidad de compra
    df = queries.consumo(dias=200); ex = queries.existencias()
    res = P.pronosticar(df, ex, P.ConfigPronostico(lead_times=queries.lead_times()), pendientes=pen,
                        folios_prog=queries.folios_programados(30), uom_compra=uc)
    red = res["resurtido"][(res["resurtido"]["nivel"] == "red") & (res["resurtido"]["producto_id"] == 1001)].iloc[0]
    assert red["en_camino"] == 3000.0 and red["primera_entrada_dia"] == 4
    if red["sugerido"] > 0:
        assert red["sugerido_compra"] == np.ceil(red["sugerido"] / 250) and red["unidad_compra"] == "Frasco 250 mL"


# 4 · Una propuesta que aumenta de importe exige las aprobaciones correspondientes
def test_4_propuesta_que_crece_recalcula_riesgo(sim):
    autonomia.set_politicas({"doble_aprobacion_riesgo_alto": True, "importe_riesgo_alto": 50000})
    cid = db.iniciar_corrida("demanda", {})
    r = autonomia.proponer("demanda", "solicitud_compra", "Solicitud de compra: 10 pz de Bloqueador bronquial",
                           {"producto_id": 1016, "cantidad": 10}, impacto={"cantidad": 10, "importe": 14500}, corrida_id=cid, sincronizar=False)
    assert r["riesgo"] == "medio"
    ctx = planificador.ContextoPlan({"resurtido": pd.DataFrame(), "pronosticos": pd.DataFrame()}, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [], cid, P.ConfigPronostico())
    # primera aprobación parcial de un operador, luego el planificador sube la cantidad ×5 (importe 72,500 > umbral)
    aj = ctx.ajustar_propuesta(r["id"], 50, "jornada extraordinaria")
    assert aj["riesgo"] == "alto" and aj["version"] == 2
    a = db.accion(r["id"])
    assert a["riesgo"] == "alto" and a["version"] == 2 and a["impacto"]["importe"] == 72500
    # ahora exige dos personas
    assert autonomia.aprobar(r["id"], "luis", "operacion")["estado"] == "aprobada_parcial"
    # si vuelve a cambiar tras la primera aprobación, la aprobación parcial se anula
    ctx.ajustar_propuesta(r["id"], 55, "otro ajuste")
    a = db.accion(r["id"])
    assert a["estado"] == "propuesta" and a["version"] == 3 and not a["aprobado_por"]
    # una aprobación queda ligada a la versión exacta: si la propuesta cambia después, NO se ejecuta
    assert autonomia.aprobar(r["id"], "luis", "operacion")["estado"] == "aprobada_parcial"
    db.actualizar_accion(r["id"], estado="aprobada", version_aprobada=3, version=4)   # aprobada v3, modificada a v4 antes de ejecutar
    res = autonomia.ejecutar(r["id"])
    assert res["estado"] == "propuesta" and "cambió" in res["mensaje"]
    assert db.accion(r["id"])["estado"] == "propuesta"


# 4b · Una transferencia ajustada nunca saca más de lo que el origen tiene utilizable
def test_4b_transferencia_no_excede_origen(sim):
    cid = db.iniciar_corrida("demanda", {})
    r = autonomia.proponer("demanda", "transferencia_interna", "Transferir 100 mL de Sevoflurano 250 mL frasco · CEDIS-MTY/Stock → HGZ17/Stock",
                           {"producto_id": 1001, "cantidad": 100, "origen": "CEDIS-MTY/Stock", "destino": "HGZ17/Stock", "origen_id": 1, "destino_id": 2},
                           impacto={"cantidad": 100, "importe": 1500, "stock_origen": 400}, corrida_id=cid, sincronizar=False)
    res = pd.DataFrame([{"nivel": "local", "almacen": "CEDIS-MTY/Stock", "producto_id": 1001, "producto": "Sevoflurano 250 mL frasco",
                         "stock_actual": 400.0, "stock_proyectado": 400.0, "costo_unit": 15.0, "es_cedis": True, "criticidad": "fuente"}])
    ctx = planificador.ContextoPlan({"resurtido": res, "pronosticos": pd.DataFrame()}, res, pd.DataFrame(), pd.DataFrame(), [], cid, P.ConfigPronostico())
    aj = ctx.ajustar_propuesta(r["id"], 900, "jornada extraordinaria")
    assert aj["ok"] and aj["despues"] == 400 and aj["faltante_origen"] == 500
    a = db.accion(r["id"])
    assert a["payload"]["cantidad"] == 400 and a["version"] == 2 and "faltan 500" in a["motivo"]
    # y si ya está en el tope, no genera una versión nueva sin cambio real
    aj2 = ctx.ajustar_propuesta(r["id"], 950, "otro intento")
    assert not aj2["ok"] and db.accion(r["id"])["version"] == 2


# 5 · Una respuesta perdida de Odoo no genera documentos duplicados
class _ClienteRespuestaPerdida:
    """Envuelve al simulador: la PRIMERA creación de stock.picking se ejecuta en Odoo pero la respuesta 'se pierde'."""
    def __init__(self, real):
        self._real, self.fallos = real, 1
    def __getattr__(self, n):
        return getattr(self._real, n)
    def create(self, modelo, vals):
        rid = self._real.create(modelo, vals)
        if modelo == "stock.picking" and self.fallos:
            self.fallos -= 1
            raise OdooError("Fallo de conexión con Odoo: ReadTimeout")
        return rid


def test_5_respuesta_perdida_sin_duplicados(sim):
    from app.odoo import client as odoo_client
    envuelto = _ClienteRespuestaPerdida(sim)
    n0 = len(sim.tablas["stock.picking"])
    res = OA.crear_transferencia_interna(1004, 3, 507, 505, referencia="Agente IA · acción #TEST-PERDIDA", cli=envuelto)
    assert res["reutilizado"] is True
    assert len(sim.tablas["stock.picking"]) == n0 + 1          # un solo documento
    # y las escrituras del cliente real no se reintentan solas
    assert "create" not in odoo_client.OdooClient.METODOS_LECTURA and "search_read" in odoo_client.OdooClient.METODOS_LECTURA


# 6 · Los expedientes distinguen hechos, hipótesis y datos faltantes
def test_6_expediente_separa_hechos_hipotesis_faltantes(sim):
    df = queries.consumo(dias=180)
    res = A.detectar(df)
    ctx = investigador.Contexto(df)
    h = res["hallazgos"].iloc[0].to_dict(); h.update({"tipo": "anomalia", "titulo": "t", "fecha": str(h["fecha"])})
    e = investigador.expediente_determinista(h, ctx)
    assert e["evidencia"] and all(any(ch.isdigit() for ch in x) for x in e["evidencia"][:2])   # hechos con cifras
    assert e["hipotesis"] and all(set(x) >= {"hipotesis", "plausibilidad", "a_favor", "en_contra", "como_verificar"} for x in e["hipotesis"])
    assert all("probabilidad" not in x for x in e["hipotesis"])
    assert e["datos_faltantes"] and e["confianza"] in ("media", "baja")
    # la confianza no depende de la severidad
    h2 = dict(h); h2["severidad"] = "critica"
    assert investigador.expediente_determinista(h2, ctx)["confianza"] == e["confianza"]
