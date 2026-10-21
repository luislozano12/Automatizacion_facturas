#!/usr/bin/env python3
"""
El extra: un chat en el navegador (http://localhost:5000).

- Lo del reto, con botones que no usan IA: cargar los XML, detalle, resumen por proveedor, incidencias y Excel.
- Además: preguntas en lenguaje natural que el modelo convierte a SQL (se muestra el SQL), y PDF de las facturas.

Uso:
  python bot.py

Al arrancar carga los XML de cfdis/, así que no hace falta correr antes reto.py.
Para las preguntas libres: copia .env.example como .env y pon OPENAI_API_KEY.
"""
import sys

from revision.base import cargar


def main():
    sys.stdout.reconfigure(errors="replace")  # que un carácter raro no truene en la consola de Windows
    try:
        cargar()
    except FileNotFoundError as e:
        sys.exit(str(e))
    from asistente.web import abrir  # después de cargar: así el encabezado de la página ya trae las cifras
    print("\nChat en http://localhost:5000  (Ctrl+C para salir)")
    abrir()


if __name__ == "__main__":
    main()
