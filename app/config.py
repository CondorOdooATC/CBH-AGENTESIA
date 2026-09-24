"""Configuración central de la aplicación (12-factor: todo por variables de entorno)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _b(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _i(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, "") or default))
    except (TypeError, ValueError):
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return default


class Settings:
    """Ajustes de la app. Se instancia una sola vez (ver get_settings)."""

    # ── Aplicación ──────────────────────────────────────────────────────────
    APP_NAME = "CBH · Agentes de IA"
    APP_SHORT = "Agentes CBH"
    ORG = "Ingeniería Cóndor"
    CLIENT = "Grupo CB · CBH+"
    VERSION = "1.3.11"

    def __init__(self) -> None:
        self.APP_ENV = os.getenv("APP_ENV", "development")
        self.SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-cambiar")
        self.BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
        self.TZ = os.getenv("TZ", "America/Mexico_City")

        # Disco persistente (Render monta /data). En local cae a ./data
        data_dir = os.getenv("DATA_DIR") or str(Path.cwd() / "data")
        self.DATA_DIR = Path(data_dir)
        self.DB_PATH = self.DATA_DIR / "cbh_agentes.db"
        self.REPORTS_DIR = self.DATA_DIR / "reportes"
        self.LOG_DIR = self.DATA_DIR / "bitacora"
        for d in (self.DATA_DIR, self.REPORTS_DIR, self.LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)

        # ── Admin inicial ───────────────────────────────────────────────────
        self.ADMIN_USER = os.getenv("ADMIN_USER", "admin")
        self.ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

        # ── Odoo ────────────────────────────────────────────────────────────
        self.ODOO_URL = (os.getenv("ODOO_URL", "") or "").rstrip("/")
        self.ODOO_DB = os.getenv("ODOO_DB", "")
        self.ODOO_USER = os.getenv("ODOO_USER", "")
        self.ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")
        self.ODOO_COMPANY_ID = _i("ODOO_COMPANY_ID", 0) or None
        self.ODOO_TIMEOUT = _i("ODOO_TIMEOUT", 120)
        # Lecturas grandes (consumo, cabeceras de folio, lotes) se bajan por bloques con varias conexiones a la vez.
        # Mismo resultado que leer en serie; sólo cambia el tiempo. 1 = en serie. Odoo.sh con pocos workers: 2–4.
        self.ODOO_LECTURAS_PARALELAS = max(1, min(8, _i("ODOO_LECTURAS_PARALELAS", 4)))
        self.ODOO_BLOQUE_LECTURA = max(200, _i("ODOO_BLOQUE_LECTURA", 2000))
        # Memoria local del consumo: la primera corrida baja todo; las siguientes sólo lo nuevo o cambiado (write_date).
        self.CONSUMO_INCREMENTAL = _b("CONSUMO_INCREMENTAL", True)
        self.CONSUMO_MEMORIA_DIAS = _i("CONSUMO_MEMORIA_DIAS", 120)   # olvida líneas que ninguna corrida pidió en N días

        # ── Acceso desde Odoo (SSO) y seguridad ─────────────────────────────
        self.SSO_SECRET = os.getenv("SSO_SECRET", "")              # mismo valor que el parámetro cbh_agentes_ia.secreto en Odoo
        self.SSO_DB = os.getenv("SSO_DB", "")                      # por omisión, ODOO_DB
        self.EMBED_ORIGINS = [x.strip() for x in os.getenv("EMBED_ORIGINS", "").split(",") if x.strip()]   # p. ej. https://cbticket2-0.odoo.com
        self.LOGIN_MAX_INTENTOS = _i("LOGIN_MAX_INTENTOS", 8)      # por usuario/IP en la ventana
        self.LOGIN_VENTANA_MIN = _i("LOGIN_VENTANA_MIN", 15)
        self.SESION_HORAS = _i("SESION_HORAS", 12)
        self.PASSWORD_MIN = _i("PASSWORD_MIN", 10)

        # ── Claude ──────────────────────────────────────────────────────────
        self.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
        self.ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
        self.ANTHROPIC_MODEL_FAST = os.getenv("ANTHROPIC_MODEL_FAST", "claude-sonnet-5")
        # Los modelos Claude 5 razonan antes de responder y ese razonamiento cuenta dentro de max_tokens: se deja
        # margen para el razonamiento + la respuesta; si aun así se corta, se reintenta una vez hasta el tope.
        self.ANTHROPIC_MAX_TOKENS = _i("ANTHROPIC_MAX_TOKENS", 16000)
        self.ANTHROPIC_MAX_TOKENS_TOPE = max(self.ANTHROPIC_MAX_TOKENS, _i("ANTHROPIC_MAX_TOKENS_TOPE", 32000))
        # Esfuerzo del modelo (low | medium | high; vacío = el que trae la API: medium en Opus 5.5, high en Sonnet 5).
        # El razonamiento se cobra como tokens de salida: «low» en las tareas con herramientas (copiloto, expedientes,
        # vigilancia, briefing) ahorra la mayor parte; los informes y el plan razonado conservan «medium».
        self.ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "low").strip().lower()
        self.ANTHROPIC_EFFORT_INFORMES = os.getenv("ANTHROPIC_EFFORT_INFORMES", "medium").strip().lower()
        self.ANTHROPIC_CACHE = _b("ANTHROPIC_CACHE", True)      # caché de prompts (system, herramientas e historial)
        self.ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
        self.LLM_ENABLED = _b("LLM_ENABLED", True) and bool(self.ANTHROPIC_API_KEY)

        # ── Presupuesto de tokens ───────────────────────────────────────────
        self.BUDGET_INPUT_TOKENS = _i("BUDGET_INPUT_TOKENS", 13_500_000)
        self.BUDGET_OUTPUT_TOKENS = _i("BUDGET_OUTPUT_TOKENS", 1_350_000)
        # Tarifas USD por millón de tokens (entrada, salida) por modelo. Verificar en la consola de Anthropic;
        # se pueden sobreescribir con PRICE_TABLE='{"claude-opus-5":[5,25],"claude-sonnet-5":[2,10]}'
        import json as _json
        self.PRICE_TABLE: dict[str, tuple[float, float]] = {"claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
                                                            "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}
        try:
            self.PRICE_TABLE.update({k: (float(v[0]), float(v[1])) for k, v in _json.loads(os.getenv("PRICE_TABLE", "{}")).items()})
        except (ValueError, TypeError, IndexError):
            pass
        self.PRICE_INPUT_PER_MTOK = _f("PRICE_INPUT_PER_MTOK", 0.0)    # respaldo si el modelo no está en la tabla
        self.PRICE_OUTPUT_PER_MTOK = _f("PRICE_OUTPUT_PER_MTOK", 0.0)

        # ── Agente 1 · Control Inteligente de Consumo ───────────────────────
        self.AGENT1_LOOKBACK_DAYS = _i("AGENT1_LOOKBACK_DAYS", 180)
        # Caché en memoria de la lectura de consumo (segundos): evita leer Odoo dos veces cuando los agentes corren seguidos.
        # 0 = sin caché (pruebas/desarrollo). En Render se fija a 600.
        self.CONSUMO_CACHE_SEG = _i("CONSUMO_CACHE_SEG", 0)
        self.AGENT1_SEVERITY_THRESHOLD = _f("AGENT1_SEVERITY_THRESHOLD", 2.5)
        self.AGENT1_MIN_SAMPLES = _i("AGENT1_MIN_SAMPLES", 8)

        # ── Agente 2 · Pronóstico de Demanda y Resurtido ────────────────────
        self.AGENT2_HORIZON_DAYS = _i("AGENT2_HORIZON_DAYS", 30)
        self.AGENT2_HISTORY_DAYS = _i("AGENT2_HISTORY_DAYS", 540)
        self.AGENT2_SERVICE_LEVEL = _f("AGENT2_SERVICE_LEVEL", 0.95)
        self.AGENT2_LEAD_TIME_DAYS = _i("AGENT2_LEAD_TIME_DAYS", 7)

        # ── Scheduler ───────────────────────────────────────────────────────
        self.SCHEDULE_ENABLED = _b("SCHEDULE_ENABLED", False)
        self.SCHEDULE_AGENT1_CRON = os.getenv("SCHEDULE_AGENT1_CRON", "0 7 * * *")
        self.SCHEDULE_AGENT2_CRON = os.getenv("SCHEDULE_AGENT2_CRON", "30 7 * * 1")
        self.SCHEDULE_VIGILANCIA_CRON = os.getenv("SCHEDULE_VIGILANCIA_CRON", "15 */3 * * *")

        # ── Retención ───────────────────────────────────────────────────────
        self.REPORT_RETENTION_DAYS = _i("REPORT_RETENTION_DAYS", 120)

    # ── Utilidades ──────────────────────────────────────────────────────────
    @property
    def odoo_configured(self) -> bool:
        return bool(self.ODOO_URL and self.ODOO_DB and self.ODOO_USER and self.ODOO_API_KEY)

    def precio(self, modelo: str | None) -> tuple[float, float]:
        m = (modelo or self.ANTHROPIC_MODEL or "").lower()
        # prefijo más largo primero: "claude-opus-5-5" no debe cobrarse con la tarifa de "claude-opus-5"
        for k in sorted(self.PRICE_TABLE, key=len, reverse=True):
            if m.startswith(k):
                return self.PRICE_TABLE[k]
        return (self.PRICE_INPUT_PER_MTOK or 5.0, self.PRICE_OUTPUT_PER_MTOK or 25.0)

    # Caché de prompts: escribir cuesta 1.25× la entrada; leer 0.1× (Opus 5.5: 0.05×). Fuente: docs de Anthropic, sep-2026.
    CACHE_ESCRITURA = 1.25

    def cache_lectura(self, modelo: str | None) -> float:
        return 0.05 if (modelo or self.ANTHROPIC_MODEL or "").lower().startswith("claude-opus-5-5") else 0.1

    def entrada_equivalente(self, tok_in: int, cache_w: int = 0, cache_r: int = 0, modelo: str | None = None) -> int:
        return int(round((tok_in or 0) + (cache_w or 0) * self.CACHE_ESCRITURA + (cache_r or 0) * self.cache_lectura(modelo)))

    def cost_usd(self, tok_in: int, tok_out: int, modelo: str | None = None, cache_w: int = 0, cache_r: int = 0) -> float:
        pi, po = self.precio(modelo)
        return (self.entrada_equivalente(tok_in, cache_w, cache_r, modelo) / 1e6) * pi + (tok_out / 1e6) * po


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
