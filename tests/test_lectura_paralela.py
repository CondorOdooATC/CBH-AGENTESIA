"""v1.3.10 · Lecturas de Odoo por bloques en paralelo: mismo resultado y mismo orden que paginar en serie."""
from __future__ import annotations

import threading

from app.config import settings
from app.odoo.client import OdooClient


class ClienteSobreSimulador(OdooClient):
    """OdooClient real (search_read_all / search_read_por_ids) con el transporte sustituido por el simulador."""

    def __init__(self, sim):
        super().__init__(url="http://sim", dbname="sim", user="u", api_key="k")
        self.sim = sim
        self.llamadas: list[tuple[str, str, int]] = []
        self.hilos: set[str] = set()
        self._l = threading.Lock()

    def login(self, forzar=False):
        return 2

    def execute(self, modelo, metodo, args=None, **kw):
        # n = registros que la llamada lee de verdad: ids en «read», ids del dominio «id in […]» en search_read
        # (0 si sólo pide write_date: eso es una comprobación barata, no una lectura), 1 para búsquedas.
        n = 0
        if metodo == "read":
            n = len(args[0])
        elif metodo == "search_read":
            dom = args[0] if args else []
            campos = tuple(kw.get("fields") or ())
            if campos != ("write_date",):
                n = len(dom[0][2]) if dom and isinstance(dom[0], list) and dom[0][0] in ("id", "picking_id") and dom[0][1] == "in" else 1
        elif metodo == "search":
            n = 1
        with self._l:
            self.llamadas.append((modelo, metodo, n))
            self.hilos.add(threading.current_thread().name)
        return self.sim.execute(modelo, metodo, args, **kw)


def _serie(sim, modelo, dominio, campos, pagina):
    out, offset = [], 0
    while True:
        lote = sim.search_read(modelo, dominio, campos, limite=pagina, orden="id", offset=offset)
        out.extend(lote)
        if len(lote) < pagina:
            return out
        offset += pagina


def test_search_read_all_paralelo_equivale_a_paginar_en_serie(sim):
    cli = ClienteSobreSimulador(sim)
    modelo, campos = "stock.move.line", ["product_id", "quantity", "date", "location_id"]
    dominio = [["state", "=", "done"]]
    esperado = _serie(sim, modelo, dominio, campos, 500)
    assert len(esperado) > 1000, "el simulador debe tener suficientes líneas para varios bloques"
    obtenido = cli.search_read_all(modelo, dominio, campos, pagina=300)
    assert [r["id"] for r in obtenido] == [r["id"] for r in esperado]
    assert obtenido == esperado
    # una búsqueda de ids + una lectura por bloque, repartidas en varios hilos
    assert [c for c in cli.llamadas if c[1] == "search"] == [(modelo, "search", 1)]
    lecturas = [c for c in cli.llamadas if c[1] == "read"]
    assert len(lecturas) == -(-len(esperado) // 300) and all(n <= 300 for _, _, n in lecturas)
    if settings.ODOO_LECTURAS_PARALELAS > 1:
        assert len(cli.hilos) > 1


def test_search_read_por_ids_en_bloques(sim):
    cli = ClienteSobreSimulador(sim)
    ids = [r["id"] for r in sim.search_read("product.product", [], ["id"], limite=0)]
    filas = cli.search_read_por_ids("product.product", ids, ["standard_price"], bloque=7)
    assert sorted(r["id"] for r in filas) == sorted(ids)
    assert sum(1 for c in cli.llamadas if c[1] == "search_read") == -(-len(ids) // 7)
    # con dominio extra: sólo los que cumplen
    filas = cli.search_read_por_ids("product.product", ids, ["standard_price"], dominio_extra=[["standard_price", ">", 0]], bloque=7)
    assert all(float(r["standard_price"]) > 0 for r in filas)


def test_consumo_igual_con_lecturas_paralelas(sim):
    """La lectura de consumo (líneas + cabeceras + lotes + costo) da lo mismo con 1 o con 4 conexiones."""
    from app.odoo import queries, client as C
    original = C.get_client()
    try:
        cli = ClienteSobreSimulador(sim)
        C.set_client(cli)
        antes = settings.ODOO_LECTURAS_PARALELAS
        settings.ODOO_LECTURAS_PARALELAS = 1
        a = queries.consumo(dias=90, cli=cli)
        settings.ODOO_LECTURAS_PARALELAS = 4
        b = queries.consumo(dias=90, cli=cli)
        settings.ODOO_LECTURAS_PARALELAS = antes
        assert len(a) == len(b) and len(a) > 0
        cols = [c for c in ("producto", "cantidad", "hospital", "lote", "importe") if c in a.columns]
        assert a[cols].reset_index(drop=True).equals(b[cols].reset_index(drop=True))
    finally:
        C.set_client(original)
