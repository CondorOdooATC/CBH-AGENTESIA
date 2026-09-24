"""Cliente JSON-RPC de Odoo (external API), con reintentos y caché corta.

Funciona contra Odoo.sh / Odoo Online usando una API key de usuario
(Ajustes ▸ Mi perfil ▸ Seguridad de la cuenta ▸ Claves API).
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from ..config import settings
from .. import db


class OdooError(RuntimeError):
    """Error devuelto por el servidor Odoo o por la capa de transporte."""


class OdooClient:
    def __init__(self, url: str | None = None, dbname: str | None = None,
                 user: str | None = None, api_key: str | None = None,
                 timeout: int | None = None) -> None:
        self.url = (url or settings.ODOO_URL).rstrip("/")
        self.db = dbname or settings.ODOO_DB
        self.user = user or settings.ODOO_USER
        self.api_key = api_key or settings.ODOO_API_KEY
        self.timeout = timeout or settings.ODOO_TIMEOUT
        self.uid: int | None = None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._version: dict | None = None

    # ── transporte ──────────────────────────────────────────────────────────
    def _rpc(self, service: str, method: str, args: list, intentos: int = 3) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": int(time.time() * 1000) % 1_000_000,
        }
        ultimo = None
        for intento in range(intentos):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
                    r = c.post(f"{self.url}/jsonrpc", json=payload,
                               headers={"Content-Type": "application/json"})
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    err = data["error"]
                    msg = (err.get("data", {}) or {}).get("message") or err.get("message") or json.dumps(err)
                    raise OdooError(msg)
                return data.get("result")
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
                ultimo = e
                if intento < intentos - 1:
                    time.sleep(1.5 * (intento + 1))
                    continue
                raise OdooError(f"Fallo de conexión con Odoo: {e}") from e
        raise OdooError(f"Fallo de conexión con Odoo: {ultimo}")

    # ── autenticación ───────────────────────────────────────────────────────
    def login(self, forzar: bool = False) -> int:
        with self._lock:
            if self.uid and not forzar:
                return self.uid
            if not (self.url and self.db and self.user and self.api_key):
                raise OdooError("Odoo no está configurado (revisa ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY).")
            uid = self._rpc("common", "login", [self.db, self.user, self.api_key])
            if not uid:
                raise OdooError("Credenciales de Odoo inválidas (usuario o API key).")
            self.uid = int(uid)
            db.log("info", "odoo", "Autenticación correcta", f"uid={self.uid} db={self.db}")
            return self.uid

    def version(self) -> dict:
        if self._version is None:
            self._version = self._rpc("common", "version", []) or {}
        return self._version

    def probar(self) -> dict:
        """Diagnóstico de conexión, usado por /health y por la UI."""
        try:
            v = self.version()
            uid = self.login(forzar=True)
            nombre = self.execute("res.users", "read", [[uid], ["name", "login"]])
            companias = self.execute("res.company", "search_read", [[], ["id", "name"]], limit=20)
            return {"ok": True, "version": v.get("server_version"), "uid": uid,
                    "usuario": nombre[0]["name"] if nombre else "", "companias": companias}
        except Exception as e:  # noqa: BLE001 - queremos reportar cualquier fallo
            return {"ok": False, "error": str(e)}

    # ── ejecución ───────────────────────────────────────────────────────────
    METODOS_LECTURA = {"search_read", "search_count", "read", "read_group", "fields_get", "name_search", "search",
                       "name_get", "check_access_rights"}

    def execute(self, modelo: str, metodo: str, args: list | None = None, **kwargs) -> Any:
        """Las lecturas se reintentan ante fallas de red; las ESCRITURAS no (si Odoo creó el documento y se perdió la
        respuesta, un reintento lo duplicaría). La recuperación de escrituras se hace por referencia en acciones.py."""
        reintentar = kwargs.pop("reintentar", None)
        uid = self.login()
        ctx = kwargs.pop("context", None) or {}
        if settings.ODOO_COMPANY_ID:
            ctx.setdefault("allowed_company_ids", [settings.ODOO_COMPANY_ID])
            ctx.setdefault("company_id", settings.ODOO_COMPANY_ID)
        ctx.setdefault("lang", "es_MX")
        if ctx:
            kwargs["context"] = ctx
        intentos = 3 if (reintentar if reintentar is not None else metodo in self.METODOS_LECTURA) else 1
        return self._rpc("object", "execute_kw",
                         [self.db, uid, self.api_key, modelo, metodo, args or [], kwargs], intentos=intentos)

    # ── atajos de lectura ───────────────────────────────────────────────────
    def search_read(self, modelo: str, dominio: list, campos: list[str] | None = None,
                    limite: int = 0, orden: str | None = None, offset: int = 0) -> list[dict]:
        kw: dict[str, Any] = {"fields": campos or []}
        if limite:
            kw["limit"] = limite
        if orden:
            kw["order"] = orden
        if offset:
            kw["offset"] = offset
        return self.execute(modelo, "search_read", [dominio], **kw) or []

    def _en_paralelo(self, fn, bloques: list) -> list:
        """Ejecuta fn sobre cada bloque con varias conexiones a la vez (cada llamada abre su propia conexión HTTP,
        así que es seguro). Conserva el orden de los bloques."""
        paralelas = max(1, min(settings.ODOO_LECTURAS_PARALELAS, len(bloques)))
        if paralelas == 1:
            return [fn(b) for b in bloques]
        with ThreadPoolExecutor(max_workers=paralelas, thread_name_prefix="odoo-lectura") as ex:
            return list(ex.map(fn, bloques))

    def search_read_all(self, modelo: str, dominio: list, campos: list[str],
                        pagina: int | None = None, tope: int = 200_000, orden: str = "id") -> list[dict]:
        """Lee TODO lo que cumple el dominio: una búsqueda de ids (barata, sin resolver nombres) y después lecturas por
        bloques con varias conexiones a la vez. Devuelve exactamente lo mismo que paginar con offset, en el mismo orden,
        pero sin que Odoo repita la búsqueda completa en cada página y aprovechando sus workers en paralelo."""
        pagina = pagina or settings.ODOO_BLOQUE_LECTURA
        ids = [int(i) for i in (self.execute(modelo, "search", [dominio], order=orden, limit=tope) or [])]
        if not ids:
            return []
        bloques = [ids[i:i + pagina] for i in range(0, len(ids), pagina)]
        partes = self._en_paralelo(lambda b: self.execute(modelo, "read", [b, list(campos)]) or [], bloques)
        por_id = {int(r["id"]): r for parte in partes for r in parte}
        return [por_id[i] for i in ids if i in por_id]

    def search_read_por_ids(self, modelo: str, ids: list[int], campos: list[str], dominio_extra: list | None = None,
                            campo: str = "id", bloque: int = 1000) -> list[dict]:
        """search_read con «campo in ids» partido en bloques y leído en paralelo (cabeceras de folio, lotes, costos).
        Mismo resultado que el bucle en serie."""
        ids = sorted({int(x) for x in ids})
        if not ids:
            return []
        bloques = [ids[i:i + bloque] for i in range(0, len(ids), bloque)]
        partes = self._en_paralelo(lambda b: self.search_read(modelo, [[campo, "in", b]] + list(dominio_extra or []), campos, limite=0), bloques)
        return [r for parte in partes for r in parte]

    def search_count(self, modelo: str, dominio: list) -> int:
        return int(self.execute(modelo, "search_count", [dominio]) or 0)

    def read_group(self, modelo: str, dominio: list, campos: list[str], groupby: list[str],
                   lazy: bool = False, limite: int = 0) -> list[dict]:
        kw: dict[str, Any] = {"lazy": lazy}
        if limite:
            kw["limit"] = limite
        return self.execute(modelo, "read_group", [dominio, campos, groupby], **kw) or []

    def fields_get(self, modelo: str, atributos: list[str] | None = None) -> dict:
        clave = f"fields:{modelo}"
        hit = self._cache.get(clave)
        if hit and time.time() - hit[0] < 900:
            return hit[1]
        res = self.execute(modelo, "fields_get", [[]],
                           attributes=atributos or ["string", "type", "relation", "required", "selection", "store"])
        self._cache[clave] = (time.time(), res)
        return res or {}

    def modelos(self, patron: str = "") -> list[dict]:
        dominio = [["transient", "=", False]]
        if patron:
            dominio.append(["model", "like", patron])
        return self.search_read("ir.model", dominio, ["model", "name", "modules"], orden="model")

    def existe_modelo(self, modelo: str) -> bool:
        return self.search_count("ir.model", [["model", "=", modelo]]) > 0

    # ── atajos de escritura ─────────────────────────────────────────────────
    def create(self, modelo: str, valores: dict | list[dict]) -> int | list[int]:
        return self.execute(modelo, "create", [valores])

    def write(self, modelo: str, ids: list[int], valores: dict) -> bool:
        return self.execute(modelo, "write", [ids, valores])

    def unlink(self, modelo: str, ids: list[int]) -> bool:
        return self.execute(modelo, "unlink", [ids])

    def call_button(self, modelo: str, ids: list[int], metodo: str) -> Any:
        return self.execute(modelo, metodo, [ids])

    def name_search(self, modelo: str, nombre: str, limite: int = 10, dominio: list | None = None) -> list:
        return self.execute(modelo, "name_search", [], name=nombre, args=dominio or [], limit=limite) or []

    def mensaje_chatter(self, modelo: str, res_id: int, cuerpo: str, asunto: str = "") -> int:
        return self.execute(modelo, "message_post", [[res_id]],
                            body=cuerpo, subject=asunto or None, message_type="comment",
                            subtype_xmlid="mail.mt_note")


# ── instancia compartida ────────────────────────────────────────────────────
_cliente: OdooClient | None = None
_cliente_lock = threading.Lock()


def get_client() -> OdooClient:
    global _cliente
    with _cliente_lock:
        if _cliente is None:
            _cliente = OdooClient()
        return _cliente


def set_client(c: OdooClient) -> None:
    """Permite inyectar un cliente simulado en las pruebas."""
    global _cliente
    with _cliente_lock:
        _cliente = c
