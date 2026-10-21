"""
El servidor del chat. Tiene dos partes:

- Lo del reto, con botones que no usan IA: cargar los XML, detalle, resumen por proveedor, incidencias y Excel.
- Lo extra: preguntas en lenguaje natural (el modelo escribe el SQL y se muestra) y PDF de las facturas.

La página está en plantillas/chat.html. Se abre con  python bot.py
"""
import io
import os
import threading
import webbrowser
from collections import deque

import openai
from flask import Flask, abort, jsonify, render_template, request, send_file

from revision import config, reportes
from revision.base import cargar, leer

from . import preguntas
from .pdf import generar_pdf

PUERTO = int(os.getenv("PUERTO_WEB", "5000"))
MAX_TURNOS = 6          # intercambios que se recuerdan, para preguntas de seguimiento ("¿y su PDF?")
MAX_DATOS_PREVIOS = 1500  # caracteres de los resultados de cada turno que se le recuerdan al modelo
FILAS_PAGINA = 25       # filas de cada consulta del modelo que se muestran en la página
FILAS_RETO = 500        # filas de los reportes del reto que se muestran en la página

historial = deque(maxlen=MAX_TURNOS)  # la página es de un solo usuario: una sola conversación
candado = threading.Lock()

app = Flask(__name__, template_folder="plantillas")

EXCEL = {"nombre": "reporte_cfdi.xlsx", "nota": "Hojas Detalle y Resumen por proveedor", "url": "/reporte_cfdi.xlsx"}
LOG = {"nombre": "incidencias.log", "nota": "Registro de la última carga", "url": "/incidencias.log"}


def cuerpo():
    datos = request.get_json(silent=True)  # exige Content-Type JSON: otra página web no puede mandarlo sin permiso
    if not isinstance(datos, dict):
        abort(400)
    return datos


def cifras():
    """Lo que se muestra en el encabezado: cuántas facturas, proveedores e incidencias hay en la base."""
    (n, proveedores, total), = leer("SELECT COUNT(*), COUNT(DISTINCT emisor_rfc), COALESCE(SUM(total), 0) FROM facturas")
    niveles = dict(leer("SELECT nivel, COUNT(*) FROM incidencias GROUP BY nivel"))
    return (f"{n} facturas · {proveedores} proveedores · ${total:,.2f} · "
            f"{niveles.get('ERROR', 0)} errores · {niveles.get('AVISO', 0)} avisos")


def tabla(titulo, columnas, filas):
    return {"titulo": titulo, "columnas": columnas, "filas": filas[:FILAS_RETO], "total": len(filas)}


def dinero(v):
    return f"${v:,.2f}"


# --- Lo del reto: respuestas fijas, sin IA

def reto_cargar():
    with candado:
        c = cargar()
    columnas, filas = reportes.incidencias()
    texto = (f"Se leyeron **{c['archivos']} archivos XML** de la carpeta cfdis: **{c['cargadas']} facturas cargadas**, "
             f"{c['errores']} archivos con error (no se cargaron) y {c['avisos']} avisos. "
             f"Hay {c['en_base']} facturas en la base.")
    return texto, [tabla("Incidencias", columnas, filas)] if filas else [], [EXCEL, LOG]


def reto_detalle():
    columnas, filas = reportes.detalle()
    if not filas:
        return "No hay facturas en la base. Usa **Cargar XML**.", [], []
    # en la página bastan estas columnas; el Excel trae todas
    orden = [3, 0, 1, 2, 4, 5, 6, 10]
    vista = [[f[i].replace("T", " ") if i == 3 else f[i] for i in orden] for f in filas]
    texto = (f"**{len(filas)} facturas** · IVA trasladado {dinero(sum(f[5] for f in filas))} · "
             f"Total {dinero(sum(f[6] for f in filas))}")
    return texto, [tabla("Detalle", [columnas[i] for i in orden], vista)], [EXCEL]


def reto_resumen():
    columnas, filas = reportes.resumen()
    if not filas:
        return "No hay facturas en la base. Usa **Cargar XML**.", [], []
    texto = (f"**{len(filas)} proveedores**, de mayor a menor total facturado. El que más facturó fue "
             f"**{filas[0][1]}** con {dinero(filas[0][5])}. Total general {dinero(sum(f[5] for f in filas))}, "
             f"IVA {dinero(sum(f[4] for f in filas))}.")
    return texto, [tabla("Resumen por proveedor", columnas, filas)], [EXCEL]


def reto_incidencias():
    columnas, filas = reportes.incidencias()
    if not filas:
        return "Sin incidencias en la última carga.", [], [LOG]
    errores = sum(f[1] == "ERROR" for f in filas)
    texto = (f"**{errores} archivos con error** (no se cargaron) y **{len(filas) - errores} avisos** "
             "(se cargaron, pero conviene revisarlos).")
    return texto, [tabla("Incidencias", columnas, filas)], [LOG]


def reto_excel():
    return "Aquí está el Excel de revisión con las hojas **Detalle** y **Resumen por proveedor**.", [], [EXCEL]


RETO = {"cargar": reto_cargar, "detalle": reto_detalle, "resumen": reto_resumen,
        "incidencias": reto_incidencias, "excel": reto_excel}


# --- Rutas

@app.get("/")
def inicio():
    return render_template("chat.html", cifras=cifras(), modelo=preguntas.MODELO, aviso=preguntas.configurado() or "")


@app.post("/reto/<accion>")
def reto(accion):
    cuerpo()
    if accion not in RETO:
        abort(404)
    texto, consultas, adjuntos = RETO[accion]()
    return jsonify(texto=texto, consultas=consultas, adjuntos=adjuntos, cifras=cifras())


@app.post("/preguntar")
def preguntar():
    pregunta = (cuerpo().get("pregunta") or "").strip()
    if not pregunta:
        abort(400)
    problema = preguntas.configurado()
    if problema:
        return jsonify(texto=problema, consultas=[], adjuntos=[])
    print(f"> {pregunta}")
    with candado:
        try:
            texto, consultas, adjuntos = preguntas.responder(pregunta, [m for t in historial for m in t])
        except openai.OpenAIError as e:
            return jsonify(texto=preguntas.error_api(e), consultas=[], adjuntos=[])
        historial.append([{"role": "user", "content": pregunta},
                          {"role": "assistant", "content": recuerdo(texto, consultas)}])
    for c in consultas:
        print(f"  SQL: {c['sql']}")
    return jsonify(
        texto=texto,
        consultas=[{"sql": c["sql"], "error": c["error"]} if "error" in c else
                   {"sql": c["sql"], "columnas": c["columnas"], "filas": c["filas"][:FILAS_PAGINA],
                    "total": len(c["filas"]), "mas": len(c["filas"]) > preguntas.MAX_FILAS}
                   for c in consultas],
        adjuntos=[{"nombre": a["nombre"], "nota": a["nota"], "url": f"/pdf/{a['uuid']}"} if a["tipo"] == "pdf"
                  else EXCEL for a in adjuntos],
    )


def recuerdo(texto, consultas):
    """Lo que se guarda de la respuesta para el siguiente turno: el texto y un extracto de los datos consultados,
    para que el modelo sepa, por ejemplo, el uuid de la factura de la que se habló."""
    datos = "\n".join(preguntas.como_texto(c["columnas"], c["filas"]) for c in consultas if "filas" in c)
    if len(datos) > MAX_DATOS_PREVIOS:
        datos = datos[:MAX_DATOS_PREVIOS] + "\n(recortado)"
    return texto + (f"\n\n[Datos consultados]\n{datos}" if datos else "")


@app.post("/reiniciar")
def reiniciar():
    cuerpo()
    with candado:
        historial.clear()
    return jsonify(ok=True)


@app.get("/pdf/<uuid>")
def pdf(uuid):
    # el XML de origen sale de la base, nunca de lo que mande el navegador
    ruta = generar_pdf(uuid)
    if not ruta:
        abort(404)
    return send_file(ruta, mimetype="application/pdf", download_name=ruta.name)


@app.get("/reporte_cfdi.xlsx")
def reporte():
    wb = reportes.libro()
    if not wb:
        abort(404)
    datos = io.BytesIO()
    wb.save(datos)
    datos.seek(0)
    return send_file(datos, as_attachment=True, download_name="reporte_cfdi.xlsx")


@app.get("/incidencias.log")
def log():
    ruta = config.SALIDA / "incidencias.log"
    if not ruta.is_file():
        abort(404)
    return send_file(ruta, mimetype="text/plain; charset=utf-8")


def abrir():
    threading.Timer(1, webbrowser.open, [f"http://localhost:{PUERTO}"]).start()
    app.run(host="127.0.0.1", port=PUERTO)  # solo accesible desde esta computadora
