# Arquitectura

```
┌──────────────────────────────┐        JSON-RPC (API key)        ┌──────────────────────┐
│  Render · Web Service        │ ───────────────────────────────▶ │  Odoo 19 (Odoo.sh)   │
│  FastAPI + APScheduler       │ ◀─────────────────────────────── │  CBHTEST / CBH1      │
│  ┌────────────────────────┐  │   lectura: consumo, quants,      │  cbh.operacion.*     │
│  │ Agente 1 · consumo     │  │   lotes, proveedores, folios     │  stock.*  purchase.* │
│  │ Agente 2 · demanda     │  │   escritura (tras aprobación):   └──────────────────────┘
│  │ Copiloto · briefing    │  │   pickings, RFQ, orderpoints,
│  │ Autonomía (políticas)  │  │   actividades, chatter
│  └────────────────────────┘  │
│  /data (disco persistente)   │        HTTPS                     ┌──────────────────────┐
│   SQLite: perfiles, hallazgos│ ───────────────────────────────▶ │  API de Claude       │
│   acciones, aprendizaje,     │   narrativa, briefing,           │  (opus-5 / sonnet-5) │
│   conversaciones, bitácora   │   copiloto con herramientas      └──────────────────────┘
│   + reportes .xlsx           │
└──────────────────────────────┘
```

## Ciclo agéntico

```
 motor estadístico ──evidencia──▶ INVESTIGADOR (Claude + herramientas) ──▶ expedientes (casos)
        │                                                                     │
        └──plan base──▶ PLANIFICADOR (Claude + herramientas) ──ajusta/agrega──▶ cola de DECISIONES
                          · precisión pasada · folios programados · escenarios          │ aprobación humana
 VIGILANCIA (cada 3 h) ──alertas──────────────────────────────────────────────────────▶ │ (doble para riesgo alto)
                                                                                        ▼
                                          revalidar ▶ ejecutar (idempotente) ▶ verificar en Odoo ▶ aprender
```

## Principios

- **Determinista primero, LLM después.** Toda cifra, hallazgo, pronóstico y propuesta sale de código auditable (`app/ml`). Claude sólo interpreta, redacta y conversa; si no hay API key o se agota el presupuesto, la plataforma sigue operando con informes deterministas.
- **Nunca depende de nombres de campos.** `app/odoo/schema.py` traduce conceptos (`consumo.cantidad`) a campos reales, los descubre solos y admite ajuste manual; sin el módulo custom cae a `stock.move.line`.
- **Nada se ejecuta en Odoo sin una persona** (nivel 1 por defecto). Políticas + cola de aprobación + reversión + bitácora.
- **Aprende en cada corrida y de cada clic.** Perfiles estadísticos recalculados con datos reales; feedback humano ajusta umbrales; notas de aprendizaje inyectadas a los agentes.
- **Un solo proceso, un solo disco.** Sin bases externas ni colas: SQLite en WAL sobre el disco persistente de Render. Suficiente para decenas de usuarios y cientos de miles de líneas.

## Agente 1 · Motor de anomalías (`app/ml/anomalias.py`)

1. Perfiles robustos (mediana/MAD/percentiles) por producto, producto×unidad, producto×médico, producto×auxiliar, producto×sub-almacén; para volátiles con duración, la métrica es la **tasa mL/min**.
2. Z robusto con piso para cantidades discretas; sólo sobreconsumo; umbral ajustado por aprendizaje.
3. Isolation Forest sobre características (cantidad relativa, tasa, hora, día, sin médico, duración).
4. Reglas: R01 excede envase · R02 báscula imposible · R03 discrepancia báscula vs. captura · R04 sin cirugía · R05 horario atípico · R06 tasa clínica · R07 lote fuera de su unidad · R08 duplicado · R09 lote caducado.
5. Patrones: R10 cambio de nivel (ventanas 30/60 días) · R11 actor vs. pares (consistente: p25 > mediana de pares) · R12 frecuencia atípica.
6. Fusión → severidad, importe en riesgo, índices de riesgo por actor/unidad, análisis histórico (tendencias, estacionalidad, YoY, CUSUM).

## Agente 2 · Motor de pronóstico (`app/ml/pronostico.py`)

- Serie diaria por producto × ubicación; métodos: media móvil, SES, Holt amortiguado, Holt-Winters semanal, estacional ingenuo, Croston/SBA (intermitentes). Selección por **WAPE semanal en backtesting de origen móvil**.
- Stock de seguridad `z·σ·√LT`, punto de reorden, cobertura, sugerido local (ciclo + LT + SS) y de red (horizonte + SS), criticidad.
- ABC (valor) × XYZ (variabilidad) con política sugerida; FEFO de caducidades; rebalanceo entre almacenes con afinidad regional.

## Acceso desde Odoo (addon puente)

`odoo_addon/cbh_agentes_ia` (Odoo 19): menú, tres grupos, controlador que redirige con un token HMAC de 60 s al servicio (`/sso`), pantalla de Ajustes con los datos para Render, usuario técnico y clave API. El servicio verifica firma, vigencia, base y repetición, crea o actualiza al usuario con rol según el grupo de Odoo (nunca `condor`) y abre sesión. Modo embebido opcional (iframe + `EMBED_ORIGINS`).

## Seguridad

- Sesiones por cookie `HttpOnly`, contraseñas PBKDF2-SHA256, roles consulta/operacion/admin/**condor** (sólo Ingeniería Cóndor: configuración del LLM y alta de usuarios condor).
- Credenciales sólo en variables de entorno; el copiloto no puede leer `res.users` ni configuración.
- Acciones de riesgo alto exigen dos personas distintas (la segunda admin/condor); propuestas versionadas: se ejecuta sólo la versión aprobada; todo revertible (cuando aplica) y trazado.
- Escrituras a Odoo sin reintentos automáticos y con recuperación por referencia (`origin`), para que una respuesta perdida no duplique documentos.
