"""
Genera la representación impresa (PDF) de un CFDI 3.3 / 4.0 a partir de su XML.

Lo usa el bot. Es una representación impresa: no sustituye al XML timbrado.
Requiere reportlab:  pip install reportlab
"""
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from revision import config
from revision.base import leer, ruta_real

TIPOS = {"I": "FACTURA", "E": "NOTA DE CRÉDITO", "P": "COMPLEMENTO DE PAGO", "T": "TRASLADO", "N": "NÓMINA"}
IMPUESTOS = {"001": "ISR", "002": "IVA", "003": "IEPS"}
FORMAS_PAGO = {
    "01": "Efectivo", "02": "Cheque nominativo", "03": "Transferencia electrónica de fondos",
    "04": "Tarjeta de crédito", "17": "Compensación", "28": "Tarjeta de débito", "99": "Por definir",
}
METODOS_PAGO = {"PUE": "Pago en una sola exhibición", "PPD": "Pago en parcialidades o diferido"}
USOS = {
    "G01": "Adquisición de mercancías", "G02": "Devoluciones, descuentos o bonificaciones",
    "G03": "Gastos en general", "I01": "Construcciones", "I04": "Equipo de cómputo y accesorios",
    "P01": "Por definir", "S01": "Sin efectos fiscales", "CP01": "Pagos",
}
REGIMENES = {
    "601": "General de Ley Personas Morales", "603": "Personas Morales con Fines no Lucrativos",
    "605": "Sueldos y Salarios", "606": "Arrendamiento", "612": "Personas Físicas con Actividades Empresariales y Profesionales",
    "616": "Sin obligaciones fiscales", "621": "Incorporación Fiscal", "626": "Régimen Simplificado de Confianza",
}
RELACIONES = {
    "01": "Nota de crédito de los documentos relacionados", "02": "Nota de débito de los documentos relacionados",
    "03": "Devolución de mercancía", "04": "Sustitución de los CFDI previos", "07": "Aplicación de anticipo",
}

ANCHO = letter[0] - 20 * mm  # ancho útil con márgenes de 10 mm
GRIS = colors.HexColor("#444444")
FONDO = colors.HexColor("#e8e8e8")

N = ParagraphStyle("n", fontName="Helvetica", fontSize=8, leading=10)
B = ParagraphStyle("b", parent=N, fontName="Helvetica-Bold")
DER = ParagraphStyle("der", parent=N, alignment=2)
TITULO = ParagraphStyle("titulo", parent=B, fontSize=12, leading=15)
SECCION = ParagraphStyle("seccion", parent=B, textColor=colors.white)
MONO = ParagraphStyle("mono", fontName="Courier", fontSize=6, leading=7, wordWrap="CJK")  # corta cadenas sin espacios


def local(tag):
    return tag.rsplit("}", 1)[-1]


def hijos(el, nombre):
    return [h for h in el if local(h.tag) == nombre] if el is not None else []


def hijo(el, nombre):
    return next(iter(hijos(el, nombre)), None)


def dinero(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return ""


def clave(codigo, catalogo):
    """'03' -> '03 - Transferencia electrónica de fondos' (o solo el código si no está en el catálogo)."""
    if not codigo:
        return ""
    return f"{codigo} - {catalogo[codigo]}" if codigo in catalogo else codigo


def p(texto, estilo=N):
    return Paragraph(escape(str(texto or "")), estilo)


def campo(etiqueta, valor):
    return Paragraph(f"<b>{escape(etiqueta)}:</b> {escape(str(valor or ''))}", N)


def seccion(texto):
    t = Table([[Paragraph(texto, SECCION)]], colWidths=[ANCHO])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GRIS), ("TOPPADDING", (0, 0), (-1, -1), 2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


def tabla(datos, anchos, derecha=()):
    """Tabla con encabezado gris; `derecha` son las columnas numéricas (alineadas a la derecha)."""
    filas = [[p(c, B) for c in datos[0]]]
    for fila in datos[1:]:
        filas.append([p(c, DER if i in derecha else N) for i, c in enumerate(fila)])
    t = Table(filas, colWidths=anchos, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), FONDO), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def nombre_impuesto(t):
    nombre = IMPUESTOS.get(t.get("Impuesto"), t.get("Impuesto") or "")
    if t.get("TipoFactor") == "Exento":
        return f"{nombre} exento"
    try:
        return f"{nombre} {float(t.get('TasaOCuota')) * 100:g}%"
    except (TypeError, ValueError):
        return nombre


def qr(texto, lado):
    w = QrCodeWidget(texto, barLevel="M")
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(lado, lado, transform=[lado / (x1 - x0), 0, 0, lado / (y1 - y0), 0, 0])
    d.add(w)
    return d


def generar(xml_path, pdf_path):
    root = ET.parse(xml_path).getroot()
    a = root.attrib
    emisor = getattr(hijo(root, "Emisor"), "attrib", {})
    receptor = getattr(hijo(root, "Receptor"), "attrib", {})
    timbre = next((e.attrib for e in root.iter() if local(e.tag) == "TimbreFiscalDigital"), {})
    tipo = a.get("TipoDeComprobante")
    h = []  # contenido del documento

    # --- encabezado: emisor a la izquierda, datos del comprobante a la derecha
    izq = [p(emisor.get("Nombre"), TITULO), campo("RFC", emisor.get("Rfc")),
           campo("Régimen fiscal", clave(emisor.get("RegimenFiscal"), REGIMENES))]
    der = [p(TIPOS.get(tipo, "COMPROBANTE"), TITULO),
           campo("Serie y folio", f"{a.get('Serie', '')} {a.get('Folio', '')}".strip()),
           campo("Fecha de emisión", a.get("Fecha", "").replace("T", " ")),
           campo("Lugar de expedición (C.P.)", a.get("LugarExpedicion")),
           campo("Versión CFDI", a.get("Version"))]
    t = Table([[izq, der]], colWidths=[ANCHO * 0.6, ANCHO * 0.4])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (1, 0), (1, 0), 0.5, GRIS)]))
    h += [t, Spacer(1, 3 * mm)]

    # --- receptor
    h.append(seccion("RECEPTOR"))
    datos = [campo("Nombre", receptor.get("Nombre")), campo("RFC", receptor.get("Rfc")),
             campo("Uso del CFDI", clave(receptor.get("UsoCFDI"), USOS))]
    if receptor.get("RegimenFiscalReceptor"):
        datos.append(campo("Régimen fiscal", clave(receptor.get("RegimenFiscalReceptor"), REGIMENES)))
    if receptor.get("DomicilioFiscalReceptor"):
        datos.append(campo("Domicilio fiscal (C.P.)", receptor.get("DomicilioFiscalReceptor")))
    h += datos + [Spacer(1, 3 * mm)]

    # --- conceptos
    conceptos = hijos(hijo(root, "Conceptos"), "Concepto")
    con_desc = any(c.get("Descuento") for c in conceptos)
    filas = [["Clave SAT", "No. ident.", "Cant.", "Unidad", "Descripción", "P. unitario"]
             + (["Descuento"] if con_desc else []) + ["Importe"]]
    for c in conceptos:
        ca = c.attrib
        unidad = " ".join(x for x in (ca.get("ClaveUnidad"), ca.get("Unidad")) if x)
        filas.append([ca.get("ClaveProdServ"), ca.get("NoIdentificacion"), f"{float(ca.get('Cantidad') or 0):g}",
                      unidad, ca.get("Descripcion"), dinero(ca.get("ValorUnitario"))]
                     + ([dinero(ca.get("Descuento")) if ca.get("Descuento") else ""] if con_desc else [])
                     + [dinero(ca.get("Importe"))])
    fijos = [18 * mm, 18 * mm, 13 * mm, 18 * mm, 23 * mm] + ([20 * mm] if con_desc else []) + [24 * mm]
    anchos = fijos[:4] + [ANCHO - sum(fijos)] + fijos[4:]
    ult = len(anchos) - 1
    h += [seccion("CONCEPTOS"), tabla(filas, anchos, derecha=(2,) + tuple(range(5, ult + 1))), Spacer(1, 3 * mm)]

    # --- condiciones de pago (izquierda) y totales (derecha)
    pago = [campo("Moneda", a.get("Moneda"))]
    if a.get("TipoCambio") and a.get("Moneda") not in ("MXN", "XXX"):
        pago.append(campo("Tipo de cambio", a.get("TipoCambio")))
    if a.get("FormaPago"):
        pago.append(campo("Forma de pago", clave(a.get("FormaPago"), FORMAS_PAGO)))
    if a.get("MetodoPago"):
        pago.append(campo("Método de pago", clave(a.get("MetodoPago"), METODOS_PAGO)))
    if a.get("CondicionesDePago"):
        pago.append(campo("Condiciones de pago", a.get("CondicionesDePago")))
    for rel in hijos(root, "CfdiRelacionados"):
        pago.append(campo("CFDI relacionados", clave(rel.get("TipoRelacion"), RELACIONES)))
        pago += [p(r.get("UUID")) for r in hijos(rel, "CfdiRelacionado")]

    totales = [("Subtotal", dinero(a.get("SubTotal")))]
    if a.get("Descuento"):
        totales.append(("Descuento", "-" + dinero(a.get("Descuento"))))
    imp = hijo(root, "Impuestos")
    for tr in hijos(hijo(imp, "Traslados"), "Traslado"):
        if tr.get("Importe") is not None:
            totales.append((nombre_impuesto(tr.attrib), dinero(tr.get("Importe"))))
    for re_ in hijos(hijo(imp, "Retenciones"), "Retencion"):
        totales.append((f"Retención {IMPUESTOS.get(re_.get('Impuesto'), '')}".strip(), "-" + dinero(re_.get("Importe"))))
    for loc in (e.attrib for e in root.iter() if local(e.tag) == "ImpuestosLocales"):
        if float(loc.get("TotaldeTraslados") or 0):
            totales.append(("Impuestos locales trasladados", dinero(loc.get("TotaldeTraslados"))))
        if float(loc.get("TotaldeRetenciones") or 0):
            totales.append(("Impuestos locales retenidos", "-" + dinero(loc.get("TotaldeRetenciones"))))
    tt = Table([[p(k, B), p(v, DER)] for k, v in totales] + [[p("TOTAL", TITULO), Paragraph(dinero(a.get("Total")), ParagraphStyle("t", parent=TITULO, alignment=2))]],
               colWidths=[38 * mm, 32 * mm])
    tt.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.5, GRIS), ("TOPPADDING", (0, 0), (-1, -1), 1),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    t = Table([[pago, tt]], colWidths=[ANCHO - 72 * mm, 72 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    h += [t, Spacer(1, 3 * mm)]

    # --- complemento de pago (solo comprobantes tipo P)
    for pg in (e for e in root.iter() if local(e.tag) == "Pago"):
        h.append(seccion("PAGO RECIBIDO"))
        h.append(campo("Fecha de pago", (pg.get("FechaPago") or "").replace("T", " ")))
        h.append(campo("Forma de pago", clave(pg.get("FormaDePagoP"), FORMAS_PAGO)))
        h.append(campo("Monto", f"{dinero(pg.get('Monto'))} {pg.get('MonedaP') or ''}"))
        filas = [["Folio fiscal del documento pagado", "Folio", "Parc.", "Saldo anterior", "Pagado", "Saldo insoluto"]]
        for d in hijos(pg, "DoctoRelacionado"):
            filas.append([d.get("IdDocumento"), f"{d.get('Serie', '')}{d.get('Folio', '')}", d.get("NumParcialidad"),
                          dinero(d.get("ImpSaldoAnt")), dinero(d.get("ImpPagado")), dinero(d.get("ImpSaldoInsoluto"))])
        fijos = [20 * mm, 12 * mm, 28 * mm, 28 * mm, 28 * mm]
        h += [tabla(filas, [ANCHO - sum(fijos)] + fijos, derecha=(2, 3, 4, 5)), Spacer(1, 3 * mm)]

    # --- timbre fiscal: QR de verificación, certificados, sellos y cadena original
    uuid = timbre.get("UUID", "")
    sello = timbre.get("SelloCFD") or a.get("Sello", "")
    url = ("https://verificacfdi.facturaelectronica.sat.gob.mx/default.aspx"
           f"?id={uuid}&re={emisor.get('Rfc', '')}&rr={receptor.get('Rfc', '')}&tt={a.get('Total', '')}&fe={sello[-8:]}")
    cadena = "||" + "|".join(x for x in (timbre.get("Version", "1.1"), uuid, timbre.get("FechaTimbrado"),
                                         timbre.get("RfcProvCertif"), timbre.get("Leyenda"), sello,
                                         timbre.get("NoCertificadoSAT")) if x) + "||"
    datos = [campo("Folio fiscal (UUID)", uuid),
             campo("No. de serie del certificado del emisor", a.get("NoCertificado")),
             campo("No. de serie del certificado del SAT", timbre.get("NoCertificadoSAT")),
             campo("Fecha y hora de certificación", (timbre.get("FechaTimbrado") or "").replace("T", " ")),
             campo("RFC del proveedor de certificación", timbre.get("RfcProvCertif")),
             p("Sello digital del CFDI", B), p(sello, MONO),
             p("Sello digital del SAT", B), p(timbre.get("SelloSAT"), MONO)]
    t = Table([[qr(url, 34 * mm), datos]], colWidths=[37 * mm, ANCHO - 37 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    h += [seccion("TIMBRE FISCAL DIGITAL"), Spacer(1, 1 * mm), t,
          p("Cadena original del complemento de certificación digital del SAT", B), p(cadena, MONO)]

    def pagina(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRIS)
        canvas.drawString(10 * mm, 7 * mm, "Este documento es una representación impresa de un CFDI. "
                                           "PDF generado a partir del XML timbrado.")
        canvas.drawRightString(letter[0] - 10 * mm, 7 * mm, f"Página {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter, leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=13 * mm,
                            title=f"{TIPOS.get(tipo, 'CFDI')} {a.get('Serie', '')}{a.get('Folio', '')}", author=emisor.get("Nombre", ""))
    doc.build(h, onFirstPage=pagina, onLaterPages=pagina)


# --- PDF de las facturas de la base

def generar_pdf(uuid, carpeta=None):
    """Ruta del PDF de la factura con ese UUID exacto (se genera desde su XML), o None si no está en la base."""
    uuid = (uuid or "").strip().upper()
    fila = leer("SELECT folio, archivo FROM facturas WHERE uuid=?", (uuid,))
    if not fila:
        return None
    folio, archivo = fila[0]
    carpeta = Path(carpeta or config.SALIDA / "pdf")
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / ("_".join(x for x in (folio, uuid) if x) + ".pdf")
    generar(ruta_real(archivo), destino)
    return destino

