# Paso a producción — lista de verificación

Qué hay que hacer, en orden, para que la plataforma opere contra **CBH1** (producción de Odoo.sh). Todo lo que está aquí se hace una sola vez; el detalle de cada paso está en `DESPLIEGUE.md`.

## 1. Antes de tocar producción (en staging)

- [ ] `VALIDACION_STAGING.md` completa, incluidas las secciones v1.3.1 a v1.3.7, con datos reales de CBHTEST.
- [ ] Configuración ▸ Lista de preparación: todo en verde salvo «Entorno».
- [ ] Al menos una RFQ, una transferencia, un aviso a personas y un correo creados desde el copiloto y verificados en Odoo.
- [ ] Un mes (o el periodo acordado) de corridas automáticas sin errores en la Bitácora.

## 2. Credenciales nuevas (obligatorio)

Las claves usadas en staging se compartieron durante las pruebas y **no deben reutilizarse**.

- [ ] En Odoo **CBH1**: Ajustes ▸ Agentes de IA ▸ **Generar clave API** (usuario técnico `agente.ia@i-condor.com` de producción) y **nuevo secreto SSO**. Copiar el bloque «Datos para Render».
- [ ] En la consola de Anthropic: **API key nueva** para producción (workspace propio del cliente, con límite de gasto mensual igual al paquete contratado) y **revocar** la que se usó en staging. Verificar que la cuenta tenga saldo: si se agota, la plataforma muestra el aviso rojo «La IA falló… credit balance is too low» y los agentes trabajan sin razonamiento.
- [ ] `SECRET_KEY` de sesión nueva (Render la genera con `generateValue: true`).
- [ ] Rotar las tres claves (Odoo, Anthropic, SSO) al menos cada seis meses o ante cualquier sospecha (`SEGURIDAD.md` §7).

## 3. Servicio de producción en Render (`cbh-agentes-ia`)

- [ ] Workspace de Render en plan **Pro** (≈ $25 USD/mes: varios miembros de Cóndor con su propia cuenta, 25 GB de tráfico) e instancia del servicio **Standard · 1 CPU / 2 GB** (≈ $25 USD/mes; en `render.yaml` ya viene `plan: standard`). Free/Starter se duermen o se quedan sin memoria en las corridas. Si las métricas de Render muestran la memoria al tope, subir a Pro (2 CPU / 4 GB, ≈ $85).
- [ ] Cuenta de Anthropic (console.anthropic.com) con saldo prepagado, **límite de gasto mensual** fijado y un workspace propio para CBH con su API key. Costo esperado de IA: ≈ $60–150 USD/mes según uso del copiloto (Configuración ▸ Presupuesto muestra el costo real del mes).
- [ ] Disco persistente montado en `/data` (respaldos automáticos de Render activados).
- [ ] Variables: `APP_ENV=production`, `ODOO_URL` = URL de CBH1, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`, `ODOO_COMPANY_ID`, `SSO_SECRET`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (producción: modelo principal; staging puede usar el rápido), `SCHEDULE_ENABLED=true`, `SCHEDULE_AGENT1_CRON`, `SCHEDULE_AGENT2_CRON`, `SCHEDULE_VIGILANCIA_CRON`, `CONSUMO_CACHE_SEG=600`, `BASE_URL` (dominio definitivo, HTTPS) y `EMBED_ORIGINS` (URL de CBH1, para que Odoo pueda embeber la plataforma).
- [ ] Dominio propio con HTTPS; en Odoo, Ajustes ▸ Agentes de IA ▸ URL de la plataforma = ese dominio (para el menú y el SSO).
- [ ] UptimeRobot (o similar) vigilando `/health`.
- [ ] Rama de despliegue: `main` del repositorio `CBH-AGENTES` (staging sigue en `staging`). Subir siempre `app`, `docs`, `tests`, `README.md` y `render.yaml`.

## 4. Odoo CBH1

- [ ] El repositorio de Odoo.sh de producción contiene **sólo** el addon `cbh_agentes_ia` de este proyecto (no el código del servicio ni carpetas de pruebas).
- [ ] Usuario técnico con los grupos mínimos (`SEGURIDAD.md` §1): lectura de operación médica, inventario, compras, contabilidad (facturas de cliente), usuarios (sólo lectura) y correo saliente; **sin** nómina ni administración.
- [ ] Grupos de Agentes de IA asignados a las personas reales (Usuario / Operación / Administrador). Quien no tenga grupo no puede entrar.
- [ ] Servidor de correo saliente configurado y probado (para los correos que el copiloto envía con aprobación).
- [ ] Grupos de Contabilidad/Facturación e Inventario con miembros reales (Configuración ▸ Avisos ▸ «¿Quién recibiría cada aviso hoy?» no debe decir «nadie»).

## 5. Primer arranque en producción

- [ ] Configuración ▸ Probar conexión → **Re-descubrir**: «Módulo de folios de CBH» debe decir *detectado automáticamente* con `cbh.medical.service.request` / `…line`.
- [ ] Configuración ▸ Qué pueden hacer los agentes: nivel **1** las primeras semanas (todo en borrador en Odoo); subir a 2 cuando Compras y Almacén lo pidan.
- [ ] Límites de seguridad revisados con el cliente: importe máximo por acción **mayor** que el umbral de riesgo alto; cantidad máxima acorde a sus compras reales; almacenes bloqueados si aplica.
- [ ] Presupuesto mensual de IA capturado por Cóndor (rol `condor`) igual al paquete contratado.
- [ ] Primera corrida manual de cada agente con alguien de Operación mirando la Bitácora; después dejar el horario automático.
- [ ] Entregar al cliente `SEGURIDAD.md` y esta lista firmada.

## Qué cambia automáticamente con `APP_ENV=production`

- Las acciones aún no validadas en producción (`acciones_solo_propuesta`: validar recepción, confirmar transferencia, cuarentena, desecho, reprogramar compra, plazo de proveedor) se proponen y aprueban pero quedan **aprobadas para ejecución manual** en Odoo. Se van liberando de esa lista conforme se validan.
- El chip del entorno cambia a **PRODUCCIÓN** y la lista de preparación deja de pedir claves nuevas.
