"""
El servidor del chat. Tiene dos partes:

- Lo del reto, con botones que no usan IA: cargar los XML, detalle, resumen por proveedor, incidencias y Excel.
- Lo extra: filtro y resumen por cliente, subir XML arrastrándolos, preguntas en lenguaje natural (el modelo
  escribe el SQL y se muestra) y PDF de las facturas.

La página está en plantillas/chat.html. Se abre con  python bot.py
"""
import io
import os
import threading
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

import openai
from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from revision import config, reportes
from revision.base import cargar, leer
from revision.lectura import RFC

from . import preguntas
from .pdf import generar_pdf

PUERTO = int(os.getenv("PUERTO_WEB", "5000"))
MAX_TURNOS = 6          # intercambios que se recuerdan, para preguntas de seguimiento ("¿y su PDF?")
MAX_DATOS_PREVIOS = 1500  # caracteres de los resultados de cada turno que se le recuerdan al modelo
FILAS_PAGINA = 25       # filas de cada consulta del modelo que se muestran en la página
FILAS_RETO = 500        # filas de los reportes del reto que se muestran en la página
MAX_SUBIDA_MB = 20      # tamaño máximo de una subida de XML

historial = deque(maxlen=MAX_TURNOS)  # la página es de un solo usuario: una sola conversación
candado = threading.Lock()

app = Flask(__name__, template_folder="plantillas")
app.config["MAX_CONTENT_LENGTH"] = MAX_SUBIDA_MB * 1024 * 1024

LOG = {"nombre": "incidencias.log", "nota": "Registro de la última carga", "url": "/incidencias.log"}


def cuerpo():
    datos = request.get_json(silent=True)  # exige Content-Type JSON: otra página web no puede mandarlo sin permiso
    if not isinstance(datos, dict):
        abort(400)
    return datos


def cliente_de(valor):
    """RFC del cliente pedido (None = todos). Un valor que no es RFC se rechaza."""
    valor = (valor or "").strip().upper()
    if valor and not RFC.match(valor):
        abort(400)
    return valor or None


def excel(cliente):
    nota = "Hojas Detalle y Resumen por proveedor" + (f" · cliente {cliente}" if cliente else "")
    return {"nombre": "reporte_cfdi.xlsx", "nota": nota,
            "url": "/reporte_cfdi.xlsx" + (f"?cliente={cliente}" if cliente else "")}


def cifras(cliente=None):
    """Lo que se muestra en el encabezado: cuántas facturas, proveedores e incidencias hay."""
    w, p = reportes.donde(cliente)
    (n, proveedores, total), = leer("SELECT COUNT(*), COUNT(DISTINCT emisor_rfc), COALESCE(SUM(total), 0) "
                                    f"FROM facturas {w}", p)
    _, filas = reportes.incidencias(cliente, sin_dueno=False)  # con cliente, solo lo que es suyo
    errores = sum(f[1] == "ERROR" for f in filas)
    return (f"{n} facturas · {proveedores} proveedores · ${total:,.2f} · "
            f"{errores} errores · {len(filas) - errores} avisos")


def estado(cliente=None):
    """Cifras del encabezado y lista de clientes, para que la página se actualice después de cada carga."""
    return {"cifras": cifras(cliente), "clientes": [{"rfc": r, "nombre": n, "facturas": k} for r, n, k in reportes.clientes()]}


def tabla(titulo, columnas, filas):
    return {"titulo": titulo, "columnas": columnas, "filas": filas[:FILAS_RETO], "total": len(filas)}


def dinero(v):
    return f"${v:,.2f}"


def de_quien(cliente):
    """Inicio del texto cuando la respuesta está filtrada por cliente."""
    if not cliente:
        return ""
    nombre = leer("SELECT MAX(receptor_nombre) FROM facturas WHERE receptor_rfc=?", (cliente,))[0][0]
    return f"Cliente **{nombre or cliente}** ({cliente}). "


# --- Lo del reto: respuestas fijas, sin IA

def resultado_carga(c, carpeta, cliente):
    """Texto y tablas de una carga: los conteos y las incidencias de esos archivos."""
    columnas, filas = reportes.incidencias(carpeta=carpeta)
    texto = (f"Se leyeron **{c['archivos']} archivos XML**: **{c['cargadas']} facturas cargadas**, "
             f"{c['errores']} con error (no se cargaron) y {c['avisos']} avisos. "
             f"Hay {c['en_base']} facturas en la base.")
    return texto, [tabla("Incidencias de esta carga", columnas, filas)] if filas else [], [excel(cliente), LOG]


def reto_cargar(cliente):
    with candado:
        c = cargar()
    return resultado_carga(c, config.CFDIS, cliente)


def reto_detalle(cliente):
    columnas, filas = reportes.detalle(cliente)
    if not filas:
        return de_quien(cliente) + "No hay facturas. Usa **Cargar XML** o arrastra archivos aquí.", [], []
    # en la página bastan estas columnas; el Excel trae todas
    orden = [3, 0, 1, 2, 4, 5, 6, 8, 12]
    vista = [[f[i].replace("T", " ") if i == 3 else f[i] for i in orden] for f in filas]
    texto = (de_quien(cliente) + f"**{len(filas)} facturas** · IVA trasladado {dinero(sum(f[5] for f in filas))} · "
             f"Total {dinero(sum(f[6] for f in filas))}")
    return texto, [tabla("Detalle", [columnas[i] for i in orden], vista)], [excel(cliente)]


def reto_resumen(cliente):
    columnas, filas = reportes.resumen(cliente)
    if not filas:
        return de_quien(cliente) + "No hay facturas. Usa **Cargar XML** o arrastra archivos aquí.", [], []
    texto = (de_quien(cliente) + f"**{len(filas)} proveedores**, de mayor a menor total facturado. El que más "
             f"facturó fue **{filas[0][1]}** con {dinero(filas[0][5])}. Total general "
             f"{dinero(sum(f[5] for f in filas))}, IVA {dinero(sum(f[4] for f in filas))}.")
    return texto, [tabla("Resumen por proveedor", columnas, filas)], [excel(cliente)]


def reto_incidencias(cliente):
    columnas, filas = reportes.incidencias(cliente)
    if not filas:
        return de_quien(cliente) + "Sin incidencias.", [], [LOG]
    errores = sum(f[1] == "ERROR" for f in filas)
    texto = (de_quien(cliente) + f"**{errores} archivos con error** (no se cargaron) y **{len(filas) - errores} "
             "avisos** (se cargaron, pero conviene revisarlos).")
    if cliente:
        sin_dueno = len(filas) - len(reportes.incidencias(cliente, sin_dueno=False)[1])
        if sin_dueno:
            texto += (f" {sin_dueno} de los errores son de archivos que no se pudieron leer, así que no se sabe de qué "
                      "cliente son; se incluyen para que no se pierdan.")
    return texto, [tabla("Incidencias", columnas, filas)], [LOG]


def reto_excel(cliente):
    return (de_quien(cliente) + "Aquí está el Excel de revisión con las hojas **Detalle** y "
            "**Resumen por proveedor**."), [], [excel(cliente)]


def reto_clientes(_cliente):
    columnas, filas = reportes.resumen_clientes()
    if not filas:
        return "No hay facturas. Usa **Cargar XML** o arrastra archivos aquí.", [], []
    texto = (f"**{len(filas)} clientes** con facturas, de mayor a menor total. Elige uno arriba, en **Cliente**, "
             "para ver solo sus reportes y que la IA conteste solo con sus facturas.")
    return texto, [tabla("Resumen por cliente", columnas, filas)], []


RETO = {"cargar": reto_cargar, "detalle": reto_detalle, "resumen": reto_resumen, "clientes": reto_clientes,
        "incidencias": reto_incidencias, "excel": reto_excel}


# --- Rutas

@app.get("/")
def inicio():
    return render_template("chat.html", cifras=cifras(), modelo=preguntas.MODELO, aviso=preguntas.configurado() or "",
                           max_mb=MAX_SUBIDA_MB)


@app.get("/estado")
def ver_estado():
    return jsonify(estado(cliente_de(request.args.get("cliente"))))


@app.post("/reto/<accion>")
def reto(accion):
    cliente = cliente_de(cuerpo().get("cliente"))
    if accion not in RETO:
        abort(404)
    texto, consultas, adjuntos = RETO[accion](cliente)
    return jsonify(texto=texto, consultas=consultas, adjuntos=adjuntos, **estado(cliente))


@app.post("/subir")
def subir():
    """Recibe XML arrastrados a la página, los guarda en salida/subidos/<fecha y hora>/ y los carga."""
    # una cabecera propia obliga al navegador a pedir permiso (CORS) antes de mandarla desde otra página,
    # y este servidor no lo da: así ninguna página ajena puede subir archivos aquí
    if request.headers.get("X-Bot") != "1":
        abort(400)
    cliente = cliente_de(request.form.get("cliente"))
    archivos = [a for a in request.files.getlist("xml") if a.filename]
    if not archivos:
        abort(400)
    carpeta = config.SALIDA / "subidos" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    carpeta.mkdir(parents=True)
    ignorados = []
    for a in archivos:
        nombre = secure_filename(a.filename)  # sin rutas ni caracteres raros: no puede escribir fuera de la carpeta
        if not nombre.lower().endswith(".xml"):
            ignorados.append(Path(a.filename).name)
            continue
        destino, n = carpeta / nombre, 1
        while destino.exists():  # dos archivos con el mismo nombre en la misma subida
            n += 1
            destino = carpeta / f"{Path(nombre).stem}_{n}.xml"
        a.save(destino)
    with candado:
        c = cargar(carpeta)
    texto, consultas, adjuntos = resultado_carga(c, carpeta, cliente)
    if ignorados:
        texto += f" No se leyeron porque no son .xml: {', '.join(ignorados)}."
    return jsonify(texto=texto, consultas=consultas, adjuntos=adjuntos, **estado(cliente))


@app.errorhandler(413)
def muy_grande(_e):
    return jsonify(texto=f"La subida pasa de {MAX_SUBIDA_MB} MB; manda los XML en partes.", consultas=[],
                   adjuntos=[]), 413


@app.post("/preguntar")
def preguntar():
    datos = cuerpo()
    pregunta = (datos.get("pregunta") or "").strip()
    cliente = cliente_de(datos.get("cliente"))
    if not pregunta:
        abort(400)
    problema = preguntas.configurado()
    if problema:
        return jsonify(texto=problema, consultas=[], adjuntos=[])
    print(f"> {pregunta}" + (f"  [cliente {cliente}]" if cliente else ""))
    with candado:
        try:
            texto, consultas, adjuntos = preguntas.responder(pregunta, [m for t in historial for m in t], cliente)
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
                  else excel(cliente) for a in adjuntos],
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
    cliente = cliente_de(request.args.get("cliente"))
    wb = reportes.libro(cliente)
    if not wb:
        abort(404)
    datos = io.BytesIO()
    wb.save(datos)
    datos.seek(0)
    nombre = f"reporte_cfdi_{cliente}.xlsx" if cliente else "reporte_cfdi.xlsx"
    return send_file(datos, as_attachment=True, download_name=nombre)


@app.get("/incidencias.log")
def log():
    ruta = config.SALIDA / "incidencias.log"
    if not ruta.is_file():
        abort(404)
    return send_file(ruta, mimetype="text/plain; charset=utf-8")


def abrir():
    threading.Timer(1, webbrowser.open, [f"http://localhost:{PUERTO}"]).start()
    app.run(host="127.0.0.1", port=PUERTO)  # solo accesible desde esta computadora
