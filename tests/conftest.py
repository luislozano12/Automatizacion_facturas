"""Lo que comparten las pruebas: cada una trabaja con una base y una carpeta de salida temporales."""
import sqlite3

import pytest

from revision import config
from revision.base import cargar


@pytest.fixture
def salida(tmp_path, monkeypatch):
    """Carpeta de salida temporal con los XML de cfdis/ ya cargados."""
    monkeypatch.setattr(config, "SALIDA", tmp_path)
    monkeypatch.setattr(config, "DB", tmp_path / "facturas.db")
    cargar(config.CFDIS)
    return tmp_path


@pytest.fixture
def dos_clientes(salida):
    """Además de cfdis/, los XML de ejemplos/otro_cliente/: dos facturas de otro receptor y un duplicado."""
    cargar(config.RAIZ / "ejemplos" / "otro_cliente")
    return salida


@pytest.fixture
def consulta(salida):
    """Función que ejecuta un SQL sobre la base temporal y devuelve las filas."""
    def ejecutar(sql):
        con = sqlite3.connect(config.DB)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    return ejecutar
