import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cbh-test-"))
os.environ["DEMO_MODE"] = "true"
os.environ["ADMIN_PASSWORD"] = "test1234"
os.environ["APP_ENV"] = "development"
os.environ["SCHEDULE_ENABLED"] = "false"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.odoo import client, schema  # noqa: E402
from app.odoo.simulado import OdooSimulado  # noqa: E402


@pytest.fixture(scope="session")
def sim():
    s = OdooSimulado()
    client.set_client(s)
    db.init_db()
    schema.descubrir(s)
    return s
