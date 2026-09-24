# Validación en staging antes de operar en producción

Marcar cada punto con evidencia (captura, folio, fecha). Nada pasa a producción sin la lista completa.

## Datos
- [ ] `Configuración ▸ Probar conexión` responde con la compañía CBH+ y versión 19.
- [ ] `Re-descubrir modelos`: `consumo` y `folio` apuntan al módulo real de CBH (no a `stock.move.line`); campos `peso_inicial`, `peso_final`, `medico`, `auxiliar`, `duracion_min`, `lote` resueltos.
- [ ] Densidad y contenido por envase presentes en el producto (o capturados en Configuración) para todos los anestésicos volátiles.
- [ ] Unidad de compra (`uom_po_id`) configurada en los productos que se compran por frasco/caja.
- [ ] Proveedor principal con `delay` correcto (secuencia 1) en cada producto A.
- [ ] Cifras cruzadas: consumo del mes por hospital en la plataforma = consumo del mismo mes en Odoo (± 0.5 %).

## Agente de consumo
- [ ] Corrida con 180 días: hallazgos críticos revisados uno a uno con Operaciones; ≥ 80 % considerados pertinentes.
- [ ] 5 casos con expediente investigado por Claude: evidencia verificable en Odoo, hipótesis razonables, sin acusaciones.
- [ ] Justificar 3 hallazgos y confirmar 1: la siguiente corrida respeta lo justificado y los umbrales se movieron en el sentido correcto (bitácora `aprendizaje`).
- [ ] Discrepancias de báscula: tolerancia y precisión ajustadas con Calidad; lecturas inconsistentes explicadas (cambio de frasco, tara).

## Agente de abasto
- [ ] Plan con 30 días: existencia utilizable, en camino y por salir coinciden con Odoo para 5 productos A.
- [ ] Agenda en su fecha real: mover una jornada programada del día 1 al día 20 en CBHTEST desplaza la demanda proyectada y la fecha de quiebre; cancelarla la elimina sin residuos.
- [ ] La jornada extraordinaria se cuenta una sola vez: `ajuste_agenda` de la ubicación = exceso de la agenda sobre el pronóstico; el planificador no la vuelve a multiplicar (sus escenarios dicen «hipotético» y reportan faltante residual).
- [ ] Repetir la corrida sin cambios no duplica propuestas ni infla cantidades (las pendientes aparecen como «vigentes», sin versión nueva).
- [ ] Asignación conjunta: con 400 piezas en el CEDIS y dos hospitales que necesitan 300 y 250, las transferencias suman ≤ 400 menos la reserva operativa; la tarjeta muestra existencia, reserva, comprometido y «puede ceder».
- [ ] Regiones configuradas (Configuración ▸ Regiones) para que la reserva del CEDIS proteja a sus hospitales; sin configurar, se reparte por igual entre CEDIS.
- [ ] Compra sugerida en unidad de compra correcta (frascos, cajas, enteros) y con proveedor principal; la columna «¿Llega antes del quiebre?» es coherente con el lead time.
- [ ] Unidades y redondeo: título, tarjeta, cantidad aprobada y documento en Odoo coinciden (mL con un decimal; piezas enteras); un producto sin conversión de unidad de compra bloquea la propuesta con motivo claro.
- [ ] Ajustar una propuesta (chat del agente o planificador) recalcula cobertura antes/después, disponibilidad del origen, importe y riesgo, y crea versión nueva.
- [ ] Importes separados en tarjetas y Excel: sugerido por el motor, propuesto, aprobado y ejecutado.
- [ ] Escenario "retraso de 5 días" y ajustes del planificador revisados por Cadena de suministro.
- [ ] Tras 2 semanas: autoevaluación muestra error real por producto; los peores casos tienen causa conocida.

## Decisiones y Odoo
- [ ] Aprobar una transferencia → documento en borrador en CBHTEST con referencia `Agente IA · acción #N`; Revertir → cancelada.
- [ ] Aprobar una RFQ → borrador correcto (proveedor, cantidad, unidad); Revertir → cancelada.
- [ ] Regla min/max: modificar una existente y revertir → valores originales restaurados.
- [ ] Riesgo alto: primera aprobación por operación, segunda por un administrador distinto; misma persona rechazada.
- [ ] Aprobación agrupada de dos transferencias del mismo origen: la segunda se topa a lo que queda y pide nueva aprobación (no se comprometen dos veces las mismas piezas).
- [ ] Simular Odoo caído al aprobar (clave API inválida un momento): la acción queda en `requiere_revision` y no se escribe nada.
- [ ] Cuarentena de lote: en Odoo la transferencia queda confirmada y RESERVADA (la cantidad ya no está disponible); la tarjeta dice «bloqueado» sólo si la reserva fue completa.
- [ ] Validar recepción: con un picking en borrador o con cantidades incompletas la acción queda en `requiere_revision` con el motivo de Odoo (nunca se completa por suposición); con uno listo, termina en «done» y las existencias se mueven.
- [ ] En producción (APP_ENV=production) las acciones no validadas quedan «aprobada para ejecución manual» y no tocan Odoo.
- [ ] Fallo parcial: si Odoo creó el documento y falló el paso siguiente, la acción queda «ejecutada» ligada a ese documento con advertencia; no se duplica.
- [ ] Revalidación: bajar la existencia del origen en Odoo y aprobar → `requiere_revision` con cantidad actualizada.
- [ ] Doble clic simultáneo en Aprobar → un solo documento en Odoo.
- [ ] Seguimiento: validar la transferencia en Odoo → la acción pasa a `concluida` en la siguiente verificación.

## Casos y conversación
- [ ] Un caso no muestra «None», «nan» ni evidencia repetida; si el histórico previo es insuficiente lo dice y la confianza queda en baja.
- [ ] Severidad y confianza aparecen por separado y no se mueven juntas.
- [ ] Una explicación escrita en el chat del caso («el médico confirma…») queda como aclaración declarada con nombre, fecha y alcance; no cambia los hechos ni crea reglas globales.
- [ ] El botón «Hablar con los agentes» responde con el contexto de cada pantalla (Hoy, Decisiones, Casos, Excel, Configuración, Bitácora).

## Acceso desde Odoo y seguridad
- [ ] Un usuario con grupo *Operación* entra desde el menú de Odoo sin contraseña y aparece con ese rol; al quitarle el grupo, su siguiente entrada es rechazada.
- [ ] Un usuario sin grupo no ve el menú; un usuario de Odoo no puede entrar por `/login` con contraseña.
- [ ] Un administrador de Odoo que se da a sí mismo el grupo *Administrador* no obtiene el rol `condor` (no puede tocar el modelo de lenguaje).
- [ ] Reutilizar un enlace de acceso o usar uno de otra base es rechazado con mensaje claro.
- [ ] Cabeceras de seguridad presentes (`Content-Security-Policy`, `Strict-Transport-Security`, `X-Content-Type-Options`), respaldo sólo con `condor`, bloqueo tras intentos fallidos.
- [ ] `docs/SEGURIDAD.md` revisado con TI del cliente: datos leídos, datos enviados a Claude (sin pacientes), retención.

## Plataforma
- [ ] Usuarios creados con roles; un usuario `consulta` no puede aprobar ni ejecutar (ni desde el copiloto).
- [ ] Un usuario `admin` del cliente **no** puede cambiar el modelo de lenguaje ni crear usuarios `condor` (sólo `condor`).
- [ ] Chat contextual: preguntar al Agente de Abasto por una propuesta concreta y al Investigador por un caso; las respuestas citan datos de la corrida.
- [ ] Ajustar una propuesta (planificador o copiloto) → versión nueva, riesgo recalculado, aprobación parcial anulada; una transferencia no excede lo utilizable en el origen.
- [ ] RFQ creada en la **unidad de compra** (frascos/cajas, enteros) y no en mL/pz.
- [ ] Proyección día por día: una entrega que llega mañana no muestra quiebre hoy; una que llega en 25 días sí lo muestra.
- [ ] Compra confirmada con recepción pendiente: se cuenta una sola vez en «en camino».
- [ ] Vigilancia corre cada 3 h y no duplica alertas.
- [ ] Presupuesto de tokens y costo por modelo visibles en Bitácora; tarifas verificadas en la consola de Anthropic.
- [ ] Respaldo descargado y restaurado en local (`DATA_DIR` con el archivo).
- [ ] UptimeRobot alertando en `/health`.

## v1.3.1 · lo que se pide se hace, avisos a personas, modo de respaldo
- [ ] Configuración ▸ Mapeo: la entidad `consumo` apunta al modelo real de folios de CBH (no a `stock.move.line`) y no aparece el aviso «Modo de respaldo». Si aparece: *Buscar el modelo de folios* → *Ajuste manual* (entidad `consumo` y `folio`) → *Re-descubrir*.
- [ ] En el copiloto: «crea una orden de compra de N frascos de <producto real>» → el copiloto propone (efecto con cantidad en frascos y en unidad base) y pregunta; al responder «sí, apruébala» la RFQ existe en Odoo (Compras ▸ Solicitudes de cotización, origen `Agente IA · acción #…`) con la cantidad en la **unidad de compra**. Si el producto no tiene proveedor, lo dice y no inventa uno.
- [ ] «manda N piezas de <producto> de <CEDIS> a <hospital>» → transferencia interna creada (borrador en nivel 1, confirmada y reservada en nivel 2); pedir más de lo disponible no se propone.
- [ ] Sin confirmación explícita el copiloto no aprueba; un usuario `consulta` no puede aprobar desde el chat; riesgo alto exige la segunda aprobación de un administrador (también desde el chat).
- [ ] Configuración ▸ Avisos ▸ «¿Quién recibiría cada aviso hoy?» muestra personas reales de Odoo (Contabilidad/Facturación, Inventario, administradores de Agentes de IA) y nunca al usuario técnico.
- [ ] Aprobar un «Avisar a Contabilidad / Facturación · Caso #…» crea una actividad «por hacer» a cada persona (visible en sus actividades de Odoo) y una nota en el chatter del folio que las notifica; «Verificar ejecutadas» reporta `k/n atendidas`; «Revertir» retira las que siguen vivas.
- [ ] Ninguna pantalla ni sugerencia menciona hospitales, productos o proveedores que no existan en Odoo del cliente.
- [ ] Bitácora sin errores `temperature`, `Object doesn't exist` ni `Invalid field 'product_uom'`.

## v1.3.2 · módulo real de CBH e IA en todo
- [ ] Configuración ▸ Mapeo: `folio` = `cbh.medical.service.request`, `consumo` = `cbh.medical.service.request.line`, confianza *descubierta*; sin aviso amarillo.
- [ ] Agente · Consumo muestra hospitales reales (Hospital / Unidad Médica), médico (anestesiólogo), técnico, quirófano y sub-almacén del ticket; las líneas con pesaje traen peso inicial/final y consumo en mL del módulo.
- [ ] Un CB Ticket cancelado no aparece en el consumo; el paciente no aparece en ninguna pantalla, Excel ni bitácora.
- [ ] Hoy ▸ Vigilancia muestra la «lectura» redactada por la IA; los expedientes dicen «investigado por Claude»; el plan dice «razonado por Claude».
- [ ] Si la IA falla, aparece el aviso rojo con el error y el origen; no hay contenido determinista sin aviso.
- [ ] Corrida del Agente · Consumo con 3 expedientes en paralelo: menos de 2 minutos en el plan Standard.

## v1.3.7 · copiloto completo y configuración intuitiva
- [ ] Configuración muestra la **Lista de preparación** con todos los puntos en verde salvo «Entorno» (ámbar en staging) y, si aplica, «Acceso desde Odoo».
- [ ] Configuración ▸ Límites de seguridad: «Importe máximo por acción» es mayor que «Riesgo alto a partir de» (en staging estaba en 1,500 MXN: subirlo, p. ej. a 150,000, o ninguna compra mayor se creará).
- [ ] Copiloto: «¿cuánto facturamos a cada hospital este mes?» y «¿qué facturas están vencidas?» responden con tabla real de Odoo (Contabilidad); «dame un Excel de las facturas vencidas» entrega el enlace.
- [ ] Copiloto: «avísale a <nombre real de un usuario de Odoo> que revise el folio <folio real>» → propuesta de aviso con la persona; al aprobar, la persona tiene la actividad en Odoo.
- [ ] Copiloto: «mándale por correo a <correo> el Excel de existencias del CEDIS» → propuesta de correo (destinatario, asunto, adjunto); al aprobar, el correo aparece en Odoo ▸ Ajustes técnicos ▸ Correos y llega al destinatario (requiere servidor de correo saliente configurado en Odoo).
- [ ] Copiloto: «aprobado» tras una propuesta la ejecuta sin pedir otra confirmación; una RFQ de un producto sin proveedor se crea igual (proveedor en blanco o «por definir») y el copiloto lo dice.
- [ ] Copiloto: «¿qué auxiliar tiene menor rendimiento?» devuelve una tabla ordenada con cifras (folios, consumo por folio, hallazgos) y una sola línea de matiz.
- [ ] Casos: las tarjetas muestran conclusión y qué hacer; «Ver más» despliega qué pasó, hipótesis y conclusión completa.
- [ ] Agente · Abasto ▸ Anticipación: con la IA activa dice «razonado por la IA»; si dice «sin IA en esta corrida», el aviso rojo de arriba explica el motivo (saldo o presupuesto) y hay que volver a ejecutar.

## v1.3.10 · lecturas incrementales
- [ ] Primera corrida tras desplegar: el progreso muestra «Líneas de consumo leídas … (lectura completa; las siguientes serán incrementales)» y Configuración ▸ Conexión a Odoo ▸ Memoria local del consumo muestra líneas, folios y MB.
- [ ] Segunda corrida (mismo día): el progreso dice «Líneas de consumo al día … releídas N (…)» con N pequeño, y la lectura tarda una fracción de la primera. Los hallazgos, casos y el plan de abasto son los mismos que en la primera corrida.
- [ ] Modificar la cantidad de una línea de un CB Ticket en Odoo y volver a correr: la Bitácora dice «releídas 1» y el consumo refleja la cantidad nueva.
- [ ] Cancelar un CB Ticket y volver a correr: sus líneas desaparecen del consumo.
- [ ] «Releer consumo completo» borra la memoria y la siguiente corrida vuelve a ser completa.
