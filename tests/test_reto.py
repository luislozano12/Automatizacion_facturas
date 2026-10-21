"""Pruebas de lo que pide el reto (reto.py y la carpeta revision/), con los XML de cfdis/."""
from pathlib import Path

import pytest
from openpyxl import load_workbook

import reto
from revision import config, reportes
from revision.base import cargar
from revision.lectura import Invalido, leer_cfdi


def test_extrae_los_campos():
    factura, conceptos, avisos = leer_cfdi(config.CFDIS / "factura_01.xml")
    assert factura["uuid"] == "A5AEC797-8306-D03B-F38B-2FFC80A4DF5A"
    assert (factura["emisor_rfc"], factura["emisor_nombre"]) == ("PAP180312KL4", "PAPELERIA DEL BAJIO DEMO")
    assert factura["fecha"] == "2026-09-28T09:56:00"
    assert (factura["subtotal"], factura["iva"], factura["total"]) == (6466.50, 1034.64, 7501.14)
    assert len(conceptos) == 1 and avisos == []


@pytest.mark.parametrize("archivo, tipo", [("factura_19.xml", "XML dañado"), ("factura_20.xml", "No es CFDI")])
def test_archivos_invalidos(archivo, tipo):
    with pytest.raises(Invalido) as e:
        leer_cfdi(config.CFDIS / archivo)
    assert e.value.tipo == tipo


def test_carga_sin_detenerse(salida, consulta):
    assert consulta("SELECT COUNT(*) FROM facturas") == [(14,)]
    errores = {(Path(a).name, t) for a, t in consulta("SELECT archivo, tipo FROM incidencias WHERE nivel='ERROR'")}
    assert errores == {("factura_04.xml", "Duplicada"), ("factura_11.xml", "Duplicada"),
                       ("factura_19.xml", "XML dañado"), ("factura_20.xml", "No es CFDI")}
    log = (salida / "incidencias.log").read_text(encoding="utf-8")
    assert "factura_19.xml | XML dañado" in log and "Duplicada" in log


def test_cargar_dos_veces_da_lo_mismo(consulta):
    antes = consulta("SELECT * FROM facturas ORDER BY uuid"), consulta("SELECT archivo, tipo FROM incidencias ORDER BY 1, 2")
    cargar(config.CFDIS)
    despues = consulta("SELECT * FROM facturas ORDER BY uuid"), consulta("SELECT archivo, tipo FROM incidencias ORDER BY 1, 2")
    assert antes == despues


def test_resumen_de_mayor_a_menor(salida):
    _, filas = reportes.resumen()
    assert [f[0] for f in filas][:2] == ["PAP180312KL4", "ALI210622RT5"]
    assert [f[5] for f in filas] == sorted((f[5] for f in filas), reverse=True)
    pap = filas[0]
    assert pap[2] == 4 and pap[5] == 107825.92  # el duplicado (factura_04) no suma


def test_reto_genera_excel_y_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SALIDA", tmp_path)
    monkeypatch.setattr(config, "DB", tmp_path / "facturas.db")
    reto.main([])
    wb = load_workbook(tmp_path / "reporte_cfdi.xlsx")
    assert wb.sheetnames == ["Detalle", "Resumen por proveedor"]
    assert wb["Detalle"].max_row == 15  # encabezado + 14 facturas
    assert wb["Resumen por proveedor"]["A2"].value == "PAP180312KL4"
    assert "Duplicada" in (tmp_path / "incidencias.log").read_text(encoding="utf-8")


def test_reto_sin_carpeta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SALIDA", tmp_path)
    monkeypatch.setattr(config, "DB", tmp_path / "facturas.db")
    with pytest.raises(SystemExit, match="No existe la carpeta"):
        reto.main([str(tmp_path / "no-existe")])
