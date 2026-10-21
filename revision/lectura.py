"""Lectura y validación de un XML de CFDI (3.3 o 4.0)."""
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

NS_CFDI = {"http://www.sat.gob.mx/cfd/3", "http://www.sat.gob.mx/cfd/4"}  # CFDI 3.3 y 4.0
RFC = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z\d]{3}$")  # 3 letras = persona moral, 4 = persona física
TOLERANCIA = Decimal("0.01")  # diferencia de redondeo aceptada al cuadrar los totales


class Invalido(Exception):
    """El archivo no se puede cargar. `tipo` es la clase de incidencia que va al log."""

    def __init__(self, tipo, detalle):
        super().__init__(detalle)
        self.tipo = tipo


def local(tag):
    return tag.rsplit("}", 1)[-1]


def espacio(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def hijos(el, nombre):
    return [h for h in el if local(h.tag) == nombre] if el is not None else []


def hijo(el, nombre):
    return next(iter(hijos(el, nombre)), None)


def dec(v):
    """Decimal del texto del XML, o None si falta o no es número. Decimal evita los errores de redondeo de float."""
    try:
        return Decimal(v)
    except (TypeError, InvalidOperation):
        return None


def suma(valores):
    return sum((v for v in valores if v is not None), Decimal(0))


def leer_cfdi(path):
    """(factura, conceptos, avisos) de un XML. Lanza Invalido si no se puede cargar."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise Invalido("XML dañado", f"El XML está mal formado o incompleto ({e})")
    if local(root.tag) != "Comprobante" or espacio(root.tag) not in NS_CFDI:
        raise Invalido("No es CFDI", f"El elemento raíz es <{local(root.tag)}>; se esperaba cfdi:Comprobante 3.3 o 4.0")
    timbre = next((e.attrib for e in root.iter() if local(e.tag) == "TimbreFiscalDigital"), {})
    uuid = timbre.get("UUID", "").strip().upper()
    if not uuid:
        raise Invalido("Sin timbre", "No tiene TimbreFiscalDigital con UUID: no es una factura timbrada")

    a = root.attrib
    emisor = getattr(hijo(root, "Emisor"), "attrib", {})
    receptor = getattr(hijo(root, "Receptor"), "attrib", {})
    subtotal, total = dec(a.get("SubTotal")), dec(a.get("Total"))
    faltan = [n for n, v in (("Fecha", a.get("Fecha")), ("SubTotal", subtotal), ("Total", total),
                             ("RFC del emisor", emisor.get("Rfc"))) if not v and v != 0]
    if faltan:
        raise Invalido("Datos incompletos", "Faltan o no son válidos: " + ", ".join(faltan))

    # impuestos a nivel comprobante (los de cada concepto ya vienen sumados aquí)
    impuestos = hijo(root, "Impuestos")
    traslados = [t.attrib for t in hijos(hijo(impuestos, "Traslados"), "Traslado")]
    iva = suma(dec(t.get("Importe")) for t in traslados if t.get("Impuesto") == "002")  # 002 = IVA; 003 = IEPS
    retenciones = dec(impuestos.get("TotalImpuestosRetenidos")) if impuestos is not None else None
    locales = [e.attrib for e in root.iter() if local(e.tag) == "ImpuestosLocales"]
    descuento = dec(a.get("Descuento")) or Decimal(0)

    avisos = []
    rfc = emisor["Rfc"].strip().upper()
    if not RFC.match(rfc):
        avisos.append(("RFC inválido", f"El RFC del emisor «{rfc}» no tiene el formato del SAT"))
    esperado = (subtotal - descuento + suma(dec(t.get("Importe")) for t in traslados) - (retenciones or 0)
                + suma(dec(x.get("TotaldeTraslados")) for x in locales)
                - suma(dec(x.get("TotaldeRetenciones")) for x in locales))
    if abs(esperado - total) > TOLERANCIA:
        avisos.append(("Totales no cuadran", f"Subtotal - descuento + impuestos = {esperado:.2f}, pero Total = {total:.2f}"))
    tipo = a.get("TipoDeComprobante")
    if tipo != "I":
        avisos.append(("No es de ingreso", f"TipoDeComprobante={tipo}: aparece en el detalle pero no suma en el resumen"))
    elif iva == 0:
        tasa_cero = any(t.get("Impuesto") == "002" and (t.get("TipoFactor") == "Exento" or dec(t.get("TasaOCuota")) == 0)
                        for t in traslados)
        avisos.append(("IVA en cero", "IVA a tasa 0% o exento" if tasa_cero else "La factura no traslada IVA"))
    if a.get("Moneda", "MXN") != "MXN":
        avisos.append(("Moneda extranjera", f"Moneda {a.get('Moneda')}, tipo de cambio {a.get('TipoCambio')}: "
                                            "los importes no están en pesos"))

    factura = {
        "uuid": uuid, "serie": a.get("Serie"), "folio": a.get("Folio"), "fecha": a["Fecha"], "tipo": tipo,
        "moneda": a.get("Moneda"), "subtotal": float(subtotal), "descuento": float(descuento), "iva": float(iva),
        "retenciones": float(retenciones or 0), "total": float(total), "emisor_rfc": rfc,
        "emisor_nombre": emisor.get("Nombre"), "receptor_rfc": receptor.get("Rfc"),
        "receptor_nombre": receptor.get("Nombre"),
    }
    conceptos = [(c.get("ClaveProdServ"), c.get("Descripcion"), float(dec(c.get("Cantidad")) or 0),
                  c.get("Unidad") or c.get("ClaveUnidad"), float(dec(c.get("ValorUnitario")) or 0),
                  float(dec(c.get("Importe")) or 0))
                 for c in hijos(hijo(root, "Conceptos"), "Concepto")]
    return factura, conceptos, avisos
