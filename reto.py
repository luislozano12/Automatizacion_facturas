#!/usr/bin/env python3
"""
Lo que pide el reto: lee la carpeta de CFDI, extrae los datos de cada factura, detecta duplicados y archivos
dañados sin detenerse, y genera el Excel ("Detalle" y "Resumen por proveedor") y el log de incidencias.

Uso:
  python reto.py            # lee cfdis/
  python reto.py OTRA_CARPETA

Resultado en salida/: reporte_cfdi.xlsx, incidencias.log y facturas.db (la base que también usa bot.py).
"""
import argparse
import sys

from revision import config
from revision.base import cargar
from revision.reportes import excel


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # que un carácter raro no truene en la consola de Windows
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("carpeta", nargs="?", default=config.CFDIS, help="carpeta con los XML (por omisión cfdis/)")
    a = p.parse_args(argv)
    try:
        carga = cargar(a.carpeta)
        # el Excel lleva solo lo de esta carpeta, aunque la base tenga además lo que se subió desde el bot
        destino = excel(carpeta=a.carpeta)
    except FileNotFoundError as e:
        sys.exit(str(e))
    except ValueError as e:  # ninguna factura válida
        sys.exit(str(e))
    except PermissionError:
        sys.exit("No se pudo escribir el Excel: probablemente está abierto. Ciérralo y vuelve a intentar.")
    print(f"\nExcel: {destino}\nLog:   {carga['log']}")


if __name__ == "__main__":
    main()
