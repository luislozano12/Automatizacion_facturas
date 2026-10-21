"""Rutas del proyecto. Las pruebas (tests/) las cambian para trabajar en una carpeta temporal."""
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
CFDIS = RAIZ / "cfdis"    # XML de entrada
SALIDA = RAIZ / "salida"  # Excel, log de incidencias, PDF y la base
DB = SALIDA / "facturas.db"
