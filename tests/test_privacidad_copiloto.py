"""v1.3.10 · El copiloto nunca saca datos de pacientes ni datos sensibles de Odoo, ni a petición del usuario."""
from __future__ import annotations

import pytest

from app.llm import tools as T


def _modelo_folio(sim):
    for m in ("cbh.operacion.medica", "cbh.medical.service.request"):
        if sim.tablas.get(m):
            return m
    pytest.skip("sin modelo de folio en el simulador")


def test_campos_de_paciente_se_omiten_y_sin_campos_no_sale_el_registro_completo(sim):
    m = _modelo_folio(sim)
    # pide explícitamente paciente y NSS → se omiten y se avisa
    r = T.t_consultar_odoo({"modelo": m, "campos": ["name", "paciente", "patient_name", "patient_nss", "diagnosis"], "limite": 3}, "u", "admin")
    assert r["filas"] and all(set(f.keys()) <= {"id", "name"} for f in r["filas"])
    assert set(r["campos_omitidos"]) == {"paciente", "patient_name", "patient_nss", "diagnosis"}
    # sin campos: sólo display_name (nunca el registro completo)
    r = T.t_consultar_odoo({"modelo": m, "limite": 2}, "u", "admin")
    assert r["filas"] and all(set(f.keys()) <= {"id", "display_name"} for f in r["filas"])
    # tampoco se puede filtrar por un dato de paciente
    with pytest.raises(ValueError):
        T.t_consultar_odoo({"modelo": m, "dominio": [["patient_name", "ilike", "juan"]], "campos": ["name"]}, "u", "admin")
    # ni agrupar por él
    r = T.t_consultar_odoo({"modelo": m, "agrupar_por": ["patient_gender", "hospital_id"], "limite": 5}, "u", "admin")
    assert r["agrupado_por"] == ["hospital_id"]


def test_modelos_sensibles_vetados(sim):
    for modelo in ("res.users", "hr.payslip", "res.partner.bank", "ir.attachment", "mail.message"):
        with pytest.raises(ValueError):
            T.t_consultar_odoo({"modelo": modelo, "campos": ["name"]}, "u", "admin")
