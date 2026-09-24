# CBH · Agentes de IA — v1.3.6 (estable para staging)

Plataforma de **agentes de inteligencia artificial** para **Grupo CB (CBH+)** sobre Odoo 19, desarrollada por **Ingeniería Cóndor**. No es un módulo de estadística: el motor numérico es sólo la capa de evidencia; el razonamiento, la anticipación y la conversación los hacen agentes con herramientas sobre Odoo, y **nada se ejecuta en Odoo sin la aprobación de una persona**.

## Los agentes

| Agente | Qué hace | Cómo razona |
|---|---|---|
| **Control de consumo** | Construye perfiles por producto/unidad/médico/auxiliar/sub-almacén, detecta diferencias no conciliadas (básculas, envases, lotes, horarios, duplicados, patrones sostenidos) y cuantifica el importe **sujeto a revisión / confirmado / aclarado**. | **Investigador** (Claude con herramientas): abre el folio, compara al actor con sus pares, concilia gramos↔mL con densidad, localiza el lote, revisa el contexto del día y los casos resueltos, y entrega un **expediente** que separa **hechos**, **hipótesis por verificar** (ordenadas por plausibilidad, con evidencia a favor/en contra y cómo verificarlas) y **datos faltantes**. Nunca inventa porcentajes. |
| **Abasto y demanda** | Pronostica por producto × ubicación (backtesting de 6 métodos, WAPE semanal, sesgo, confianza), proyecta el saldo **día por día** con existencia utilizable, entradas fechadas (compras por recibir y transferencias internas, cada una contada una sola vez y en unidad base) y salidas, demanda **comprometida** por folios programados, stock de seguridad con variabilidad del plazo, unidad de **compra** (mL→frascos), ABC/XYZ, FEFO y rebalanceo regional. | **Planificador** (Claude con herramientas): revisa su **precisión pasada**, folios programados y entregas retrasadas; **simula escenarios** concretos (retrasar *esta* entrega o *este* proveedor, jornada extraordinaria en *este* hospital); **ajusta o agrega propuestas** —cada ajuste recalcula riesgo y aprobaciones, genera una versión nueva y nunca transfiere más de lo que el origen tiene— y explica *lo que el pronóstico no ve*. |
| **Vigilancia continua** | Cada 3 h: consumos sin cirugía en 24 h, existencias que cayeron bajo el punto de reorden, entregas que se volvieron tardías (propone recordatorio al proveedor), jornadas extraordinarias próximas, propuestas sin decisión. | Alertas deduplicadas a la cola de decisiones. |
| **Copiloto** | Chat con 20 herramientas: Odoo en vivo, agentes, expedientes, Excels a la medida, propuestas y aprendizaje. | Claude con bucle de herramientas y control de roles. |

**Lo que se pide, se hace (v1.3.1).** «Crea una orden de compra de 10 frascos de X», «manda 20 piezas de Y del CEDIS al hospital Z»: el copiloto propone la acción con su efecto exacto (documento, cantidad en la unidad de compra o base, origen/destino), pide confirmación explícita y, con ella, la **aprueba y ejecuta en Odoo** por el mismo camino que el botón Aprobar (rol, candado, revalidación, doble aprobación en riesgo alto, idempotencia), devolviendo la referencia real de Odoo. Compatible con Odoo 19 (`product_uom_id` en líneas de compra).

**Avisos a personas, no tickets (v1.3.1).** El cliente no usa Helpdesk: cuando un caso necesita seguimiento humano (retener un folio de la facturación al IMSS, inspeccionar un lote, verificar en sitio), el agente propone **avisar a personas concretas de Odoo** —los usuarios de los grupos de Odoo del equipo (Contabilidad/Facturación, Inventario) más los administradores de Agentes de IA, y los logins fijados en Configuración ▸ Avisos— creando una actividad «por hacer» con plazo para cada una y una nota en el chatter que las notifica (bandeja de Odoo y correo). Se puede retirar; el seguimiento cuenta cuántas la atendieron.

**RFQ aunque el producto no tenga proveedor (v1.3.6).** Si el producto no tiene proveedor configurado, la solicitud de cotización se crea de todos modos: con el proveedor en blanco cuando la base lo permite y, si Odoo lo exige (comportamiento estándar), con el contacto «PROVEEDOR POR DEFINIR» que la plataforma crea una sola vez; la tarjeta lo avisa desde la propuesta y el chatter de la RFQ pide asignar el proveedor real antes de confirmar. El nombre del contacto se cambia en Políticas.

**Presupuesto mensual de IA con tope duro (v1.3.5).** Ingeniería Cóndor fija el paquete de tokens del mes (Configuración ▸ Presupuesto de IA; el cliente lo ve, no lo edita). Al acercarse al umbral (80 % por defecto) todas las pantallas avisan; al agotarse, los agentes no corren, el chat responde que hay que comprar más tokens y el aviso rojo aparece en toda la plataforma hasta ampliar el paquete. El contador se reinicia cada mes y el costo estimado en USD se muestra junto al consumo.

**Odoo 19 de punta a punta y corridas medibles (v1.3.3).** Unidades de medida al estilo Odoo 19 (sin `uom_po_id`: la unidad de compra sale del proveedor principal y las unidades relativas se reconstruyen), RFQ en frascos/cajas igual que antes; riesgo alto con **una sola aprobación de administrador** (política configurable); cada corrida registra en la bitácora cuánto tardó cada paso (lectura de Odoo, motor, investigación, plan, Excel) y lo muestra en pantalla; el backtesting completo se reserva a las combinaciones más activas; una sola lectura de consumo cuando los agentes corren seguidos; el copiloto responde rankings operativos (técnicos, unidades por artículo, qué reabastecer) con datos.

**Lee el módulo real de CBH (v1.3.2).** El mapeo conoce `cbh_operaciones_medicas` (Odoo 19): el CB Ticket (`cbh.medical.service.request`) y sus consumos (`cbh.medical.service.request.line`). La línea no tiene fecha ni actores: la plataforma los trae de la cabecera (fecha de cirugía + hora de inicio de anestesia, anestesiólogo, técnico, quirófano, sub-almacén de surtido, tipo de evento, duración), toma el lote del movimiento de consumo real, usa la variante que se movió en inventario, entiende el pesaje del módulo (1 g = 1 mL, `consumed_qty_ml`, 0/0 = sin pesaje) y valora a costo estándar. Los folios cancelados no entran y el paciente nunca se lee. Si el módulo tuviera otro nombre técnico, la **detección automática** lo encuentra por la forma de sus modelos (producto + cantidad + lote + báscula; cabecera con hospital, médico y líneas) sin configurar nada a mano.

**Todo impulsado por IA (v1.3.2).** Informes, expedientes (en paralelo), plan razonado con escenarios, lectura de la vigilancia, briefing y chat los hace Claude; los motores numéricos sólo aportan evidencia. Si una llamada a la IA falla, la plataforma lo avisa en pantalla en vez de entregar en silencio contenido determinista. Los límites por corrida (expedientes, escenarios, llamadas) se ajustan en Configuración y sólo acotan el tiempo.

**Modo de respaldo explícito (v1.3.1).** Si no se detecta el módulo de folios de CBH, la plataforma trabaja sobre movimientos de inventario (`stock.move.line`) y lo dice en todas las pantallas: desactiva las reglas *sin cirugía* y *duplicado* y los avisos de facturación (no hay folio que facturar), valora el consumo a costo estándar del producto y explica cómo se calcula el impacto. Configuración ▸ *Buscar el modelo de folios* localiza el modelo real en Odoo y el *Ajuste manual* lo fija. Ningún nombre de hospital, producto o proveedor de ejemplo aparece en la interfaz ni en los prompts: sólo datos reales de Odoo.

**Conversación en lenguaje natural en todas las pantallas.** El botón **💬 Hablar con los agentes** está en toda la plataforma y conoce lo que se está viendo: en *Agente · Consumo* y *Agente · Abasto* se pregunta sobre la corrida actual («¿por qué propones esa transferencia?», «¿qué pasa si el proveedor se retrasa 5 días?»); en cada **caso** se conversa con el Investigador sobre ese expediente; en **Decisiones** explica cada propuesta con cifras y el orden en que conviene decidir; en Hoy, Casos, Excel, Configuración y Bitácora responde sobre esa pantalla; y el **Copiloto** responde sobre cualquier dato de Odoo. Las conversaciones son privadas por usuario y por contexto. Una explicación aportada por una persona («el médico confirma…») queda registrada como **aclaración declarada** (autor, fecha, alcance), nunca como hecho verificado ni regla global.

**Coherencia de principio a fin (v1.3).** La agenda quirúrgica entra en su fecha real como piso diario del pronóstico (sin sumarse ni multiplicarse dos veces); el saldo se proyecta al cierre de cada día con entradas y salidas fechadas y los eventos fuera de la ventana no se cuentan; la cobertura son días hasta agotar y cada necesidad tiene su **fecha necesaria de abastecimiento**; las transferencias se asignan **conjuntamente** (lo que otras propuestas ya apartaron, la reserva operativa del CEDIS para sus hospitales); las cantidades respetan la unidad y su precisión en título, tarjeta, aprobación, Odoo y Excel; cada ajuste recalcula cobertura, disponibilidad, importe y riesgo; y los importes sugerido, propuesto, aprobado y ejecutado se informan por separado.

**Instalación del lado de Odoo.** El addon `odoo_addon/cbh_agentes_ia` (Odoo 19) se sube al repositorio de Odoo.sh como cualquier módulo: menú **Agentes de IA**, grupos Consulta/Operación/Administrador, acceso sin segundo login (token firmado de 60 s) y la pantalla de Ajustes con los datos listos para Render. El servicio sigue en Render.

Sin API key de Claude todo sigue funcionando en **modo determinista** (misma evidencia, hipótesis típicas por regla, reglas de anticipación, respuestas guiadas) y la interfaz lo indica.

## Ciclo completo

detectar → **investigar** → proponer (con efecto exacto, antes/después y versión) → **aprobar** (dos personas para riesgo alto) → **revalidar** contra Odoo hoy → ejecutar (idempotente, sólo la versión aprobada) → **verificar** en Odoo hasta concluir/retrasarse → **aprender** (perfiles, umbrales, casos resueltos, rechazos, notas).

## Lo que los agentes pueden hacer en Odoo (siempre con aprobación)

Transferencia interna · solicitud de cotización (en la unidad de compra, unidades enteras; sin proveedor configurado se crea igual con el proveedor en blanco o «por definir») · regla mín/máx · actividad · nota en el chatter · alerta interna · **aviso a personas** (actividad + notificación a grupos de Odoo o a personas concretas; ticket de Helpdesk sólo si la política `usar_helpdesk` está activa) · **correo** por el servidor de correo de Odoo, con Excel adjunto si se pide · **cuarentena de lote** · desecho de lote · **reprogramar compra** · recordatorio al proveedor · confirmar transferencia · **validar recepción** (riesgo alto) · ajustar plazo del proveedor · solicitar conteo físico. Cada tipo tiene su efecto explicado en la tarjeta, su nivel de riesgo, su reversión (cuando aplica: las reglas recuperan sus valores anteriores, las transferencias y RFQ se cancelan) y su verificación posterior en Odoo.

## Confiabilidad

- Niveles de autonomía 0–2; **no existe modo autónomo**. Nada se escribe en Odoo sin aprobación.
- Riesgo alto: lo aprueba **un administrador** (o Cóndor). Si el cliente lo prefiere, la política `doble_aprobacion_riesgo_alto` exige dos personas distintas.
- Toda modificación de una propuesta crea una **versión** y anula aprobaciones parciales; se ejecuta sólo la versión aprobada.
- Al aprobar se **revalida**: política vigente, vigencia, existencia utilizable en el origen, compras recientes duplicadas. Cambio material → `requiere_revision`.
- Transiciones de estado **atómicas**; escrituras a Odoo **sin reintentos** y con **recuperación por referencia**: una respuesta perdida no genera documentos duplicados.
- Roles: `consulta` < `operacion` < `admin` < **`condor`**. La configuración del modelo de lenguaje (modelo, tokens, temperatura) sólo la modifica el equipo de Ingeniería Cóndor; la API key vive únicamente en las variables de entorno de Render.
- Antes de escribir en Odoo se **revalida** de nuevo (política, vigencia, disponibilidad con el libro de compromisos); si la información indispensable no se puede consultar, no se ejecuta. Un fallo parcial liga el documento creado por referencia (sin duplicar) y lo marca con advertencia. Odoo pide una decisión (entrega parcial, lotes) → `requiere_revision`, nunca se completa por suposición.
- En producción, las acciones aún no validadas (validar recepción, confirmar transferencia, cuarentena, desecho, reprogramar compra, plazo de proveedor) quedan **aprobadas para ejecución manual**; en staging sí se ejecutan para validarlas. La cuarentena bloquea de verdad: confirma y **reserva** el lote y reporta cuánto quedó bloqueado.
- Casos de aceptación automatizados (`tests/test_aceptacion.py`, `tests/test_estabilidad.py`, `tests/test_sso_seguridad.py`): agenda en su fecha real; jornada contada una sola vez y sin duplicar propuestas; asignación conjunta (400 piezas, 300 + 250 → nunca 550) y aprobaciones en serie; eventos fuera de ventana; unidades y redondeo de punta a punta; impacto recalculado tras ajustar; revalidación antes de ejecutar y detención si Odoo no responde; producción sin ejecutar acciones no validadas; cuarentena real; validar recepción con asistente; fallo parcial sin duplicados; Excel completo o con recorte explícito; privacidad estricta; aclaraciones trazables; SSO (firma, caducidad, base, repetición, rol tope, sin suplantación) y CSRF.

## Aprendizaje (sin datasets externos)

1. Cada corrida reconstruye perfiles y re-selecciona métodos de pronóstico con el histórico real de Odoo.
2. Justificar un hallazgo **relaja** el umbral de ese perfil; confirmarlo lo **endurece**; repetir la misma clasificación no lo mueve; todo queda trazado.
3. Rechazos con motivo, resoluciones de casos y notas del copiloto son memoria permanente que los agentes leen.
4. El planificador se **autoevalúa**: compara lo que pronosticó contra lo que ocurrió y lo dice.

## Estructura

```
app/agents/   consumo · demanda · investigador · planificador · vigilancia · autonomia · briefing · copiloto
app/ml/       anomalias.py · pronostico.py             (evidencia determinista, proyección día por día)
app/odoo/     client · schema (auto-descubrimiento) · queries · acciones (15 tipos) · simulado (gemelo para demo/pruebas)
app/llm/      claude (bucle de herramientas) · prompts · tools (copiloto)
app/reports/  Excel con identidad Odoo (portada, "Acerca de", totales, fechas reales, hasta 1M de filas)
app/web/      Hoy · agentes (con chat) · casos (con chat) · decisiones agrupadas · copiloto · configuración · bitácora
odoo_addon/   cbh_agentes_ia (Odoo 19): menú, grupos, SSO, ajustes con datos para Render
tests/        79 pruebas (plataforma, confiabilidad, agentes con Claude simulado, aceptación, estabilidad, SSO y seguridad, modo de respaldo, avisos, acciones desde el chat)
docs/         DESPLIEGUE.md · ARQUITECTURA.md · VALIDACION_STAGING.md · SEGURIDAD.md
```

## Correr en local (demo sin Odoo)

```bash
pip install -r requirements.txt
./scripts/demo_local.sh          # http://localhost:8000 · admin / demo1234
python -m pytest tests -q
```

## Desplegar

`docs/DESPLIEGUE.md` (addon en Odoo.sh → servicio en Render, staging → producción), `docs/VALIDACION_STAGING.md` (lo que debe pasar antes de operar en producción) y `docs/SEGURIDAD.md` (qué datos se leen, qué va a Claude, roles, controles).

## v1.3.7 · copiloto completo y configuración intuitiva

- **Copiloto**: además de consumo, existencias y abasto, consulta **facturación y cobranza** (`consultar_facturacion`: ventas por hospital/producto/mes, cartera vencida, facturas por cobrar), **cualquier modelo de Odoo** con agrupación y sumas (`consultar_odoo`, salvo usuarios, credenciales, nómina y correo), genera **Excel de cualquiera de esas fuentes** (`excel_desde_consulta`), **avisa a personas concretas** en Odoo y **envía correos** con el Excel adjunto (`enviar_correo`, vía `mail.mail` de Odoo). Todo lo que escribe o envía pasa por aprobación; «aprobado», «hazlo», «adelante» cuentan como confirmación y no se pide una segunda. Si una propuesta falló por un dato faltante (proveedor, ubicación), el copiloto crea la corregida y retira la errónea. Las preguntas de ranking («¿qué auxiliar tiene menor rendimiento?», «¿qué unidad consume más X?») se contestan con tabla y cifra.
- **Configuración**: empieza con una **lista de preparación** (Odoo, módulo de folios, IA, presupuesto, acceso desde Odoo, autonomía y límites, avisos, corridas automáticas, entorno) con semáforo y qué hacer en cada caso; cada campo tiene nombre en lenguaje llano y una explicación de qué pasa si se cambia; los ajustes estadísticos quedan plegados en «Ajustes avanzados». Avisa si el importe máximo por acción es menor que el umbral de riesgo alto (bloquearía todas las acciones grandes).
- **Casos** resumidos (conclusión y qué hacer) con «Ver más» para el expediente completo; **Copiloto** con más espacio de lectura e ideas de preguntas plegables; **Anticipación** del agente de Abasto explica qué es el plan base, los ajustes, los riesgos y los escenarios, y avisa cuando la IA no estuvo disponible en la corrida.
- Lista de paso a producción en `docs/PRODUCCION.md`.

## v1.3.8 · versión de paso a producción

- Sin cambios funcionales sobre v1.3.7. `render.yaml` fija la instancia de producción en `standard` (1 CPU / 2 GB) y `docs/PRODUCCION.md` / `docs/DESPLIEGUE.md` documentan el plan de Render (workspace Pro + instancia Standard) y la cuenta de Anthropic con límite de gasto. Es la versión que se valida en `staging` y se promueve tal cual a `main`.

## v1.3.9 · Opus 5.5

- Modelo principal por defecto `claude-opus-5-5` (USD 4 / 20 por millón de tokens, 20 % más barato que Opus 5) y su tarifa en la tabla de costos; la búsqueda de tarifa usa el prefijo más largo para no cobrar Opus 5.5 con la tarifa de Opus 5.

## v1.3.10 · lecturas de Odoo incrementales y en paralelo, mismo resultado

- **Memoria local del consumo** (`app/odoo/memoria_consumo.py`): la primera corrida baja todo el consumo de Odoo y lo guarda en el disco persistente; las siguientes sólo piden a Odoo los ids vigentes (ventana, folios no cancelados) y releen únicamente las líneas, folios y transferencias **nuevas o cuyo `write_date` cambió**. El resultado es idéntico al de una lectura completa (prueba `tests/test_consumo_incremental.py`); lo que cambia es que una corrida diaria baja un día de datos en vez de 180 o 540. `CONSUMO_INCREMENTAL=false` la desactiva; Configuración ▸ **Releer consumo completo** la borra (por ejemplo al cambiar de base). Si el mapeo de campos cambia, se relee todo solo.
- Las lecturas grandes ya no paginan con *offset* (Odoo repetía la búsqueda completa en cada página): una sola búsqueda de ids y lecturas por bloques con varias conexiones a la vez (`ODOO_LECTURAS_PARALELAS`, por defecto 4; `ODOO_BLOQUE_LECTURA`, 2000). Mismo resultado y orden.
- El progreso de la corrida muestra cada fase de la lectura (líneas · cabeceras · lotes) y la Bitácora registra el desglose y cuánto se releyó.
- `render.yaml`: `CONSUMO_CACHE_SEG=3600` en producción para que Abasto reutilice la lectura de Consumo de la misma mañana.
- **Privacidad reforzada en el copiloto**: `consultar_odoo` veta por nombre de campo los datos de pacientes (nombre, NSS, nacimiento, sexo, diagnóstico), credenciales, RFC/CURP y datos bancarios en cualquier modelo; sin campos explícitos devuelve sólo `display_name`; no permite filtrar ni agrupar por esos campos; vetados además `res.partner.bank`, `ir.attachment`, `mail.message`. Las lecturas de los agentes filtran cualquier campo de paciente aunque el mapeo lo trajera (`queries.sin_paciente`). Prueba: `tests/test_privacidad_copiloto.py`.

## v1.3.11 · Claude 5 en producción: razonamiento firmado, respuesta garantizada y menos tokens

- **Causa del «HTTP 400 · Invalid signature in thinking block» y del «(sin respuesta)»**: Opus 5.5 y Sonnet 5 razonan siempre antes de responder y devuelven ese razonamiento como bloques `thinking` firmados y ligados al *system* y a la conversación con que se generaron. La plataforma los guardaba con la conversación y los reenviaba en el siguiente turno con otro *system* (fecha, notas de aprendizaje) → la API los rechazaba (las cuentas de Anthropic creadas desde el 31-ago-2026 aplican esa vinculación estricta; la de staging no). Además el razonamiento cuenta dentro de `max_tokens` (8,000): con una respuesta larga se quedaba sin espacio y llegaba vacía.
- Corrección (`app/llm/claude.py`, `app/agents/copiloto.py`): los bloques de razonamiento no se reenvían entre turnos (`sin_razonamiento`), se pide razonamiento adaptativo con `block_binding.prefix_mismatch_behavior=drop_block` (cabecera beta `thinking-binding-controls-2026-08-01`) como segunda barrera, `ANTHROPIC_MAX_TOKENS` sube a 16,000 y si la respuesta se corta a media herramienta se reintenta con el doble (hasta `ANTHROPIC_MAX_TOKENS_TOPE`, 32,000); si termina sin texto se le pide redactar y, en último caso, la plataforma explica qué pasó en lugar de mostrar «(sin respuesta)». Si la API de una cuenta no acepta alguna configuración opcional (razonamiento, esfuerzo, caché), se reintenta sin esa parte y se anota en la Bitácora.
- **Menos tokens y menos tiempo, mismo funcionamiento**:
  - *Caché de prompts* (`ANTHROPIC_CACHE`, activa): herramientas, parte estable del *system* e historial se marcan para caché; en los bucles de herramientas (copiloto, expedientes, planificador) cada llamada relee lo anterior al 5–10 % del precio. El presupuesto sigue midiendo dinero: `tokens_entrada` guarda la entrada **equivalente** (sin caché + escritura × 1.25 + lectura × 0.1; Opus 5.5 lectura × 0.05) y Configuración muestra cuánto vino de la caché y cuánto fue razonamiento.
  - *Esfuerzo del modelo* por origen (`ANTHROPIC_EFFORT=low` para copiloto, expedientes, vigilancia y briefing; `ANTHROPIC_EFFORT_INFORMES=medium` para los informes de Consumo y Abasto y el plan razonado). El razonamiento se cobra como salida: «low» lo reduce a lo necesario y acelera las respuestas.
  - *Historial del copiloto* recortado a 12 intercambios, sin razonamientos, y con los resultados de herramientas de turnos viejos resumidos en una línea (la respuesta redactada se conserva íntegra).
  - *Resultados de herramientas* en JSON compacto (sin espacios, sin claves vacías, decimales acotados), tope 24,000 caracteres.
  - El copiloto genera los Excel de consultas con `excel_desde_consulta` (todas las filas, sin pasarlas por el chat); `generar_excel` queda para tablas pequeñas que él redacta.
- **Configuración y Bitácora sólo para administradores**: los roles Consulta y Operación no ven esos menús ni pueden abrir `/configuracion`, `/bitacora`, `Probar conexión`, `Re-descubrir` ni la memoria de consumo (403); la herramienta `estado_plataforma` del copiloto y el contexto de esas pantallas también son de administrador.
- Simulador: genera datos hasta ayer (los folios de «hoy» entraban o no en las lecturas según la hora), y las pruebas de ranking toleran el orden entre los tres primeros. Pruebas nuevas: `tests/test_llm_eficiencia.py`.
