"""
Preguntas en lenguaje natural sobre la base de facturas. El modelo de OpenAI escribe el SQL, el programa
lo revisa y lo ejecuta en una conexión de solo lectura, y con el resultado el modelo redacta la respuesta.
Se muestra siempre el SQL que se ejecutó, para poder revisarlo.

Configuración: copia .env.example como .env y pon OPENAI_API_KEY (y, si quieres, MODELO_OPENAI).
Requiere:  pip install openai python-dotenv
"""
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import date

import openai
from dotenv import load_dotenv

from revision import config
from revision.base import SIN_DATOS, leer
from revision.lectura import RFC

load_dotenv(config.RAIZ / ".env")

MODELO = os.getenv("MODELO_OPENAI", "gpt-5.4-mini")
MAX_VUELTAS = 5    # consultas por pregunta, para que no se quede en un ciclo
MAX_FILAS = 50     # filas que se le devuelven al modelo por consulta
MAX_SEGUNDOS = 10  # una consulta que tarde más se corta

SISTEMA = """Eres el asistente de revisión de facturas (CFDI) de KM&G, un despacho que revisa las facturas \
que sus clientes reciben de sus proveedores. Contestas preguntas consultando una base SQLite con la herramienta \
`consultar`. Hoy es {hoy}. {alcance}

Esquema:
  facturas(uuid PK, serie, folio, fecha TEXT ISO 'AAAA-MM-DDTHH:MM:SS', tipo, moneda, subtotal, descuento,
           iva, retenciones, total, emisor_rfc, emisor_nombre, receptor_rfc, receptor_nombre, archivo)
  conceptos(id, uuid -> facturas.uuid, clave_prod, descripcion, cantidad, unidad, valor_unitario, importe)
  incidencias(id, archivo, nivel, tipo, detalle, uuid)
    nivel 'ERROR' = el archivo no se cargó (tipo: 'XML dañado', 'XML no permitido', 'No es CFDI', 'Sin timbre',
    'Duplicada', 'Datos incompletos', 'Error inesperado'); nivel 'AVISO' = se cargó pero hay que revisarlo
    (tipo: 'IVA en cero', 'Totales no cuadran', 'RFC inválido', 'RFC receptor inválido', 'No es de ingreso',
    'Moneda extranjera'). Las incidencias con uuid NULL son de archivos que no se pudieron leer.

Reglas:
- El proveedor es el emisor; el cliente de KM&G es el receptor. Agrupa e identifica siempre por RFC \
(emisor_rfc o receptor_rfc), no por nombre.
- Un RFC de 12 caracteres es de persona moral (empresa) y uno de 13, de persona física.
- tipo 'I' = factura de ingreso. "Facturado" = SUM(total) con tipo='I'. La columna iva es el IVA trasladado.
- Al mencionar un proveedor o un cliente, da su nombre y su RFC.
- Las facturas duplicadas no están en la tabla facturas (solo la primera copia); están en incidencias. Una \
duplicada sí es una factura (repetida); "no es factura" son los tipos 'No es CFDI', 'XML dañado' y 'Sin timbre'.
- Si preguntan por archivos, nombra cada archivo (columna archivo).
- Para buscar por nombre usa la función norm(), que quita acentos y pasa a minúsculas, y escribe el texto \
buscado sin acentos y en minúsculas: norm(emisor_nombre) LIKE '%logisticos tapatios%'. Nunca compares nombres \
sin norm(). Si una búsqueda no da resultados, prueba con menos palabras antes de responder que no hay nada.
- Filtra fechas comparando texto: fecha >= '2026-09-01' AND fecha < '2026-10-01'.
- Las sumas y conteos hazlos en SQL (SUM, COUNT), nunca a mano.
- Una sola sentencia SELECT (o WITH ... SELECT) por llamada; la base es de solo lectura.

Archivos:
- Si piden ver o descargar una factura, localiza su uuid con `consultar` y llama a `enviar_pdf`. Si la \
descripción coincide con varias facturas y no está claro cuál quieren, lista las candidatas y pregunta.
- Si piden el Excel o el reporte de revisión, llama a `enviar_excel`.

Respuesta: breve, en español, en texto plano (puedes usar **negritas**), con el dato que se pidió. Dinero como \
$1,234.56. No copies tablas largas: quien pregunta ya ve las filas de tus consultas. Si la pregunta no tiene que \
ver con las facturas, di en una línea que solo atiendes temas de las facturas cargadas."""


def herramienta(nombre, descripcion, requeridos=(), **propiedades):
    return {"type": "function", "function": {"name": nombre, "description": descripcion, "parameters": {
        "type": "object", "required": list(requeridos), "properties": propiedades}}}


HERRAMIENTAS = [
    herramienta("consultar", "Ejecuta una consulta SELECT de SQLite sobre la base de facturas y devuelve las filas.",
                ["sql"], sql={"type": "string", "description": "Una sola sentencia SELECT (o WITH ... SELECT)."}),
    herramienta("enviar_pdf", "Adjunta a la respuesta el PDF (representación impresa) de una o varias facturas.",
                ["uuids"], uuids={"type": "array", "items": {"type": "string"}, "description": "Valores de facturas.uuid."}),
    herramienta("enviar_excel", "Adjunta el Excel de revisión, con las hojas 'Detalle' y 'Resumen por proveedor'."),
]
MAX_PDFS = 10  # PDF por respuesta


def validar(sql):
    """El SQL limpio, o ValueError si no es una sola consulta de lectura."""
    sql = (sql or "").strip().rstrip(";").strip()
    if ";" in sql:
        raise ValueError("Solo se permite una sentencia.")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise ValueError("Solo se permiten consultas SELECT.")
    return sql


def norm(texto):
    """Sin acentos y en minúsculas, para buscar nombres: 'Logísticos' y 'LOGISTICOS' quedan igual."""
    texto = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").lower()


def conectar(cliente=None):
    """Conexión de solo lectura: aunque el SQL pasara la validación, no podría modificar la base.

    Con `cliente` (RFC receptor), las tablas facturas, conceptos e incidencias se reemplazan por vistas temporales
    con solo lo de ese cliente, y el autorizador de SQLite prohíbe leer las tablas originales (main.facturas…).
    Así el filtro no depende de que el modelo se acuerde de ponerlo.
    """
    con = sqlite3.connect(config.DB.as_uri() + "?mode=ro", uri=True)
    con.create_function("norm", 1, norm, deterministic=True)
    if cliente:
        if not RFC.match(cliente):
            con.close()
            raise ValueError(f"RFC de cliente inválido: {cliente}")
        de_cliente = f"SELECT uuid FROM main.facturas WHERE receptor_rfc='{cliente}'"  # RFC ya validado
        con.executescript(f"""
            CREATE TEMP VIEW facturas AS SELECT * FROM main.facturas WHERE receptor_rfc='{cliente}';
            CREATE TEMP VIEW conceptos AS SELECT * FROM main.conceptos WHERE uuid IN ({de_cliente});
            CREATE TEMP VIEW incidencias AS SELECT * FROM main.incidencias WHERE uuid IS NULL OR uuid IN ({de_cliente});
            """)
        con.set_authorizer(lambda accion, _a, _b, base, vista: sqlite3.SQLITE_DENY
                           if accion == sqlite3.SQLITE_READ and base == "main" and vista is None else sqlite3.SQLITE_OK)
    limite = time.monotonic() + MAX_SEGUNDOS
    con.set_progress_handler(lambda: time.monotonic() > limite, 10000)
    return con


def ejecutar_sql(sql, cliente=None):
    """(columnas, filas) de la consulta. Lanza ValueError o sqlite3.Error si no se puede."""
    con = conectar(cliente)
    try:
        cur = con.execute(validar(sql))
        return [d[0] for d in cur.description or []], cur.fetchmany(MAX_FILAS + 1)
    finally:
        con.close()


def como_texto(columnas, filas):
    salida = [" | ".join(columnas)] + [" | ".join("" if v is None else str(v) for v in f) for f in filas[:MAX_FILAS]]
    if len(filas) > MAX_FILAS:
        salida.append(f"(hay más filas; solo se muestran {MAX_FILAS}. Agrega filtros o usa agregados)")
    return "\n".join(salida) if filas else "Sin resultados."


def ejecutar(nombre, args, consultas, adjuntos, cliente=None):
    """Corre una herramienta y devuelve el texto que verá el modelo."""
    if nombre == "consultar":
        sql = args.get("sql")
        try:
            columnas, filas = ejecutar_sql(sql, cliente)
        except (ValueError, sqlite3.Error) as e:
            # el error regresa al modelo para que corrija la consulta en la siguiente vuelta
            consultas.append({"sql": sql, "error": str(e)})
            return f"Error: {e}"
        consultas.append({"sql": sql, "columnas": columnas, "filas": filas})
        return como_texto(columnas, filas)
    if nombre == "enviar_pdf":
        pedidos = [u.strip().upper() for u in args.get("uuids") or [] if isinstance(u, str)]
        ya = {a["uuid"] for a in adjuntos if "uuid" in a}
        faltan = []
        for u in pedidos[:MAX_PDFS]:
            fila = leer("SELECT folio, emisor_nombre, total FROM facturas WHERE uuid=? AND (? IS NULL OR receptor_rfc=?)",
                        (u, cliente, cliente))  # con cliente, solo sus facturas
            if not fila:
                faltan.append(u)
            elif u not in ya:
                folio, emisor, total = fila[0]
                adjuntos.append({"tipo": "pdf", "uuid": u, "nombre": f"Factura {folio or u[:8]}.pdf",
                                 "nota": f"{emisor} · ${total:,.2f}"})
                ya.add(u)
        return (f"Se adjuntarán {len(ya)} PDF." + (f" No existen en la base: {', '.join(faltan)}." if faltan else "")
                + (f" Solo se mandan {MAX_PDFS} por respuesta." if len(pedidos) > MAX_PDFS else ""))
    if nombre == "enviar_excel":
        if not any(a["tipo"] == "xlsx" for a in adjuntos):
            adjuntos.append({"tipo": "xlsx", "nombre": "reporte_cfdi.xlsx", "nota": "Detalle y Resumen por proveedor"})
        return "Se adjuntará el Excel de revisión."
    return f"No existe la herramienta {nombre}."


def alcance(cliente):
    if not cliente:
        return "Ves las facturas de todos los clientes."
    nombre = leer("SELECT MAX(receptor_nombre) FROM facturas WHERE receptor_rfc=?", (cliente,))[0][0]
    return (f"Estás viendo solo las facturas del cliente {nombre or ''} ({cliente}): las tablas ya vienen "
            "filtradas, no hace falta filtrar por receptor_rfc.")


def responder(pregunta, historial=(), cliente=None):
    """(respuesta, consultas, adjuntos).

    `historial` son los mensajes de turnos anteriores ({"role", "content"}), para preguntas de seguimiento.
    `cliente` (RFC receptor) limita todo lo que el modelo puede ver a las facturas de ese cliente.
    Cada consulta es un dict con sql y columnas/filas, o error. Cada adjunto: tipo ('pdf' o 'xlsx'), nombre, nota
    y, en los PDF, uuid.
    """
    api = openai.OpenAI()
    mensajes = [{"role": "system", "content": SISTEMA.format(hoy=date.today().isoformat(), alcance=alcance(cliente))},
                *historial, {"role": "user", "content": pregunta}]
    consultas, adjuntos = [], []
    for _ in range(MAX_VUELTAS):
        m = api.chat.completions.create(model=MODELO, messages=mensajes, tools=HERRAMIENTAS).choices[0].message
        if not m.tool_calls:
            return (m.content or "").strip(), consultas, adjuntos
        mensajes.append({"role": "assistant", "content": m.content, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in m.tool_calls]})
        for llamada in m.tool_calls:
            try:
                args = json.loads(llamada.function.arguments or "{}")
                resultado = ejecutar(llamada.function.name, args if isinstance(args, dict) else {}, consultas, adjuntos,
                                     cliente)
            except ValueError as e:
                resultado = f"Argumentos inválidos: {e}"
            mensajes.append({"role": "tool", "tool_call_id": llamada.id, "content": resultado})
    return "No pude terminar de responder; intenta con una pregunta más específica.", consultas, adjuntos


def error_api(e):
    """Mensaje para el usuario de un error de la API de OpenAI."""
    if isinstance(e, openai.AuthenticationError):
        return "La API key de OpenAI no es válida. Revisa OPENAI_API_KEY en el archivo .env."
    if isinstance(e, openai.NotFoundError):
        return f"No existe el modelo '{MODELO}'. Revisa MODELO_OPENAI en el archivo .env."
    if isinstance(e, openai.RateLimitError):
        return "OpenAI rechazó la consulta por límite de uso o falta de saldo en la cuenta."
    if isinstance(e, openai.APIConnectionError):
        return "No hay conexión con OpenAI; revisa el internet."
    if isinstance(e, openai.APIStatusError):
        return f"OpenAI devolvió un error ({e.status_code}): {e.message}"
    return f"Error con OpenAI: {e}"


def configurado():
    """Texto del problema de configuración que impide preguntar, o None si todo está listo."""
    if not os.getenv("OPENAI_API_KEY"):
        return ("Sin OPENAI_API_KEY: los botones del reto funcionan, pero para preguntas libres copia "
                ".env.example como .env y pon tu llave de OpenAI.")
    if not config.DB.is_file():
        return SIN_DATOS
    return None

