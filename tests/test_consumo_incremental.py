"""v1.3.10 · Memoria local del consumo: la primera lectura baja todo; las siguientes sólo lo nuevo o cambiado, con el
mismo resultado que una lectura completa."""
from __future__ import annotations

import pandas as pd

from app.config import settings
from app.odoo import memoria_consumo as mc, queries
from tests.test_lectura_paralela import ClienteSobreSimulador


def _lecturas(cli, modelo):
    return [c for c in cli.llamadas if c[0] == modelo and c[1] in ("read", "search_read")]


def _comparables(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in ("folio", "producto", "cantidad", "hospital", "medico", "auxiliar", "lote", "importe", "fecha") if c in df.columns]
    return df.sort_values(["folio", "producto", "cantidad"] if "folio" in cols else cols).reset_index(drop=True)[cols]


def test_incremental_igual_que_completa_y_relee_solo_lo_cambiado(sim):
    mc.borrar()
    queries._CONSUMO_CACHE.clear()
    modelo = "cbh.operacion.medica.line"
    cli = ClienteSobreSimulador(sim)
    # 1) primera lectura: completa, deja memoria en disco
    a = queries.consumo(dias=120, cli=cli)
    assert len(a) > 100
    assert mc.estado()["memorias"] and mc.estado()["memorias"][0]["lineas"] >= len(a)
    n_lecturas_completa = sum(n for _, _, n in _lecturas(cli, modelo))
    # 2) segunda lectura sin cambios: mismo resultado, sin releer líneas (sólo búsquedas de ids)
    cli.llamadas.clear()
    b = queries.consumo(dias=120, cli=cli)
    assert _comparables(a).equals(_comparables(b))
    assert sum(n for _, _, n in _lecturas(cli, modelo)) == 0
    assert any(c[1] == "search" for c in cli.llamadas)
    # 3) cambia UNA línea en Odoo (cantidad) y una cabecera (técnico): sólo eso se relee y el resultado lo refleja
    en_ventana = set(int(x) for x in a["id"].tolist()) if "id" in a.columns else set()
    lin = next(r for r in sim.tablas[modelo] if r["id"] in en_ventana)
    campo_qty = next(k for k in ("qty_used", "quantity", "cantidad", "product_uom_qty") if k in lin)
    nueva_qty = float(lin[campo_qty] or 0) + 7.0
    sim.write(modelo, [lin["id"]], {campo_qty: nueva_qty})
    cli.llamadas.clear()
    c = queries.consumo(dias=120, cli=cli)
    leidas = sum(n for _, _, n in _lecturas(cli, modelo))
    assert 1 <= leidas < max(10, n_lecturas_completa // 10), f"releyó {leidas} líneas; debía releer casi sólo la cambiada"
    fila = c[c["cantidad"] == nueva_qty]
    assert not fila.empty
    # 4) comparado con una lectura completa forzada (memoria desactivada): idéntico
    settings.CONSUMO_INCREMENTAL = False
    try:
        completa = queries.consumo(dias=120, cli=cli)
    finally:
        settings.CONSUMO_INCREMENTAL = True
    assert _comparables(c).equals(_comparables(completa))
    # 5) una línea nueva en Odoo aparece en la siguiente lectura
    antes = len(c)
    nueva = {k: v for k, v in lin.items() if k not in ("id", "write_date")}
    sim.tablas[modelo].append({**nueva, "id": sim._id(), "write_date": "2030-01-01 00:00:00"})
    d = queries.consumo(dias=120, cli=cli)
    assert len(d) == antes + 1
    # 6) releer completo borra la memoria
    assert mc.borrar() >= 1 and not mc.estado()["memorias"]
