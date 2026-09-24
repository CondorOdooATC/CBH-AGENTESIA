# Despliegue — staging → producción en Render

Objetivo: tener el servicio corriendo contra **CBHTEST** (staging de Odoo.sh) en la primera hora, validarlo con la lista de `VALIDACION_STAGING.md` y promoverlo a **CBH1** (producción) con un merge. Desplegar el servicio es rápido; **ponerlo en operación** exige la validación en staging con datos reales.

## 0. Cómo queda armado

Dos piezas, cada una en su lugar:

- **Addon de Odoo `cbh_agentes_ia`** (carpeta `odoo_addon/cbh_agentes_ia`): se sube al repositorio de Odoo.sh como cualquier otro módulo. Pone el menú **Agentes de IA** en Odoo, los grupos de acceso (Consulta / Operación / Administrador), el acceso sin segundo login y la pantalla de Ajustes con los datos listos para Render. No instala dependencias de Python ni tareas en Odoo.
- **Servicio `cbh-agentes-ia`** (este repositorio): corre en Render con disco persistente y se conecta a Odoo por la API con un usuario técnico. Es donde viven los agentes, el motor, el chat y los Excels.

El cliente instala un módulo y entra desde Odoo con su usuario de siempre; sus permisos se administran con los grupos de Odoo.

## 1. Lo que necesitas a la mano (10 min)

| Qué | Dónde |
|---|---|
| Cuenta de Render (workspace Pro; instancia Standard 1 CPU / 2 GB para producción, Starter o Free para staging) | render.com |
| Repositorio Git con esta carpeta (GitHub/GitLab), **separado** del repo de Odoo.sh | ramas `staging` y `main` |
| API key de Anthropic | console.anthropic.com ▸ API Keys |
| Acceso al repositorio de Odoo.sh de CBH (rama de staging CBHTEST) | para subir el addon |

> El addon **no** va en el repo de Render y el servicio **no** va en el repo de Odoo.sh: Odoo.sh instalaría el `requirements.txt` de la raíz en el build de Odoo.

## 2. Instalar el addon en Odoo (staging CBHTEST) — 10 min

1. Copia la carpeta `odoo_addon/cbh_agentes_ia` a la raíz del repositorio de Odoo.sh (junto a los demás módulos), haz commit a la rama de **CBHTEST** y espera el build.
2. Apps ▸ Actualizar lista ▸ instala **Agentes de IA · CBH (puente)**. Al instalarse genera un secreto SSO y crea el usuario técnico `agente.ia@i-condor.com` (sin contraseña; sólo entra por clave API) con Inventario administrador, Compras usuario, Empleados oficial y Contactos, en todas las compañías.
3. Ajustes ▸ **Agentes de IA** ▸ pulsa **Generar clave API**. Copia el bloque **Datos para Render** (ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY, ODOO_COMPANY_ID, SSO_SECRET). Deja la URL de la plataforma vacía hasta el paso 3.
4. Ajustes ▸ Usuarios ▸ da a cada persona el grupo **Agentes de IA**: *Consulta* (dirección), *Operación* (inventarios, compras, jefaturas) o *Administrador* (segunda aprobación de riesgo alto, políticas). Sin grupo, el menú no aparece.
5. Revisa que el usuario técnico tenga lectura sobre los modelos de CBH (`cbh.operacion.medica` y sus líneas). Si un campo custom no se descubre solo, se ajusta después desde Configuración ▸ Mapeo en la plataforma.

## 3. Crear el servicio en Render (10 min)

1. Sube este repositorio y crea las ramas:
   ```bash
   git init && git add . && git commit -m "CBH · Agentes de IA v1.3"
   git branch -M main && git checkout -b staging
   git remote add origin git@github.com:IngenieriaCondor/cbh-agentes-ia.git
   git push -u origin staging && git push -u origin main
   ```
2. Render ▸ **New ▸ Blueprint** ▸ elige el repositorio ▸ Render lee `render.yaml` y crea `cbh-agentes-staging` (rama `staging`) y `cbh-agentes-ia` (rama `main`), cada uno con disco `/data` de 1 GB ▸ **Apply**. El primer build tarda ~3 minutos.
3. Render ▸ `cbh-agentes-staging` ▸ **Environment** ▸ pega las variables del paso 2.3 más `ADMIN_PASSWORD` (cuenta local `condor` de Ingeniería Cóndor, mínimo 10 caracteres) y `ANTHROPIC_API_KEY`. Deja `EMBED_ORIGINS` vacío (pestaña nueva). Guarda → Render redepliega solo.
4. Copia la URL del servicio (`https://cbh-agentes-staging.onrender.com`) en Odoo ▸ Ajustes ▸ Agentes de IA ▸ **URL de la plataforma** y pulsa **Probar conexión**: debe decir «Conexión correcta … usuario … rol …».

> Modelos: staging usa `claude-sonnet-5` (rápido y barato) y producción `claude-opus-5-5`. Se cambian con `ANTHROPIC_MODEL` o desde Configuración con un usuario `condor`. La API key vive **sólo** en las variables de entorno de Render: nunca se muestra ni se edita desde la interfaz.

## 4. Verificar en staging (15 min)

1. `https://cbh-agentes-staging.onrender.com/health` → `"ok": true`.
2. Desde Odoo, con un usuario que tenga el grupo *Operación*, abre el menú **Agentes de IA**: entra sin pedir contraseña y aparece con su nombre y rol en la barra lateral. Con un usuario sin grupo, el menú no existe.
3. Entra también como `condor` (usuario local, `/login`) para la configuración: **Configuración ▸ Probar conexión** (versión de Odoo y compañías) y **Re-descubrir modelos y campos**; si un campo salió `no_encontrado`, usa *Ajuste manual*.
4. **Agente · Consumo ▸ Actualizar** (180 días) y **Agente · Abasto ▸ Actualizar** (30 días). Revisa informes, Excel y **Decisiones**.
5. Aprueba una transferencia de prueba → en Odoo CBHTEST aparece la transferencia interna en borrador con referencia `Agente IA · acción #N` → **Revertir** desde Decisiones → queda cancelada.
6. Recorre `docs/VALIDACION_STAGING.md` completa (cálculos, aprobaciones, acciones nuevas, seguridad) antes de promover.
7. Conversa con cada agente desde su pantalla y con el Investigador desde un caso; el botón **💬 Hablar con los agentes** está en todas las pantallas.

## 5. Promover a producción (cuando la validación en staging esté completa)

```bash
git checkout main && git merge staging && git push
```

Render despliega `cbh-agentes-ia` automáticamente. Del lado de Odoo, promueve el addon a la rama de **producción (CBH1)** con el mismo merge que usan para los demás módulos, instálalo, genera la clave API y el secreto **de producción** (son distintos a los de staging) y captúralos en `cbh-agentes-ia` ▸ Environment junto con la URL definitiva. Repite el paso 4. En producción el scheduler corre el Agente 1 diario a las 07:00 y el Agente 2 los lunes a las 07:30 (hora CDMX); ajusta `SCHEDULE_AGENT*_CRON` si hace falta.

## 6. Dominio .mx y monitoreo (10 min)

- **Dominio**: Render ▸ `cbh-agentes-ia` ▸ Settings ▸ **Custom Domains** ▸ `agentes.i-condor.mx` (o el dominio comprado) → agrega el CNAME que indica Render en tu DNS. TLS se emite solo.
- **UptimeRobot** (gratuito): New Monitor ▸ HTTPS ▸ `https://agentes.i-condor.mx/health` ▸ cada 5 min ▸ alertas a tu correo.

## 7. Operación diaria

| Tarea | Dónde |
|---|---|
| Ver lo que importa hoy | Panel (briefing + KPIs + decisiones pendientes) |
| Aprobar / rechazar acciones | Acciones (rechazar con motivo enseña al agente) |
| Justificar o confirmar hallazgos | Hallazgos (entrena los umbrales) |
| Pedir un Excel a la medida | Copiloto: «dame un Excel de…» |
| Enseñar reglas del negocio | Configuración ▸ Aprendizaje, o díselo al Copiloto |
| Ver consumo de tokens / costo | Bitácora (presupuesto mensual configurable) |
| Respaldo de la base | `/api/respaldo` (sólo `condor`) descarga el SQLite completo; programar descarga semanal |
| Dar o quitar acceso a una persona | Odoo ▸ Usuarios ▸ grupo Agentes de IA (Consulta / Operación / Administrador); aplica en su siguiente entrada |
| Acciones «aprobadas para ejecución manual» | En producción, cuarentena, desecho, validar recepción, confirmar transferencia, reprogramar compra y plazo de proveedor no se ejecutan solas hasta validarse en staging: la operación las hace en Odoo |
| Cerrar casos investigados | Casos ▸ Resolver / Descartar con resolución (entrena a los agentes) |
| Segunda aprobación de riesgo alto | Otro usuario con rol admin aprueba desde Decisiones |

> El disco persistente de Render no es alta disponibilidad ni respaldo: un servicio con disco corre en una sola instancia y sin despliegue sin interrupciones. Para operación crítica con más usuarios, migrar la persistencia a PostgreSQL administrado (la capa `app/db.py` está aislada para ello).

## Problemas comunes

| Síntoma | Causa / solución |
|---|---|
| `Credenciales de Odoo inválidas` | La API key se generó en otra base (cada rama de Odoo.sh tiene sus propias claves) |
| Los agentes ven 0 líneas | El usuario no tiene CBH+ en compañías permitidas, o `ODOO_COMPANY_ID` es de otra compañía |
| Consumo sin básculas / médicos | Campos no mapeados: Configuración ▸ Mapeo ▸ ajuste manual de `peso_inicial`, `peso_final`, `medico`… |
| "Sin LLM" en la barra lateral | Falta `ANTHROPIC_API_KEY` o se agotó el presupuesto; los agentes siguen funcionando con informes deterministas |
| «Firma inválida» al entrar desde Odoo | El `SSO_SECRET` de Render no es el de Ajustes ▸ Agentes de IA de **esa** base (staging y producción tienen secretos distintos) |
| «El enlace caducó» | El reloj del servidor de Odoo o de Render está desfasado > 60 s, o se reutilizó un enlace; vuelve a abrir desde el menú |
| Embebido en Odoo se queda en la pantalla de acceso | El navegador bloquea cookies de terceros (Safari) o falta `EMBED_ORIGINS`; usa el modo pestaña nueva |
| Acción `bloqueada` | Fuera de política (tope, ubicación). Ajusta políticas o aprueba manualmente en Odoo |
