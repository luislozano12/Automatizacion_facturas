"""Los reportes del reto: detalle, resumen por proveedor e incidencias, y el Excel que los junta.

Cada reporte devuelve (columnas, filas); los usan el Excel y el bot.
"""
from datetime import datetime
from pathlib import Path

from . import config
from .base import SIN_DATOS, leer

DETALLE = ("SELECT uuid, emisor_rfc, emisor_nombre, fecha, subtotal, iva, total, serie, folio, moneda, archivo "
           "FROM facturas ORDER BY fecha, uuid")
# solo facturas de ingreso: una nota de crédito (E) o un complemento de pago (P) no es "facturado"
RESUMEN = ("SELECT emisor_rfc, MAX(emisor_nombre), COUNT(*), ROUND(SUM(subtotal), 2), ROUND(SUM(iva), 2), "
           "ROUND(SUM(total), 2) FROM facturas WHERE tipo='I' GROUP BY emisor_rfc ORDER BY 6 DESC")
INCIDENCIAS = "SELECT archivo, nivel, tipo, detalle FROM incidencias ORDER BY nivel DESC, archivo"  # ERROR antes que AVISO


def detalle():
    """Una fila por factura."""
    filas = [(*f[:10], Path(f[10]).name) for f in leer(DETALLE)]
    return (["UUID", "RFC emisor", "Nombre emisor", "Fecha", "Subtotal", "IVA trasladado", "Total",
             "Serie", "Folio", "Moneda", "Archivo"], filas)


def resumen():
    """Total facturado e IVA por RFC emisor, de mayor a menor."""
    return ["RFC emisor", "Nombre emisor", "Facturas", "Subtotal", "IVA trasladado", "Total facturado"], leer(RESUMEN)


def incidencias():
    """Archivos que no se cargaron (ERROR) y facturas cargadas que conviene revisar (AVISO)."""
    return ["Archivo", "Nivel", "Tipo", "Detalle"], [(Path(f[0]).name, *f[1:]) for f in leer(INCIDENCIAS)]


def libro():
    """El Excel de revisión (hojas "Detalle" y "Resumen por proveedor"), o None si la base está vacía."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    columnas, facturas = detalle()
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
    columnas, proveedores = resumen()
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


def excel(destino=None):
    """Guarda el Excel y devuelve su ruta. Lanza ValueError si no hay facturas y PermissionError si está abierto."""
    destino = Path(destino or config.SALIDA / "reporte_cfdi.xlsx")
    wb = libro()
    if not wb:
        raise ValueError(SIN_DATOS)
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino
