"""Pruebas del evaluador de la IA (evals/correr.py), sin llamar a OpenAI."""
import importlib.util
import json

import pytest

from revision import config

spec = importlib.util.spec_from_file_location("correr", config.RAIZ / "evals" / "correr.py")
correr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(correr)


@pytest.mark.parametrize("esperado, respuesta, ok", [
    ("107825.92", "Te facturó **$107,825.92** en 4 facturas.", True),
    ("papeleria del bajio", "Fue PAPELERÍA DEL BAJÍO DEMO.", True),
    ("3", "Te mandó 3 facturas.", True),
    ("3", "La última fue el 2026-09-30.", False),     # el 3 de una fecha no cuenta
    ("52399.6", "Total $52,399.65", False),            # una cifra cortada no cuenta
    ("372820.96", "El total es $372,820.96.", True),   # el punto final no estorba
])
def test_aparece(esperado, respuesta, ok):
    assert correr.aparece(esperado, respuesta) is ok


def test_revisar():
    caso = {"contiene": ["52399.65"], "no_contiene": ["320421.31"], "pdf": ["UUID-1"]}
    assert correr.revisar(caso, "Son $52,399.65", [{"tipo": "pdf", "uuid": "UUID-1"}]) == []
    fallas = correr.revisar(caso, "Son $320,421.31", [])
    assert fallas == ["falta «52399.65»", "no debía decir «320421.31»", "no adjuntó el PDF UUID-1"]


def test_casos_bien_formados():
    casos = json.loads(correr.CASOS.read_text(encoding="utf-8"))
    assert len({c["id"] for c in casos}) == len(casos) >= 15
    for c in casos:
        assert c["pregunta"] and set(c) <= {"id", "pregunta", "cliente", "contiene", "no_contiene", "pdf"}
        assert c.get("contiene") or c.get("no_contiene") or c.get("pdf")  # cada caso revisa algo


def test_cifras_esperadas_salen_de_los_datos(monkeypatch, tmp_path):
    # las cifras de casos.json deben ser las que da la base que arma el evaluador
    monkeypatch.setattr(config, "SALIDA", tmp_path)
    monkeypatch.setattr(config, "DB", tmp_path / "x.db")
    correr.preparar_base()
    from revision.base import leer
    assert leer("SELECT COUNT(*), ROUND(SUM(total), 2), ROUND(SUM(iva), 2) FROM facturas") == [(16, 372820.96, 40516.17)]
    assert leer("SELECT ROUND(SUM(total), 2) FROM facturas WHERE receptor_rfc='OCD200101XY9'") == [(52399.65,)]
    assert leer("SELECT uuid FROM facturas ORDER BY total DESC LIMIT 1") == [("7AAC319F-FB75-9E0F-ED1D-9D1690624FE3",)]
