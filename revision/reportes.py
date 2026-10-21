"""Los reportes: detalle, resumen por proveedor, resumen por cliente e incidencias, y el Excel que los junta.

Cada reporte devuelve (columnas, filas); los usan el Excel y el bot. `cliente` (RFC receptor) limita el reporte
a las facturas de ese cliente de KMG; sin él se incluyen todas.
"""
from datetime import datetime
from pathlib import Path

from . import config
from .base import SIN_DATOS, leer, ruta_guardada

# solo facturas de ingreso: una nota de crédito (E) o un complemento de pago (P) no es "facturado"
INGRESO = "tipo='I'"


def donde(cliente, *condiciones):
    """WHERE y parámetros con las condiciones dadas y, si hay cliente, el filtro por receptor."""
    condiciones = list(condiciones) + (["receptor_rfc=?"] if cliente else [])
    return ("WHERE " + " AND ".join(condiciones) if condiciones else ""), ((cliente,) if cliente else ())


def detalle(cliente=None):
    """Una fila por factura."""
    w, p = donde(cliente)
    filas = leer("SELECT uuid, emisor_rfc, emisor_nombre, fecha, subtotal, iva, total, receptor_rfc, receptor_nombre, "
                 f"serie, folio, moneda, archivo FROM facturas {w} ORDER BY fecha, uuid", p)
    return (["UUID", "RFC emisor", "Nombre emisor", "Fecha", "Subtotal", "IVA trasladado", "Total",
             "RFC receptor", "Nombre receptor", "Serie", "Folio", "Moneda", "Archivo"],
            [(*f[:12], Path(f[12]).name) for f in filas])


def resumen(cliente=None):
    """Total facturado e IVA por RFC emisor, de mayor a menor."""
    w, p = donde(cliente, INGRESO)
    return (["RFC emisor", "Nombre emisor", "Facturas", "Subtotal", "IVA trasladado", "Total facturado"],
            leer("SELECT emisor_rfc, MAX(emisor_nombre), COUNT(*), ROUND(SUM(subtotal), 2), ROUND(SUM(iva), 2), "
                 f"ROUND(SUM(total), 2) FROM facturas {w} GROUP BY emisor_rfc ORDER BY 6 DESC", p))


def resumen_clientes():
    """Por cada cliente de KMG (RFC receptor): facturas recibidas, proveedores, IVA y total, de mayor a menor."""
    return (["RFC cliente", "Nombre cliente", "Facturas", "Proveedores", "IVA trasladado", "Total facturado"],
            leer("SELECT receptor_rfc, MAX(receptor_nombre), COUNT(*), COUNT(DISTINCT emisor_rfc), "
                 f"ROUND(SUM(iva), 2), ROUND(SUM(total), 2) FROM facturas WHERE {INGRESO} "
                 "GROUP BY receptor_rfc ORDER BY 6 DESC"))


def incidencias(cliente=None, carpeta=None, sin_dueno=True):
    """Archivos que no se cargaron (ERROR) y facturas cargadas que conviene revisar (AVISO).

    Con cliente: las de sus facturas y, si `sin_dueno`, también las de archivos que no se pudieron leer, que no se
    sabe de quién son. Con carpeta: solo las de los archivos de esa carpeta (las de una carga).
    """
    condiciones, p = [], []
    if cliente:
        suyas = "uuid IN (SELECT uuid FROM facturas WHERE receptor_rfc=?)"
        condiciones.append(f"(uuid IS NULL OR {suyas})" if sin_dueno else suyas)
        p.append(cliente)
    if carpeta:
        prefijo = ruta_guardada(carpeta) + "/"
        condiciones.append("substr(archivo, 1, ?) = ?")
        p += [len(prefijo), prefijo]
    filtro = "WHERE " + " AND ".join(condiciones) if condiciones else ""
    filas = leer(f"SELECT archivo, nivel, tipo, detalle FROM incidencias {filtro} ORDER BY nivel DESC, archivo", p)
    return ["Archivo", "Nivel", "Tipo", "Detalle"], [(Path(f[0]).name, *f[1:]) for f in filas]  # ERROR antes que AVISO


def clientes():
    """[(rfc, nombre, facturas)] de los clientes con facturas en la base, el de más facturas primero."""
    return leer("SELECT receptor_rfc, MAX(receptor_nombre), COUNT(*) FROM facturas WHERE receptor_rfc IS NOT NULL "
                "GROUP BY receptor_rfc ORDER BY 3 DESC, 2")


# --- Excel

def libro(cliente=None):
    """El Excel de revisión (hojas "Detalle" y "Resumen por proveedor"), o None si no hay facturas."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    columnas, facturas = detalle(cliente)
    if not facturas:
        return None
    wb = Workbook()
    ws = wb.active
    ws.title = "Detalle"
    ws.append(columnas)
    for f in facturas:
        f = list(f)
        try:
            f[3] = datetime.fromisoformat(f[3])  # la fecha como fecha de Excel
        except ValueError:
            pass  # se deja el texto tal como venía en el XML
        ws.append(f)
    formato(ws, dinero=(4, 5, 6), fecha=3)

    w2 = wb.create_sheet("Resumen por proveedor")
    columnas, proveedores = resumen(cliente)
    w2.append(columnas)
    for fila in proveedores:
        w2.append(list(fila))
    n = len(proveedores) + 1
    w2.append(["TOTAL", None, f"=SUM(C2:C{n})", f"=SUM(D2:D{n})", f"=SUM(E2:E{n})", f"=SUM(F2:F{n})"])
    formato(w2, dinero=(3, 4, 5), filtro=False)
    for c in w2[n + 1]:
        c.font = Font(bold=True)
    return wb


def formato(ws, dinero=(), fecha=None, filtro=True):
    """Encabezado en negritas, primera fila fija, formato de moneda y fecha, y ancho de columnas."""
    from openpyxl.styles import Font, PatternFill

    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9D9D9")
    ws.freeze_panes = "A2"
    if filtro:
        ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(ws.columns):
        for c in col[1:]:
            if i in dinero:
                c.number_format = '"$"#,##0.00'
            elif i == fecha:
                c.number_format = "yyyy-mm-dd hh:mm"
        ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 3, 50)


def excel(destino=None, cliente=None):
    """Guarda el Excel y devuelve su ruta. Lanza ValueError si no hay facturas y PermissionError si está abierto."""
    destino = Path(destino or config.SALIDA / "reporte_cfdi.xlsx")
    wb = libro(cliente)
    if not wb:
        raise ValueError(SIN_DATOS)
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino
