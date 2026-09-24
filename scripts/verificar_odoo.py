#!/usr/bin/env python3
"""Verifica la conexión a Odoo y muestra el mapeo descubierto. Uso:

    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_API_KEY=... python scripts/verificar_odoo.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", str(Path.cwd() / "data"))

from app import db  # noqa: E402
from app.odoo import queries, schema  # noqa: E402
from app.odoo.client import get_client  # noqa: E402

db.init_db()
cli = get_client()
print("▶ Probando conexión…")
r = cli.probar()
print(json.dumps(r, ensure_ascii=False, indent=2))
if not r.get("ok"):
    sys.exit(1)
print("\n▶ Descubriendo modelos y campos…")
m = schema.descubrir(cli)
for ent, v in m["entidades"].items():
    print(f"  {ent:24s} → {v.get('modelo') or '—':34s} [{v.get('confianza')}]")
for a in m.get("avisos", []):
    print("  ⚠", a)
print("\n▶ Muestra de consumo (últimos 7 días)…")
df = queries.consumo(dias=7, tope=2000)
print(f"  {len(df)} líneas · origen {df['origen_datos'].iloc[0] if len(df) else '—'}")
if len(df):
    print(df[["fecha", "folio", "hospital", "producto", "cantidad", "unidad"]].head(5).to_string(index=False))
print("\n▶ Existencias…")
ex = queries.existencias()
print(f"  {len(ex)} quants en {ex['ubicacion'].nunique() if len(ex) else 0} ubicaciones")
print("\nListo. Si algún campo salió «no_encontrado», ajústalo en Configuración ▸ Mapeo (o con schema.sobreescribir).")
