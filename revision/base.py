"""La base SQLite: tablas, carga de una carpeta de XML y registro (log) de incidencias."""
import logging
import sqlite3
import sys
from pathlib import Path

from . import config
from .lectura import Invalido, leer_cfdi

SIN_DATOS = "No hay facturas en la base: carga primero los XML de la carpeta cfdis."

log = logging.getLogger("cfdi")


def conectar():
    config.DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS facturas(
            uuid TEXT PRIMARY KEY, serie TEXT, folio TEXT, fecha TEXT, tipo TEXT, moneda TEXT,
            subtotal REAL, descuento REAL, iva REAL, retenciones REAL, total REAL,
            emisor_rfc TEXT, emisor_nombre TEXT, receptor_rfc TEXT, receptor_nombre TEXT, archivo TEXT);
        CREATE TABLE IF NOT EXISTS conceptos(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT REFERENCES facturas(uuid), clave_prod TEXT,
            descripcion TEXT, cantidad REAL, unidad TEXT, valor_unitario REAL, importe REAL);
        -- nivel: 'ERROR' = el archivo no se cargó; 'AVISO' = se cargó pero conviene revisarlo
        CREATE TABLE IF NOT EXISTS incidencias(
            id INTEGER PRIMARY KEY AUTOINCREMENT, archivo TEXT, nivel TEXT, tipo TEXT, detalle TEXT, uuid TEXT);
        CREATE INDEX IF NOT EXISTS ix_emisor ON facturas(emisor_rfc);
        CREATE INDEX IF NOT EXISTS ix_receptor ON facturas(receptor_rfc);
        CREATE INDEX IF NOT EXISTS ix_con ON conceptos(uuid);
        """
    )
    return con


def leer(sql, params=()):
    con = conectar()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def abrir_log(salida):
    """El log de la corrida: se reescribe en cada carga y también se muestra en la consola."""
    salida.mkdir(parents=True, exist_ok=True)
    log.handlers.clear()
    log.setLevel(logging.INFO)
    logging.addLevelName(logging.WARNING, "AVISO")  # mismo nombre que en la tabla incidencias
    archivo = logging.FileHandler(salida / "incidencias.log", mode="w", encoding="utf-8")
    archivo.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(logging.Formatter("  %(levelname)-7s %(message)s"))
    log.addHandler(archivo)
    log.addHandler(consola)
    return archivo


def ruta_guardada(xml):
    """Cómo se guarda la ruta de un XML en la base: relativa al proyecto si está dentro de él (cfdis/factura_01.xml).

    Así la base no lleva rutas personales (C:/Users/...), que la IA vería y mandaría a OpenAI, y sigue sirviendo
    si se mueve la carpeta del proyecto.
    """
    ruta = Path(xml).resolve()
    try:
        return ruta.relative_to(config.RAIZ).as_posix()
    except ValueError:
        return ruta.as_posix()


def ruta_real(guardada):
    """La ruta en disco de un XML guardado con ruta_guardada()."""
    return config.RAIZ / guardada


def incidencia(con, ruta, nivel, tipo, detalle, uuid=None):
    con.execute("INSERT INTO incidencias(archivo, nivel, tipo, detalle, uuid) VALUES(?,?,?,?,?)",
                (ruta, nivel, tipo, detalle, uuid))
    log.log(logging.ERROR if nivel == "ERROR" else logging.WARNING, f"{Path(ruta).name} | {tipo} | {detalle}")


def guardar(con, factura, conceptos, ruta):
    fila = {**factura, "archivo": ruta}
    con.execute(f"INSERT INTO facturas({','.join(fila)}) VALUES({','.join('?' * len(fila))})", list(fila.values()))
    con.executemany("INSERT INTO conceptos(uuid, clave_prod, descripcion, cantidad, unidad, valor_unitario, importe)"
                    " VALUES(?,?,?,?,?,?,?)", [(factura["uuid"],) + c for c in conceptos])


def cargar(carpeta=None, salida=None):
    """Lee los XML de la carpeta y devuelve los conteos de la carga.

    Cada archivo se procesa de nuevo, así que correrlo dos veces da lo mismo.
    """
    carpeta = Path(carpeta or config.CFDIS)
    salida = Path(salida or config.SALIDA)
    if not carpeta.is_dir():
        raise FileNotFoundError(f"No existe la carpeta {carpeta}")
    archivo_log = abrir_log(salida)
    archivos = sorted(p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() == ".xml")
    log.info(f"Carpeta {carpeta.name}: {len(archivos)} archivos XML")
    con = conectar()
    cargadas = errores = avisos = 0
    for xml in archivos:
        ruta = ruta_guardada(xml)
        # lo que se haya cargado antes de este archivo se reemplaza por lo que diga ahora
        con.execute("DELETE FROM conceptos WHERE uuid IN (SELECT uuid FROM facturas WHERE archivo=?)", (ruta,))
        con.execute("DELETE FROM facturas WHERE archivo=?", (ruta,))
        con.execute("DELETE FROM incidencias WHERE archivo=?", (ruta,))
        try:
            factura, conceptos, pendientes = leer_cfdi(xml)
        except Invalido as e:
            incidencia(con, ruta, "ERROR", e.tipo, str(e))
            errores += 1
            continue
        except Exception as e:  # cualquier otro problema con un archivo no debe detener el lote
            incidencia(con, ruta, "ERROR", "Error inesperado", f"{type(e).__name__}: {e}")
            errores += 1
            continue
        previa = con.execute("SELECT archivo FROM facturas WHERE uuid=?", (factura["uuid"],)).fetchone()
        if previa:  # se queda la primera copia (por orden de nombre de archivo); las demás no suman
            incidencia(con, ruta, "ERROR", "Duplicada", f"El UUID {factura['uuid']} ya se cargó desde "
                                                       f"{Path(previa[0]).name}", factura["uuid"])
            errores += 1
            continue
        guardar(con, factura, conceptos, ruta)
        for tipo, detalle in pendientes:
            incidencia(con, ruta, "AVISO", tipo, detalle, factura["uuid"])
            avisos += 1
        cargadas += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM facturas").fetchone()[0]
    con.close()
    log.info(f"Cargadas: {cargadas} | Con error (no cargadas): {errores} | Avisos: {avisos} | "
             f"Facturas en la base: {total}")
    log.removeHandler(archivo_log)
    archivo_log.close()
    return {"archivos": len(archivos), "cargadas": cargadas, "errores": errores, "avisos": avisos, "en_base": total,
            "log": salida / "incidencias.log"}
