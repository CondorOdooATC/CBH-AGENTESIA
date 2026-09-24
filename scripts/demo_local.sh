#!/usr/bin/env bash
# Levanta la plataforma en modo DEMO (Odoo simulado) en http://localhost:8000 — usuario admin / demo1234
set -e
cd "$(dirname "$0")/.."
export DEMO_MODE=true ADMIN_PASSWORD=${ADMIN_PASSWORD:-demo1234} DATA_DIR=${DATA_DIR:-$PWD/data} APP_ENV=development SCHEDULE_ENABLED=false
python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --reload
