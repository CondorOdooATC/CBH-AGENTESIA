# Seguridad de los datos

Este documento describe qué datos maneja la plataforma, dónde viven, quién puede verlos y qué controles técnicos los protegen. Es la referencia para la validación en staging y para responder a Dirección, Contabilidad y al área de TI del cliente.

## 1. Qué datos se leen de Odoo y cuáles no

La plataforma lee de Odoo, con un usuario técnico de sólo el alcance necesario (`agente.ia@i-condor.com`): líneas de consumo de los folios de operación médica (fecha, hospital, sub-almacén, producto, cantidad, unidad, lote, médico, auxiliar, duración, pesos de báscula, importe), usuarios internos de Odoo (nombre, login, contacto y grupos, sólo para dirigir avisos), existencias y reservas por ubicación y lote, compras confirmadas y transferencias pendientes, proveedores y plazos, unidades de medida y folios programados (fecha, hospital, tipo de cirugía).

Desde v1.3.7 también lee, sólo a petición del copiloto y para reportes, las facturas de cliente y su cobranza (`account.move` y `account.move.line`: número, cliente, hospital/analítica, producto, importes, saldo pendiente y vencimiento). No lee asientos de nómina, contratos ni salarios.

No lee ni almacena la identidad ni los datos clínicos del paciente: los campos de paciente (nombre, NSS, fecha de nacimiento, sexo, edad, diagnóstico) están excluidos de todas las consultas de los agentes y de la memoria local, y el copiloto los veta por nombre de campo en cualquier modelo: no los devuelve aunque el usuario los pida, no permite filtrar por ellos y, si no se piden campos concretos, sólo devuelve el nombre del registro (nunca el registro completo). Lo mismo aplica a credenciales, RFC/CURP y datos bancarios. Tampoco lee contraseñas ni datos de nómina. El copiloto no puede consultar `res.users`, claves de API, parámetros de configuración, servidores de correo ni modelos de nómina (`hr.payslip`, `hr.contract`, `hr.salary`): esos modelos están vetados en la herramienta `consultar_odoo` (la plataforma sólo lee usuarios para resolver a quién avisar). Cualquier otro modelo se consulta con los permisos del usuario técnico, que es el tope real de lo que la plataforma puede ver.

## 2. Qué se envía al modelo de lenguaje (Claude)

Sólo cuando hay `ANTHROPIC_API_KEY`. Se envían resúmenes y tablas ya agregadas del motor (hallazgos, expedientes, plan de abasto, propuestas) y, durante una investigación o conversación, los registros concretos que las herramientas devuelven (líneas de un folio, historial de un auxiliar por producto, existencias de un lote). Todo pasa por `app/llm/claude.py` con un tope de tamaño por llamada y queda registrado en la bitácora de uso (tokens, costo, origen, usuario). Nunca se envían claves, contraseñas ni datos de pacientes. Anthropic no entrena con los datos de la API; el contrato de datos aplicable es el de la cuenta de API de Ingeniería Cóndor.

Sin API key la plataforma funciona completa en modo determinista y ningún dato sale del servicio.

## 3. Dónde viven los datos

Los datos de trabajo (hallazgos, casos, aclaraciones, propuestas, bitácora, aprendizaje, conversaciones, Excels generados) y, desde v1.3.10, una copia local de las líneas de consumo y cabeceras de folio leídas de Odoo (sin paciente; sirve para que cada corrida sólo pida a Odoo lo nuevo o lo que cambió) viven en el disco persistente del servicio en Render (`/data`), cifrado en reposo por Render, en una base SQLite de una sola instancia. Las credenciales (clave API de Odoo, API key de Anthropic, secreto SSO, clave de sesión) viven únicamente en las variables de entorno de Render; la interfaz nunca las muestra ni las edita. El respaldo completo de la base sólo puede descargarlo un usuario con rol `condor`.

El tráfico entre el navegador y la plataforma, entre la plataforma y Odoo, y entre la plataforma y Anthropic va siempre por HTTPS.

## 4. Quién puede ver y hacer qué

Roles de la plataforma: `consulta` (ve y conversa), `operacion` (aprueba, rechaza, resuelve casos, ejecuta agentes), `admin` (aprobación de riesgo alto, límites de seguridad, usuarios) y `condor` (exclusivo de Ingeniería Cóndor: configuración del modelo de lenguaje, alta de usuarios condor, respaldo). Los roles se aplican también dentro de las herramientas del copiloto y en la aprobación agrupada.

Con el addon de Odoo, los usuarios entran con su sesión de Odoo y su rol proviene de sus grupos de Odoo (Agentes de IA / Consulta, Operación, Administrador). El rol `condor` nunca se otorga ni se retira por esa vía. Las conversaciones son privadas de cada usuario: ni un administrador ni Cóndor pueden leer las de otra persona.

## 5. Controles técnicos

Autenticación: contraseñas con PBKDF2-SHA256 (120,000 iteraciones y sal por usuario), mínimo de 10 caracteres, comparación en tiempo constante; límite de intentos por usuario y por IP (8 en 15 minutos); sesiones de 12 horas con rotación del token al iniciar sesión; cookie `HttpOnly`, `Secure` y `SameSite`.

Acceso desde Odoo (SSO): token HMAC-SHA256 firmado con un secreto compartido, vigencia de 60 segundos, un solo uso, ligado a la base de Odoo configurada; los usuarios de Odoo no pueden entrar por el formulario con contraseña y un usuario de Odoo nunca suplanta una cuenta local.

Protección del navegador: cabeceras `Content-Security-Policy` (incluye `frame-ancestors` para permitir sólo a Odoo embeber la plataforma), `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `Strict-Transport-Security` y `Cache-Control: no-store` en la API; protección CSRF en toda petición que cambia estado (debe venir del mismo origen).

Aprobación desde el chat: el copiloto sólo puede aprobar una propuesta cuando el usuario lo confirma de forma explícita en la conversación («sí», «aprobada», «hazlo», «adelante»…); la herramienta exige esa frase, aplica el rol del usuario (consulta nunca aprueba), el candado, la revalidación contra Odoo y la regla de riesgo alto (un administrador; dos personas si el cliente activa esa política), exactamente igual que el botón Aprobar, y queda en la bitácora con la frase de confirmación.

Avisos a personas: los destinatarios se resuelven en Odoo (grupos del equipo y administradores de Agentes de IA, más logins configurados, o personas concretas que el usuario nombra en el chat); nunca se avisa al usuario técnico; el aviso crea actividades y una nota en el chatter, es idempotente por referencia y reversible.

Correos: la plataforma no tiene servidor de correo propio. Un correo pedido en el chat («mándale el reporte a …») es una acción que requiere aprobación, muestra antes destinatarios, asunto y adjunto, y al aprobarse se entrega a través del servidor de correo saliente de Odoo (`mail.mail`), de modo que queda en la bitácora de correo del cliente con su remitente y firma. Los adjuntos sólo pueden ser archivos generados por la propia plataforma (Excel de una consulta); nunca archivos arbitrarios del servidor.

Escrituras en Odoo: nada se escribe sin aprobación humana; riesgo alto exige a un administrador (dos personas distintas si la política `doble_aprobacion_riesgo_alto` está activa); las compras y transferencias que superan los límites de cantidad o importe se bloquean aunque se aprueben; cada acción lleva una referencia única (idempotencia), sin reintentos automáticos y con recuperación por referencia; toda aprobación, ejecución, reversión y cambio de configuración queda en la bitácora con usuario, fecha e IP de acceso.

Secretos en la bitácora: nunca se registran claves ni tokens; los errores de Odoo se recortan y no incluyen credenciales.

## 5b. Presupuesto de IA

El consumo de Claude se mide por mes en tokens de entrada y salida y en costo estimado. Ingeniería Cóndor fija el paquete mensual desde la plataforma (rol `condor`); el cliente lo ve pero no lo modifica. Al agotarse, la plataforma detiene los agentes y el chat y lo indica en todas las pantallas, de modo que nunca se consume más de lo contratado.

## 6. Retención y borrado

Las conversaciones y los Excels pueden borrarse desde la plataforma; la bitácora y el aprendizaje son permanentes por diseño (trazabilidad). A solicitud del cliente, Ingeniería Cóndor puede purgar datos por fecha o entidad con un script documentado (`scripts/purgar.py`, a solicitud). Al terminar el servicio, el disco de Render se elimina con el servicio.

## 7. Lo que el cliente debe hacer de su lado

Dar al usuario técnico sólo los grupos indicados; no compartir el secreto SSO fuera de Ajustes ▸ Agentes de IA; mantener actualizados los grupos de Odoo (quitar el grupo a quien deja el puesto invalida su acceso en su siguiente entrada); usar HTTPS y dominio propio en Render; y avisar a Cóndor para rotar claves (Odoo, Anthropic, SSO) al menos cada seis meses o ante cualquier sospecha.

## v1.3.11 · pantallas de administración y razonamiento del modelo

- **Configuración y Bitácora** (conexión a Odoo, mapeo de modelos, límites, presupuesto de IA, usuarios, eventos del sistema) son exclusivas de los roles Administrador y Cóndor: los roles Consulta y Operación no ven esos menús y las rutas y sus API responden 403. En el copiloto, `estado_plataforma` y el contexto de esas pantallas también requieren Administrador.
- **Razonamiento del modelo**: los bloques `thinking` que devuelve Claude 5 no se guardan en la conversación ni se reenvían entre turnos; la petición pide a la API que descarte cualquier bloque cuya firma no corresponda a la conversación (`drop_block`). Ningún dato adicional sale de la plataforma: la caché de prompts de Anthropic guarda temporalmente (5 minutos) el mismo contenido que ya se envía en cada llamada, bajo las mismas condiciones de no entrenamiento y retención de la API.
