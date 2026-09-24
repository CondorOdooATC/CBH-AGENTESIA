"""Herramientas del Copiloto (definiciones para Claude + despachador)."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from .. import db
from ..odoo import queries, schema
from ..odoo.client import get_client
from ..reports import builders
from ..agents import autonomia
from ..agents.base import df_registros

HERRAMIENTAS: list[dict] = [
    {"name": "consultar_consumo",
     "description": "Consulta líneas de consumo de insumos/anestésicos desde Odoo en un rango de fechas, con filtros y "
                    "agrupación opcional. Devuelve filas (máx. 500) o agregados. Úsala para responder preguntas de "
                    "consumo por hospital, médico, auxiliar, producto, folio, lote, sub-almacén o fecha.",
     "input_schema": {"type": "object", "properties": {
         "desde": {"type": "string", "description": "Fecha inicial YYYY-MM-DD"},
         "hasta": {"type": "string", "description": "Fecha final YYYY-MM-DD"},
         "dias": {"type": "integer", "description": "Alternativa: últimos N días (por defecto 30)"},
         "filtro_texto": {"type": "string", "description": "Texto a buscar en producto/hospital/médico/auxiliar/folio/lote"},
         "agrupar_por": {"type": "array", "items": {"type": "string"},
                         "description": "Columnas para agrupar: producto, hospital, medico, auxiliar, subalmacen, folio, lote, dia, mes"},
         "limite": {"type": "integer", "description": "Máximo de filas (por defecto 200)"}}, "required": []}},
    {"name": "consultar_existencias",
     "description": "Existencias actuales por producto y ubicación/almacén (con lote y caducidad si aplica).",
     "input_schema": {"type": "object", "properties": {
         "filtro_texto": {"type": "string", "description": "Texto a buscar en producto o ubicación"},
         "agrupar_por": {"type": "array", "items": {"type": "string"}, "description": "producto, almacen, ubicacion, lote"},
         "limite": {"type": "integer"}}, "required": []}},
    {"name": "consultar_odoo",
     "description": "Lectura genérica de cualquier modelo de Odoo (search_read) con dominio. Sólo lectura. Útil para "
                    "folios, contactos, empleados, órdenes de compra, transferencias, facturas, etc. Con `agrupar_por` y `sumar` "
                    "devuelve totales agregados (read_group), p. ej. compras por proveedor o movimientos por producto.",
     "input_schema": {"type": "object", "properties": {
         "modelo": {"type": "string"}, "dominio": {"type": "array", "items": {}, "description": "Dominio Odoo, p.ej. [[\"state\",\"=\",\"done\"]]"},
         "campos": {"type": "array", "items": {"type": "string"}}, "limite": {"type": "integer"},
         "orden": {"type": "string"},
         "agrupar_por": {"type": "array", "items": {"type": "string"}, "description": "campos por los que agrupar (read_group)"},
         "sumar": {"type": "array", "items": {"type": "string"}, "description": "campos numéricos a sumar por grupo"}}, "required": ["modelo"]}},
    {"name": "consultar_facturacion",
     "description": "Análisis contable de facturación y cobranza desde Odoo (sólo lectura): facturas de cliente o de proveedor en un "
                    "periodo, con importes, saldo pendiente, estado de pago y vencimiento; resumen (facturado, cobrado, por cobrar, "
                    "vencido, top clientes, por mes) o detalle por factura o por línea de producto (qué se facturó, a quién, cuánto). "
                    "Úsala para preguntas de ventas, ingresos, cartera, morosidad, facturación por hospital/producto.",
     "input_schema": {"type": "object", "properties": {
         "desde": {"type": "string"}, "hasta": {"type": "string"}, "dias": {"type": "integer", "description": "por defecto 90"},
         "tipo": {"type": "string", "enum": ["cliente", "proveedor"], "description": "cliente = ventas/cobranza; proveedor = compras/pagos"},
         "nivel": {"type": "string", "enum": ["resumen", "facturas", "lineas"], "description": "resumen (por defecto), facturas o lineas de producto"},
         "filtro_texto": {"type": "string", "description": "cliente, producto o folio de factura"},
         "agrupar_por": {"type": "array", "items": {"type": "string"}, "description": "cliente, mes, estado_pago, producto (en lineas)"},
         "limite": {"type": "integer"}}, "required": []}},
    {"name": "listar_campos",
     "description": "Lista los campos de un modelo de Odoo (nombre técnico, etiqueta, tipo).",
     "input_schema": {"type": "object", "properties": {"modelo": {"type": "string"}}, "required": ["modelo"]}},
    {"name": "ejecutar_agente",
     "description": "Ejecuta un agente ahora: 'consumo' (anomalías) o 'demanda' (pronóstico y resurtido). Tarda de 10 a 60 s. "
                    "Devuelve KPIs, informe y enlace al Excel.",
     "input_schema": {"type": "object", "properties": {
         "agente": {"type": "string", "enum": ["consumo", "demanda"]},
         "dias": {"type": "integer", "description": "Ventana histórica (consumo)"},
         "horizonte": {"type": "integer", "description": "Días a pronosticar (demanda)"}}, "required": ["agente"]}},
    {"name": "ultimos_resultados",
     "description": "Resultados más recientes de un agente sin volver a ejecutarlo (KPIs, hallazgos/alertas top, informe).",
     "input_schema": {"type": "object", "properties": {"agente": {"type": "string", "enum": ["consumo", "demanda", "briefing"]}},
                      "required": ["agente"]}},
    {"name": "listar_hallazgos",
     "description": "Hallazgos de anomalías guardados, con filtros por severidad/estado.",
     "input_schema": {"type": "object", "properties": {
         "severidad": {"type": "string", "enum": ["critica", "alta", "media", "baja"]},
         "estado": {"type": "string", "enum": ["nueva", "revisada", "justificada", "confirmada", "descartada"]},
         "limite": {"type": "integer"}}, "required": []}},
    {"name": "clasificar_hallazgo",
     "description": "Marca un hallazgo como justificada/confirmada/descartada/revisada con una nota. Esto entrena al agente.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}, "estado": {"type": "string", "enum": ["revisada", "justificada", "confirmada", "descartada"]},
         "nota": {"type": "string"}}, "required": ["id", "estado"]}},
    {"name": "listar_resurtido",
     "description": "Plan de resurtido vigente (última corrida del Agente 2) con filtro por criticidad.",
     "input_schema": {"type": "object", "properties": {
         "criticidad": {"type": "string", "enum": ["desabasto", "critico", "reordenar", "ok", "exceso", "sin_movimiento", "fuente"]},
         "limite": {"type": "integer"}}, "required": []}},
    {"name": "generar_excel",
     "description": "Genera un archivo Excel con las hojas y filas indicadas y devuelve el enlace de descarga. Usa esto "
                    "cuando el usuario pida un Excel/reporte/archivo. Las filas pueden ser listas de objetos.",
     "input_schema": {"type": "object", "properties": {
         "titulo": {"type": "string"},
         "hojas": {"type": "array", "items": {"type": "object", "properties": {
             "nombre": {"type": "string"}, "columnas": {"type": "array", "items": {"type": "string"}},
             "filas": {"type": "array", "items": {}}, "formatos": {"type": "object"},
             "totales": {"type": "array", "items": {"type": "string"}}, "texto": {"type": "string"}},
             "required": ["nombre", "filas"]}},
         "notas": {"type": "array", "items": {"type": "string"}},
         "kpis": {"type": "array", "items": {"type": "array", "items": {}}}}, "required": ["titulo", "hojas"]}},
    {"name": "excel_desde_consulta",
     "description": "Atajo para reportes de cualquier cosa: ejecuta consultar_consumo, consultar_existencias, consultar_facturacion "
                    "o consultar_odoo (fuente 'odoo': parametros = modelo, dominio, campos, agrupar_por, sumar) con los mismos parámetros y "
                    "guarda TODAS las filas (hasta el límite de Excel, ~1 millón) directamente en un Excel, sin pasar los datos por el chat. "
                    "Ideal para reportes grandes (kardex, consumo mensual completo, cartera, compras por proveedor…).",
     "input_schema": {"type": "object", "properties": {
         "fuente": {"type": "string", "enum": ["consumo", "existencias", "facturacion", "odoo"]}, "titulo": {"type": "string"},
         "parametros": {"type": "object", "description": "Los mismos parámetros que la consulta"}},
         "required": ["fuente", "titulo"]}},
    {"name": "enviar_correo",
     "description": "Propone enviar un correo electrónico desde el servidor de correo de Odoo a personas (nombres o logins de usuarios de "
                    "Odoo, contactos, o direcciones de correo). Puede adjuntar un Excel generado en esta conversación (reporte_id). Como toda "
                    "acción, queda en la cola y se envía cuando el usuario la aprueba (pregunta «¿lo envío?» y con el sí llama aprobar_accion). "
                    "Úsala cuando pidan «mándale un correo a…», «envía el reporte a…», «notifica por correo…».",
     "input_schema": {"type": "object", "properties": {
         "para": {"type": "array", "items": {"type": "string"}, "description": "nombres, logins o correos"},
         "asunto": {"type": "string"}, "cuerpo": {"type": "string", "description": "texto o HTML sencillo"},
         "reporte_id": {"type": "integer", "description": "id del Excel generado (lo devuelven generar_excel / excel_desde_consulta) para adjuntarlo"}},
         "required": ["para", "asunto", "cuerpo"]}},
    {"name": "proponer_accion",
     "description": "Propone una acción en Odoo que quedará en la cola de aprobación humana: transferencia_interna "
                    "(producto_id, cantidad, origen, destino por nombre de ubicación), solicitud_compra (producto_id, cantidad), "
                    "regla_reabastecimiento (producto_id, ubicacion, minimo, maximo), actividad (modelo, res_id, resumen, nota), "
                    "Para solicitud_compra puedes dar `cantidad_compra` en la unidad de compra del producto (frascos, cajas) en vez de "
                    "`cantidad` en unidad base; la herramienta convierte y redondea a unidades enteras de compra. "
                    "nota_chatter (modelo, res_id, cuerpo), alerta (titulo) o aviso_equipo (equipo: facturacion|calidad|operaciones|direccion, "
                    "asunto, cuerpo, plazo_dias; opcional producto_id, modelo y res_id): avisa a personas concretas de Odoo con una actividad "
                    "y una notificación; el cliente NO usa Helpdesk, así que para 'avisar a contabilidad/administración' usa aviso_equipo. "
                    "Para avisar a personas concretas de Odoo agrega `personas` (nombres o logins) al payload del aviso_equipo.",
     "input_schema": {"type": "object", "properties": {
         "tipo": {"type": "string", "enum": ["transferencia_interna", "solicitud_compra", "regla_reabastecimiento",
                                             "actividad", "nota_chatter", "alerta", "aviso_equipo"]},
         "titulo": {"type": "string"}, "motivo": {"type": "string"},
         "payload": {"type": "object"}, "impacto": {"type": "object"}}, "required": ["tipo", "titulo", "payload"]}},
    {"name": "listar_acciones",
     "description": "Cola de acciones autónomas (propuestas, aprobadas, ejecutadas…).",
     "input_schema": {"type": "object", "properties": {"estado": {"type": "string"}, "limite": {"type": "integer"}}, "required": []}},
    {"name": "aprobar_accion",
     "description": "Aprueba una propuesta pendiente (por id) y, si con eso queda aprobada, LA EJECUTA EN ODOO de inmediato "
                    "(crea la RFQ, la transferencia, el aviso…) con todas las revalidaciones. Úsala SÓLO cuando el usuario haya "
                    "confirmado de forma explícita en su mensaje que quiere aprobarla/ejecutarla; pasa esa frase en "
                    "`confirmacion_usuario`. Devuelve el estado real (ejecutada + referencia de Odoo, aprobada_parcial si falta un "
                    "administrador, requiere_revision, aprobada_manual, error).",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}, "confirmacion_usuario": {"type": "string"}},
                      "required": ["id", "confirmacion_usuario"]}},
    {"name": "rechazar_accion",
     "description": "Rechaza una propuesta pendiente (por id) con el motivo que dio el usuario; los agentes aprenden del rechazo.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}, "motivo": {"type": "string"}}, "required": ["id", "motivo"]}},
    {"name": "registrar_aprendizaje",
     "description": "Guarda una nota de aprendizaje permanente que los agentes respetarán (excepciones, contexto operativo, "
                    "reglas del cliente). ambito: global|producto|hospital|medico|auxiliar|unidad|agente; clave: nombre del "
                    "producto/hospital/etc.",
     "input_schema": {"type": "object", "properties": {
         "ambito": {"type": "string"}, "clave": {"type": "string"}, "nota": {"type": "string"}}, "required": ["ambito", "nota"]}},
    {"name": "buscar",
     "description": "Busca productos, ubicaciones, unidades médicas, médicos o empleados por nombre y devuelve ids.",
     "input_schema": {"type": "object", "properties": {
         "tipo": {"type": "string", "enum": ["producto", "ubicacion", "unidad_medica", "medico", "empleado", "proveedor"]},
         "texto": {"type": "string"}}, "required": ["tipo", "texto"]}},
    {"name": "listar_casos",
     "description": "Expedientes de investigación (casos) creados por el Agente Investigador, con conclusión, confianza, impacto y acción.",
     "input_schema": {"type": "object", "properties": {"estado": {"type": "string", "enum": ["abierto", "en_revision", "resuelto", "descartado"]},
                                                       "limite": {"type": "integer"}}, "required": []}},
    {"name": "ver_caso", "description": "Expediente completo de un caso (evidencia, hipótesis con probabilidad, conclusión, trazas de investigación).",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "investigar_hallazgo", "description": "Lanza la investigación de un hallazgo (por id) y devuelve el expediente. Tarda 20-60 s.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "registrar_aclaracion", "description": "Registra una explicación aportada por una persona sobre un caso como ACLARACIÓN "
                                                    "declarada (con autor, fecha y alcance): NO es un hecho verificado ni una regla global. "
                                                    "Alcance: caso (por defecto), actor (auxiliar/médico), producto_hospital u hospital.",
     "input_schema": {"type": "object", "properties": {"caso_id": {"type": "integer"}, "texto": {"type": "string"},
                                                       "alcance": {"type": "string", "enum": ["caso", "actor", "producto_hospital", "hospital"]}},
                      "required": ["caso_id", "texto"]}},
    {"name": "resolver_caso", "description": "Cierra un caso como resuelto o descartado con la resolución (esto entrena a los agentes).",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}, "estado": {"type": "string", "enum": ["resuelto", "descartado", "en_revision"]},
                                                       "resolucion": {"type": "string"}}, "required": ["id", "estado"]}},
    {"name": "estado_plataforma",
     "description": "Estado de conexión a Odoo, mapeo de modelos, nivel de autonomía, presupuesto de tokens y últimas corridas.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


# ── despachador con control de roles ────────────────────────────────────────
_ROLES = {"consulta": 1, "operacion": 2, "admin": 3, "condor": 4}
PERMISOS = {  # rol mínimo por herramienta; las no listadas son de consulta
    "ejecutar_agente": "operacion", "clasificar_hallazgo": "operacion", "proponer_accion": "operacion",
    "registrar_aprendizaje": "operacion", "investigar_hallazgo": "operacion", "resolver_caso": "operacion",
    "registrar_aclaracion": "consulta", "aprobar_accion": "operacion", "rechazar_accion": "operacion", "enviar_correo": "operacion",
    "estado_plataforma": "admin",       # conexión, mapeo y consumo de tokens: información de Configuración (sólo administradores)
}


def ejecutar(nombre: str, args: dict, usuario: str = "", rol: str = "consulta") -> Any:
    fn = _REGISTRO.get(nombre)
    if not fn:
        raise ValueError(f"Herramienta desconocida: {nombre}")
    minimo = PERMISOS.get(nombre, "consulta")
    if _ROLES.get(rol, 0) < _ROLES[minimo]:
        raise PermissionError(f"La herramienta «{nombre}» requiere rol {minimo}; el usuario {usuario} tiene rol {rol}. "
                              "Explícale al usuario que no tiene permiso y sugiere pedirlo a un administrador.")
    return fn(args, usuario, rol)


def _consumo_df(args: dict) -> pd.DataFrame:
    df = queries.consumo(desde=args.get("desde"), hasta=args.get("hasta"), dias=int(args.get("dias") or 30), tope=200_000)
    ft = (args.get("filtro_texto") or "").strip().lower()
    if ft and not df.empty:
        cols = ["producto", "hospital", "medico", "auxiliar", "folio", "lote", "subalmacen", "almacen"]
        mask = pd.Series(False, index=df.index)
        for c in cols:
            if c in df.columns:
                mask |= df[c].astype(str).str.lower().str.contains(ft, regex=False)
        df = df[mask]
    return df


def _agrupar(df: pd.DataFrame, por: list[str], valor_cols: list[str]) -> pd.DataFrame:
    d = df.copy()
    if "mes" in por:
        d["mes"] = pd.to_datetime(d["fecha"]).dt.to_period("M").astype(str)
    if "dia" in por:
        d["dia"] = pd.to_datetime(d["fecha"]).dt.date.astype(str)
    por = [c for c in por if c in d.columns]
    if not por:
        return d
    agg = {c: "sum" for c in valor_cols if c in d.columns}
    if "folio" in d.columns and "folio" not in por:
        agg["folio"] = "nunique"
    g = d.groupby(por, as_index=False).agg(agg)
    if "folio" in g.columns and "folio" not in por:
        g = g.rename(columns={"folio": "folios"})
    g["lineas"] = d.groupby(por).size().values
    return g.sort_values(valor_cols[0] if valor_cols and valor_cols[0] in g.columns else por[0], ascending=False)


_ALIAS_COLUMNAS = {"tecnico": "auxiliar", "técnico": "auxiliar", "empleado": "auxiliar", "unidad": "hospital", "unidad_medica": "hospital",
                   "unidad médica": "hospital", "hospital_id": "hospital", "anestesiologo": "medico", "anestesiólogo": "medico", "doctor": "medico",
                   "articulo": "producto", "artículo": "producto", "insumo": "producto", "ubicacion": "subalmacen", "quirofano": "quirofano"}


def t_consultar_consumo(args, usuario, rol):
    df = _consumo_df(args)
    lim = int(args.get("limite") or 200)
    por = [_ALIAS_COLUMNAS.get(str(c).strip().lower(), str(c).strip().lower()) for c in (args.get("agrupar_por") or [])]
    if df.empty:
        return {"filas": [], "total_filas": 0, "mensaje": "Sin consumo con esos filtros."}
    if por:
        g = _agrupar(df, por, ["cantidad", "importe"])
        return {"filas": df_registros(g.round(2), lim), "total_filas": int(len(g)), "agrupado_por": por,
                "totales": {"cantidad": round(float(df["cantidad"].sum()), 2), "importe": round(float(df["importe"].sum()), 2)}}
    cols = ["fecha", "folio", "hospital", "subalmacen", "quirofano", "medico", "cirujano", "auxiliar", "producto", "lote", "cantidad", "unidad",
            "importe", "duracion_min", "peso_inicial", "peso_final", "consumo_ml", "tipo_evento", "estado_folio"]
    cols = [c for c in cols if c in df.columns]
    return {"filas": df_registros(df.sort_values("fecha", ascending=False)[cols], lim), "total_filas": int(len(df)),
            "totales": {"cantidad": round(float(df["cantidad"].sum()), 2), "importe": round(float(df["importe"].sum()), 2)}}


def t_consultar_existencias(args, usuario, rol):
    df = queries.existencias()
    ft = (args.get("filtro_texto") or "").strip().lower()
    if ft and not df.empty:
        mask = df["producto"].astype(str).str.lower().str.contains(ft, regex=False) | \
            df["ubicacion"].astype(str).str.lower().str.contains(ft, regex=False)
        df = df[mask]
    if df.empty:
        return {"filas": [], "total_filas": 0}
    por = args.get("agrupar_por") or []
    lim = int(args.get("limite") or 300)
    if por:
        g = _agrupar(df, por, ["cantidad"] + (["disponible"] if "disponible" in df.columns else []))
        return {"filas": df_registros(g.round(2), lim), "total_filas": int(len(g))}
    cols = [c for c in ("producto", "almacen", "ubicacion", "lote", "caducidad", "cantidad", "disponible") if c in df.columns]
    return {"filas": df_registros(df[cols].sort_values(["producto", "ubicacion"]), lim), "total_filas": int(len(df))}


_MODELOS_VETADOS = ("res.users", "ir.config", "auth", "res.users.apikeys", "hr.payslip", "hr.contract", "hr.salary", "ir.mail_server", "fetchmail",
                    "res.partner.bank", "account.bank", "payment.token", "ir.attachment", "mail.message", "mail.mail")
# Minimización de datos: la identidad y los datos clínicos del paciente nunca salen de Odoo, ni a petición del usuario.
# Se veta por nombre de campo (cualquier modelo) para que un modelo custom nuevo quede cubierto sin tocar código.
_CAMPOS_VETADOS = ("patient", "paciente", "nss", "curp", "diagnos", "birthdate", "fecha_nacimiento", "gender", "sexo",
                   "afiliacion", "expediente", "password", "api_key", "token", "secret", "vat", "rfc", "bank", "iban", "clabe")


def _campo_vetado(nombre: str) -> bool:
    n = str(nombre or "").lower()
    return any(v in n for v in _CAMPOS_VETADOS)


def _campos_del_dominio(dominio) -> list[str]:
    out = []
    for termino in dominio or []:
        if isinstance(termino, (list, tuple)) and len(termino) == 3 and isinstance(termino[0], str):
            out.append(termino[0])
    return out


def t_consultar_odoo(args, usuario, rol):
    modelo = args["modelo"]
    if any(x in modelo for x in _MODELOS_VETADOS):
        raise ValueError("Modelo no permitido (usuarios, credenciales, nómina, adjuntos, mensajes y datos bancarios no se consultan desde el chat).")
    dominio = args.get("dominio") or []
    if any(_campo_vetado(c) for c in _campos_del_dominio(dominio)):
        raise ValueError("No se consultan ni se filtran datos de pacientes ni datos sensibles (identidad, NSS, diagnóstico, credenciales, cuentas bancarias).")
    cli = get_client()
    por = [str(c) for c in (args.get("agrupar_por") or []) if c and not _campo_vetado(c)]
    if por:
        sumar = [str(c) for c in (args.get("sumar") or []) if c and not _campo_vetado(c)]
        grupos = cli.read_group(modelo, dominio, sumar + por, por, limite=min(int(args.get("limite") or 200), 2000))
        filas = []
        for g in grupos:
            fila = {c: (g.get(c)[1] if isinstance(g.get(c), (list, tuple)) else g.get(c)) for c in por}
            fila.update({c: g.get(c) for c in sumar})
            fila["registros"] = g.get(f"{por[0]}_count") or g.get("__count")
            filas.append(fila)
        return {"filas": filas, "total_filas": len(filas), "agrupado_por": por}
    pedidos = [str(c) for c in (args.get("campos") or []) if c]
    campos = [c for c in pedidos if not _campo_vetado(c)]
    if not campos:
        # sin campos explícitos NO se devuelve el registro completo (traería todo, pacientes incluidos): sólo lo identificable
        campos = ["display_name"]
    rows = cli.search_read(modelo, dominio, campos, limite=min(int(args.get("limite") or 100), 500), orden=args.get("orden"))
    out = {"filas": rows, "total_filas": len(rows)}
    omitidos = [c for c in pedidos if _campo_vetado(c)]
    if omitidos:
        out["campos_omitidos"] = omitidos
        out["nota"] = "Los datos de pacientes y datos sensibles no se consultan desde la plataforma."
    return out


def _facturacion_df(p: dict) -> pd.DataFrame:
    nivel = p.get("nivel") or "resumen"
    fn = queries.facturacion_lineas if nivel == "lineas" else queries.facturacion
    df = fn(desde=p.get("desde"), hasta=p.get("hasta"), dias=int(p.get("dias") or 90), tipo=p.get("tipo") or "cliente")
    ft = (p.get("filtro_texto") or "").strip().lower()
    if ft and not df.empty:
        mask = pd.Series(False, index=df.index)
        for c in ("cliente", "producto", "factura", "origen"):
            if c in df.columns:
                mask |= df[c].astype(str).str.lower().str.contains(ft, regex=False)
        df = df[mask]
    return df


def t_consultar_facturacion(args, usuario, rol):
    nivel = args.get("nivel") or "resumen"
    df = _facturacion_df(args)
    lim = int(args.get("limite") or 200)
    if df.empty:
        return {"filas": [], "total_filas": 0, "mensaje": "Sin facturas con esos filtros (o el usuario técnico no tiene acceso a Contabilidad)."}
    por = [_ALIAS_COLUMNAS.get(str(c).strip().lower(), str(c).strip().lower()) for c in (args.get("agrupar_por") or [])]
    if nivel == "resumen" and not por:
        return {"resumen": queries.resumen_facturacion(df) if "saldo" in df.columns else {"lineas": int(len(df)), "importe": round(float(df["importe"].sum()), 2)}}
    valores = ["cantidad", "importe"] if nivel == "lineas" else ["total", "saldo"]
    if por:
        g = _agrupar(df, por, valores)
        return {"filas": df_registros(g.round(2), lim), "total_filas": int(len(g)), "agrupado_por": por}
    cols = [c for c in ("factura", "cliente", "fecha", "vencimiento", "subtotal", "total", "saldo", "estado", "estado_pago", "vencida", "origen",
                        "producto", "cantidad", "precio_unitario", "importe") if c in df.columns]
    return {"filas": df_registros(df.sort_values("fecha", ascending=False)[cols], lim), "total_filas": int(len(df)),
            "totales": {c: round(float(df[c].sum()), 2) for c in valores if c in df.columns}}


def t_enviar_correo(args, usuario, rol):
    from ..odoo import acciones as OA
    dest = OA.resolver_destinatarios_correo(list(args.get("para") or []))
    if not dest["correos"]:
        return {"estado": "no_propuesta", "motivo": "No encontré destinatarios con correo: " + "; ".join(dest["no_resueltos"])}
    payload = {"correos": dest["correos"], "destinatarios": [d.get("nombre") or d["correo"] for d in dest["detalle"]],
               "n_destinatarios": len(dest["correos"]), "destinatarios_texto": ", ".join(dest["correos"][:4]) + (f" y {len(dest['correos']) - 4} más" if len(dest["correos"]) > 4 else ""),
               "asunto": str(args.get("asunto") or "")[:200], "cuerpo": str(args.get("cuerpo") or ""), "reporte_id": args.get("reporte_id"),
               "adjunto_texto": ""}
    if args.get("reporte_id"):
        rep = db.reporte(int(args["reporte_id"]))
        if not rep:
            return {"estado": "no_propuesta", "motivo": f"No existe el reporte #{args['reporte_id']}."}
        payload["adjunto_texto"] = f", adjuntando el Excel «{rep['archivo']}»"
    r = autonomia.proponer("copiloto", "correo", f"Correo: {payload['asunto']}"[:140], payload,
                           motivo=(f"Solicitado por {usuario} desde el chat." + (f" Sin resolver: {'; '.join(dest['no_resueltos'])}." if dest["no_resueltos"] else "")),
                           usuario=usuario, sincronizar=False)
    if r.get("id"):
        a = db.accion(int(r["id"])) or {}
        r.update({"efecto": a.get("efecto"), "destinatarios": dest["correos"], "no_resueltos": dest["no_resueltos"],
                  "siguiente_paso": "Muestra a quién y qué se enviará y pregunta «¿lo envío?»; sólo con el sí llama aprobar_accion."})
    return r


def t_listar_campos(args, usuario, rol):
    f = get_client().fields_get(args["modelo"])
    return {"campos": [{"nombre": k, "etiqueta": v.get("string"), "tipo": v.get("type"), "relacion": v.get("relation")}
                       for k, v in sorted(f.items())][:300]}


def t_ejecutar_agente(args, usuario, rol):
    from ..agents import consumo, demanda
    if args["agente"] == "consumo":
        r = consumo.ejecutar(dias=args.get("dias"), usuario=usuario, disparo="copiloto")
    else:
        r = demanda.ejecutar(horizonte=args.get("horizonte"), usuario=usuario, disparo="copiloto")
    return {"corrida_id": r["corrida_id"], "kpis": r["kpis"], "informe": r["informe"][:6000], "reporte": r["reporte"],
            "acciones_propuestas": len(r["acciones"]), "segundos": r["segundos"]}


def t_ultimos_resultados(args, usuario, rol):
    clave = {"consumo": "agente1_ultimo", "demanda": "agente2_ultimo", "briefing": "ultimo_briefing"}[args["agente"]]
    d = db.get_ajuste(clave, {}) or {}
    if not d:
        return {"mensaje": "Ese agente aún no se ha ejecutado."}
    d = dict(d)
    if "informe" in d:
        d["informe"] = d["informe"][:6000]
    return d


def t_listar_hallazgos(args, usuario, rol):
    rows = db.anomalias(estado=args.get("estado"), severidad=args.get("severidad"), limite=int(args.get("limite") or 100))
    for r in rows:
        for k in ("metodos", "motivos"):
            try:
                r[k] = json.loads(r[k]) if r.get(k) else []
            except (json.JSONDecodeError, TypeError):
                pass
    return {"filas": rows, "total_filas": len(rows)}


def t_clasificar_hallazgo(args, usuario, rol):
    from ..agents import consumo
    return consumo.retroalimentar(int(args["id"]), args["estado"], args.get("nota", ""), usuario)


def t_listar_resurtido(args, usuario, rol):
    u = db.ultima_corrida("demanda")
    if not u:
        return {"mensaje": "El Agente 2 aún no se ha ejecutado."}
    rows = db.resurtido(corrida_id=u["id"], criticidad=args.get("criticidad"), limite=int(args.get("limite") or 200))
    return {"corrida_id": u["id"], "filas": rows, "total_filas": len(rows)}


def t_generar_excel(args, usuario, rol):
    return builders.reporte_libre(args["titulo"], args["hojas"], args.get("notas"), args.get("kpis"), usuario)


def t_excel_desde_consulta(args, usuario, rol):
    p = args.get("parametros") or {}
    if args["fuente"] == "consumo":
        df = _consumo_df(p)
        por = p.get("agrupar_por") or []
        if por and not df.empty:
            df = _agrupar(df, por, ["cantidad", "importe"])
        else:
            cols = ["fecha", "folio", "hospital", "subalmacen", "medico", "auxiliar", "producto", "lote", "caducidad",
                    "cantidad", "unidad", "importe", "duracion_min", "peso_inicial", "peso_final", "consumo_peso"]
            df = df[[c for c in cols if c in df.columns]]
        fmt = {"importe": '"$"#,##0.00'}
    elif args["fuente"] == "facturacion":
        df = _facturacion_df(p)
        por = [_ALIAS_COLUMNAS.get(str(c).strip().lower(), str(c).strip().lower()) for c in (p.get("agrupar_por") or [])]
        valores = ["cantidad", "importe"] if (p.get("nivel") == "lineas") else ["total", "saldo"]
        if por and not df.empty:
            df = _agrupar(df, por, valores)
        fmt = {"total": '"$"#,##0.00', "saldo": '"$"#,##0.00', "subtotal": '"$"#,##0.00', "importe": '"$"#,##0.00'}
    elif args["fuente"] == "odoo":
        r = t_consultar_odoo({**p, "limite": p.get("limite") or 2000}, usuario, rol)
        df = pd.DataFrame(r["filas"])
        for c in df.columns:
            if df[c].map(lambda v: isinstance(v, (list, tuple))).any():
                df[c] = df[c].map(lambda v: v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else v)
        fmt = {}
    else:
        df = queries.existencias()
        ft = (p.get("filtro_texto") or "").lower()
        if ft and not df.empty:
            df = df[df["producto"].str.lower().str.contains(ft, regex=False) | df["ubicacion"].str.lower().str.contains(ft, regex=False)]
        por = p.get("agrupar_por") or []
        if por and not df.empty:
            df = _agrupar(df, por, ["cantidad"])
        fmt = {}
    total = int(len(df))
    meta = {"Parámetros": p, "Periodo": f"{p.get('desde') or ('últimos ' + str(p.get('dias') or 30) + ' días')} → {p.get('hasta') or 'hoy'}",
            "Fuente": args["fuente"]}
    return builders.reporte_dataframe(args["titulo"], df.reset_index(drop=True), tipo="consulta", formatos=fmt, usuario=usuario,
                                      metadatos=meta, total_origen=total)


def t_proponer_accion(args, usuario, rol):
    payload = dict(args.get("payload") or {})
    from ..odoo import acciones as OA
    if args["tipo"] == "transferencia_interna":
        for k in ("origen", "destino"):
            if k in payload and f"{k}_id" not in payload:
                u = OA.ubicacion_por_nombre(str(payload[k]))
                if not u:
                    raise ValueError(f"No encontré la ubicación «{payload[k]}».")
                payload[f"{k}_id"] = u["id"]
                payload[k] = u["complete_name"]
    if args["tipo"] == "regla_reabastecimiento" and "ubicacion_id" not in payload and payload.get("ubicacion"):
        u = OA.ubicacion_por_nombre(str(payload["ubicacion"]))
        if not u:
            raise ValueError(f"No encontré la ubicación «{payload['ubicacion']}».")
        payload["ubicacion_id"] = u["id"]
    if "producto" in payload and "producto_id" not in payload:
        pr = OA.producto_por_nombre(str(payload["producto"]))
        if not pr:
            raise ValueError(f"No encontré el producto «{payload['producto']}».")
        payload["producto_id"] = pr["id"]
        payload["producto"] = pr["display_name"]
        if not payload.get("unidad") and pr.get("uom_id"):
            payload["unidad"] = pr["uom_id"][1]
    elif payload.get("producto_id") and not payload.get("producto"):
        pr = OA.producto_por_nombre_o_id(int(payload["producto_id"]))
        if pr:
            payload["producto"] = pr["display_name"]
            payload.setdefault("unidad", pr["uom_id"][1] if pr.get("uom_id") else "")
    # compras pedidas en unidad de compra («10 frascos»): se convierten a unidad base con la conversión real de Odoo
    if args["tipo"] == "solicitud_compra" and payload.get("producto_id") and payload.get("cantidad_compra") not in (None, "") \
            and payload.get("cantidad") in (None, ""):
        uc = queries.unidades_compra().get(int(payload["producto_id"]))
        n = float(payload["cantidad_compra"])
        if uc and uc.get("ratio"):
            payload["cantidad"] = n * float(uc["ratio"])
            payload["unidad"] = uc.get("unidad_base") or payload.get("unidad", "")
            payload["unidad_compra"] = uc.get("unidad_compra")
        elif uc and uc.get("ratio") is None:
            return {"estado": "no_propuesta", "motivo": f"Odoo conoce la unidad de compra ({uc.get('unidad_compra')}) de este producto pero no la "
                                                        "conversión a su unidad base; corrígela en Odoo (Unidades de medida) antes de proponer."}
        else:
            payload["cantidad"] = n          # sin unidad de compra distinta: la unidad de compra es la base
    if args["tipo"] in ("solicitud_compra", "transferencia_interna") and payload.get("cantidad") in (None, ""):
        raise ValueError("Falta la cantidad (o cantidad_compra para compras).")
    if args["tipo"] == "solicitud_compra" and payload.get("proveedor") and not payload.get("proveedor_id"):
        prov = OA.get_client().search_read("res.partner", [["name", "ilike", str(payload["proveedor"])]], ["id", "name"], limite=3)
        if len(prov) == 1:
            payload["proveedor_id"], payload["proveedor"] = prov[0]["id"], prov[0]["name"]
        elif not prov:
            return {"estado": "no_propuesta", "motivo": f"No encontré el proveedor «{payload['proveedor']}» en los contactos de Odoo."}
        else:
            return {"estado": "no_propuesta", "motivo": f"«{payload['proveedor']}» es ambiguo: {', '.join(x['name'] for x in prov)}."}
    if args["tipo"] == "solicitud_compra" and payload.get("producto_id") and not payload.get("proveedor_id"):
        try:
            payload["sin_proveedor"] = not bool(OA.proveedor_de(int(payload["producto_id"])))
        except Exception:  # noqa: BLE001
            payload["sin_proveedor"] = False
        if payload["sin_proveedor"]:
            args["motivo"] = (args.get("motivo") or "") + " · Producto sin proveedor configurado en Odoo: la RFQ se creará con el proveedor en blanco (o «por definir») para que Compras lo asigne."
    # el copiloto pasa por los mismos controles que los agentes: mismas políticas, mismo libro de compromisos,
    # misma revalidación al aprobar; la existencia del origen se comprueba al proponer para no prometer lo que no hay
    if args["tipo"] == "transferencia_interna" and payload.get("origen") and payload.get("producto_id"):
        from ..odoo.client import get_client, OdooError
        try:
            q = get_client().search_read("stock.quant", [["product_id", "=", int(payload["producto_id"])], ["location_id", "=", int(payload["origen_id"])]],
                                         ["quantity", "reserved_quantity"], limite=200)
            disponible = sum(float(x.get("quantity") or 0) - float(x.get("reserved_quantity") or 0) for x in q)
            comp = float(autonomia.compromisos_activos()["origen"].get((int(payload["producto_id"]), str(payload["origen"])), 0.0))
            if float(payload.get("cantidad") or 0) > disponible - comp:
                return {"estado": "no_propuesta", "motivo": f"{payload['origen']} sólo puede ceder {max(0.0, disponible - comp):g} "
                                                            f"{payload.get('unidad', '')} ({disponible:g} disponibles en Odoo, {comp:g} ya comprometidos en otras propuestas)."}
        except (OdooError, KeyError, ValueError, TypeError):
            pass
    if args["tipo"] == "aviso_equipo":
        # destinatarios resueltos en Odoo al proponer, para que la tarjeta diga a quién llegará (equipo + personas concretas)
        logins_extra: list[str] = []
        for persona in (payload.get("personas") or []):
            persona = str(persona).strip()
            if not persona:
                continue
            if "@" in persona:
                logins_extra.append(persona.lower()); continue
            us = OA.get_client().search_read("res.users", [["name", "ilike", persona], ["share", "=", False]], ["login", "name"], limite=3)
            if len(us) == 1:
                logins_extra.append(str(us[0]["login"]).lower())
            elif not us:
                return {"estado": "no_propuesta", "motivo": f"No encontré a «{persona}» entre los usuarios de Odoo."}
            else:
                return {"estado": "no_propuesta", "motivo": f"«{persona}» es ambiguo: {', '.join(u['name'] for u in us)}. Indica el nombre completo o el login."}
        payload["logins_extra"] = logins_extra
        dest = OA.destinatarios_equipo(str(payload.get("equipo") or "operaciones"), incluir_logins=logins_extra)
        nombres = [p["nombre"] for p in dest["personas"]]
        payload.update({"equipo": dest["equipo"], "equipo_nombre": dest["nombre"], "n_destinatarios": len(nombres), "destinatarios": nombres,
                        "destinatarios_texto": ", ".join(nombres[:4]) + (f" y {len(nombres) - 4} más" if len(nombres) > 4 else "") if nombres else "nadie todavía",
                        "asunto": payload.get("asunto") or args["titulo"], "cuerpo": payload.get("cuerpo") or args.get("motivo", ""),
                        "plazo_dias": int(payload.get("plazo_dias") or 5)})
        if not nombres:
            return {"estado": "no_propuesta", "motivo": " ".join(dest["avisos"]) or "No hay destinatarios en Odoo para ese equipo."}
    r = autonomia.proponer("copiloto", args["tipo"], args["titulo"], payload, args.get("motivo", ""),
                           args.get("impacto"), usuario=usuario)
    if r.get("id"):
        a = db.accion(int(r["id"])) or {}
        r.update({"titulo": a.get("titulo"), "efecto": a.get("efecto"), "riesgo": a.get("riesgo"), "estado": a.get("estado"),
                  "cantidad": (a.get("payload") or {}).get("cantidad"), "unidad": a.get("unidad") or (a.get("payload") or {}).get("unidad"),
                  "siguiente_paso": "Muestra el efecto al usuario y pregúntale si la apruebas; sólo con su confirmación llama aprobar_accion."})
    return r


def t_listar_acciones(args, usuario, rol):
    return {"filas": db.acciones(estado=args.get("estado"), limite=int(args.get("limite") or 50)),
            "conteo": db.resumen_acciones(), "nivel_autonomia": autonomia.nivel()}


_CONFIRMACIONES = ("sí", "si ", "si,", "si.", "apru", "hazlo", "adelante", "ejecut", "confirm", "dale", "procede", "ok", "correcto",
                   "de acuerdo", "va ", "órale", "orale", "yes", "créala", "creala", "mándala", "mandala")


def t_aprobar_accion(args, usuario, rol):
    """La aprobación desde el chat pasa por EXACTAMENTE el mismo camino que el botón «Aprobar»: rol del usuario, candado,
    revalidación contra Odoo, doble aprobación en riesgo alto, gating de producción y ejecución idempotente. La única
    condición adicional es una confirmación explícita del usuario en la conversación (nunca la inventa el modelo)."""
    conf = str(args.get("confirmacion_usuario") or "").strip().lower()
    if not conf or not any(c in conf for c in _CONFIRMACIONES):
        return {"estado": "sin_confirmar", "mensaje": "Pide al usuario que confirme explícitamente («sí, apruébala») antes de aprobar."}
    a = db.accion(int(args["id"]))
    if not a:
        raise ValueError(f"No existe la propuesta #{args['id']}.")
    if a["estado"] not in autonomia.ESTADOS_PENDIENTES:
        return {"id": a["id"], "estado": a["estado"], "mensaje": f"La propuesta #{a['id']} ya no está pendiente (estado {a['estado']})."}
    r = autonomia.aprobar(int(args["id"]), usuario, rol)
    a2 = db.accion(int(args["id"])) or {}
    r["titulo"], r["efecto"] = a2.get("titulo"), a2.get("efecto")
    if a2.get("odoo_ref"):
        r["odoo_ref"], r["odoo_modelo"], r["estado_odoo"] = a2.get("odoo_ref"), a2.get("odoo_modelo"), a2.get("estado_odoo")
    db.log("info", "copiloto", f"Aprobación desde el chat de la propuesta #{args['id']}: {r.get('estado')}", conf[:200], usuario)
    return r


def t_rechazar_accion(args, usuario, rol):
    a = db.accion(int(args["id"]))
    if not a:
        raise ValueError(f"No existe la propuesta #{args['id']}.")
    if a["estado"] not in autonomia.ESTADOS_PENDIENTES:
        return {"id": a["id"], "estado": a["estado"], "mensaje": "La propuesta ya no está pendiente."}
    return autonomia.rechazar(int(args["id"]), usuario, str(args.get("motivo") or "Rechazada desde el chat"))


def t_registrar_aprendizaje(args, usuario, rol):
    i = db.agregar_aprendizaje(args["ambito"], args.get("clave", ""), args["nota"], usuario=usuario)
    return {"id": i, "guardado": True}


def t_registrar_aclaracion(args, usuario, rol):
    c = db.caso(int(args["caso_id"]))
    if not c:
        raise ValueError("Caso no encontrado.")
    i = db.agregar_aclaracion(c["id"], usuario, args["texto"], args.get("alcance") or "caso", c.get("entidades") or {}, fuente="chat")
    db.log("info", "casos", f"Aclaración #{i} registrada en el caso #{c['id']} vía copiloto", args["texto"][:200], usuario)
    return {"id": i, "guardado": True, "declarada_por": usuario, "alcance": args.get("alcance") or "caso",
            "nota": "Queda como declaración no verificada en el expediente; no modifica hechos ni reglas."}


def t_buscar(args, usuario, rol):
    cli = get_client()
    texto, tipo = args["texto"], args["tipo"]
    if tipo == "producto":
        rows = cli.search_read("product.product", ["|", ["name", "ilike", texto], ["default_code", "ilike", texto]],
                               ["id", "display_name", "default_code", "uom_id"], limite=15)
    elif tipo == "ubicacion":
        rows = cli.search_read("stock.location", [["complete_name", "ilike", texto], ["usage", "=", "internal"]],
                               ["id", "complete_name", "warehouse_id"], limite=15)
    elif tipo == "unidad_medica":
        campo = schema.campo("unidad_medica", "es_unidad") or "is_medical_unit"
        rows = cli.search_read("res.partner", [["name", "ilike", texto], [campo, "=", True]], ["id", "name"], limite=15)
    elif tipo == "medico":
        campo = schema.campo("unidad_medica", "es_medico") or "is_doctor"
        rows = cli.search_read("res.partner", [["name", "ilike", texto], [campo, "=", True]], ["id", "name"], limite=15)
    elif tipo == "empleado":
        rows = cli.search_read("hr.employee", [["name", "ilike", texto]], ["id", "name"], limite=15)
    else:
        rows = cli.search_read("res.partner", [["name", "ilike", texto], ["supplier_rank", ">", 0]], ["id", "name"], limite=15)
    return {"filas": rows}


def t_listar_casos(args, usuario, rol):
    return {"filas": [{k: c.get(k) for k in ("id", "tipo", "titulo", "severidad", "estado", "conclusion", "confianza", "impacto_mxn",
                                             "accion_recomendada", "responsable", "investigado_con", "creado_en")}
                      for c in db.casos(estado=args.get("estado"), limite=int(args.get("limite") or 30))]}


def t_ver_caso(args, usuario, rol):
    c = db.caso(int(args["id"]))
    return c or {"error": "Caso no encontrado"}


def t_investigar_hallazgo(args, usuario, rol):
    from ..main import api_investigar_hallazgo
    return api_investigar_hallazgo(int(args["id"]), {"usuario": usuario, "rol": rol})


def t_resolver_caso(args, usuario, rol):
    from ..main import api_resolver_caso, ResolverCaso
    return api_resolver_caso(int(args["id"]), ResolverCaso(estado=args["estado"], resolucion=args.get("resolucion", "")),
                             {"usuario": usuario, "rol": rol})


def t_estado_plataforma(args, usuario, rol):
    return {"odoo": get_client().probar(), "mapeo": {k: v.get("modelo") for k, v in schema.cargar()["entidades"].items()},
            "autonomia": autonomia.resumen(), "tokens": db.uso_periodo(),
            "corridas": [{k: c.get(k) for k in ("id", "agente", "estado", "inicio", "resumen")} for c in db.corridas(limite=6)]}


_REGISTRO = {
    "consultar_consumo": t_consultar_consumo, "consultar_existencias": t_consultar_existencias,
    "consultar_odoo": t_consultar_odoo, "listar_campos": t_listar_campos, "ejecutar_agente": t_ejecutar_agente,
    "ultimos_resultados": t_ultimos_resultados, "listar_hallazgos": t_listar_hallazgos,
    "clasificar_hallazgo": t_clasificar_hallazgo, "listar_resurtido": t_listar_resurtido,
    "generar_excel": t_generar_excel, "excel_desde_consulta": t_excel_desde_consulta,
    "proponer_accion": t_proponer_accion, "listar_acciones": t_listar_acciones,
    "consultar_facturacion": t_consultar_facturacion, "enviar_correo": t_enviar_correo,
    "aprobar_accion": t_aprobar_accion, "rechazar_accion": t_rechazar_accion,
    "registrar_aprendizaje": t_registrar_aprendizaje, "registrar_aclaracion": t_registrar_aclaracion, "buscar": t_buscar, "estado_plataforma": t_estado_plataforma,
    "listar_casos": t_listar_casos, "ver_caso": t_ver_caso, "investigar_hallazgo": t_investigar_hallazgo, "resolver_caso": t_resolver_caso,
}
