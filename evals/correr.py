#!/usr/bin/env python3
"""
Evaluaciones de la IA del bot: hace las preguntas de casos.json al modelo real y revisa cada respuesta.

Las respuestas esperadas se calcularon con SQL sobre cfdis/ más ejemplos/otro_cliente/, y el script carga
exactamente esos XML en una base temporal: no toca salida/ y el resultado no depende de lo que se haya subido.

Cada caso puede revisar:
  contiene     textos o cifras que deben aparecer en la respuesta (sin importar acentos, mayúsculas, $ ni comas)
  no_contiene  textos que no deben aparecer (p. ej. datos de otro cliente)
  pdf          uuid de las facturas que se deben adjuntar
y puede fijar `cliente` (RFC receptor) para preguntar como si se hubiera elegido ese cliente en el bot.

Uso:
  python evals/correr.py                 # todos los casos
  python evals/correr.py --caso duplicadas
  python evals/correr.py --veces 3       # cada caso 3 veces: el modelo no siempre contesta igual

Requiere OPENAI_API_KEY en el .env. El resultado se guarda en evals/resultados/.
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))  # para importar revision/ y asistente/ al correrlo como script

from asistente import preguntas  # noqa: E402
from revision import config  # noqa: E402
from revision.base import cargar  # noqa: E402

CASOS = Path(__file__).with_name("casos.json")
RESULTADOS = Path(__file__).with_name("resultados")


def normal(texto):
    """Sin acentos, en minúsculas y sin $ ni comas: '$107,825.92 Papelería' -> '107825.92 papeleria'."""
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn").lower()
    return texto.replace("$", "").replace(",", "")


def aparece(esperado, respuesta):
    """El texto esperado aparece como palabra o cifra completa: '3' no cuenta dentro de '2026-09-30'."""
    return re.search(rf"(?<![\w.]){re.escape(normal(esperado))}(?![\w]|\.\d)", normal(respuesta)) is not None


def revisar(caso, respuesta, adjuntos):
    """Lista de fallas del caso (vacía si pasó)."""
    fallas = [f"falta «{e}»" for e in caso.get("contiene", []) if not aparece(e, respuesta)]
    fallas += [f"no debía decir «{e}»" for e in caso.get("no_contiene", []) if aparece(e, respuesta)]
    entregados = {a.get("uuid") for a in adjuntos if a["tipo"] == "pdf"}
    fallas += [f"no adjuntó el PDF {u}" for u in caso.get("pdf", []) if u not in entregados]
    return fallas


def preparar_base():
    """Base temporal con los XML de la prueba y los del segundo cliente."""
    carpeta = Path(tempfile.mkdtemp(prefix="evals_cfdi_"))
    config.SALIDA, config.DB = carpeta, carpeta / "facturas.db"
    with contextlib.redirect_stdout(io.StringIO()):  # el log de la carga no hace falta aquí
        cargar(config.CFDIS)
        cargar(config.RAIZ / "ejemplos" / "otro_cliente")


def main():
    sys.stdout.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--caso", help="correr solo el caso con ese id")
    p.add_argument("--veces", type=int, default=1, help="veces que se repite cada caso")
    a = p.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("Falta OPENAI_API_KEY: copia .env.example como .env y pon tu llave de OpenAI.")
    casos = [c for c in json.loads(CASOS.read_text(encoding="utf-8")) if not a.caso or c["id"] == a.caso]
    if not casos:
        sys.exit(f"No hay un caso con id {a.caso}")
    preparar_base()

    corridas = []
    for caso in casos:
        for vez in range(1, a.veces + 1):
            try:
                respuesta, consultas, adjuntos = preguntas.responder(caso["pregunta"], cliente=caso.get("cliente"))
                fallas = revisar(caso, respuesta, adjuntos)
            except preguntas.openai.OpenAIError as e:
                sys.exit(preguntas.error_api(e))
            corridas.append({"id": caso["id"], "vez": vez, "pregunta": caso["pregunta"], "cliente": caso.get("cliente"),
                             "ok": not fallas, "fallas": fallas, "respuesta": respuesta,
                             "sql": [c["sql"] for c in consultas], "errores_sql": sum("error" in c for c in consultas),
                             "pdf": [x.get("uuid") for x in adjuntos if x["tipo"] == "pdf"]})
            marca = "OK   " if not fallas else "FALLA"
            print(f"{marca} {caso['id']}" + (f" (vez {vez})" if a.veces > 1 else "")
                  + ("" if not fallas else "  -> " + "; ".join(fallas)))

    bien = sum(c["ok"] for c in corridas)
    print(f"\n{bien}/{len(corridas)} correctas ({bien / len(corridas):.0%}) con {preguntas.MODELO}")
    RESULTADOS.mkdir(exist_ok=True)
    destino = RESULTADOS / f"{datetime.now():%Y-%m-%d_%H%M}_{re.sub(r'[^\w.-]', '_', preguntas.MODELO)}.json"
    destino.write_text(json.dumps({"modelo": preguntas.MODELO, "fecha": datetime.now().isoformat(timespec="seconds"),
                                   "correctas": bien, "total": len(corridas), "corridas": corridas},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Detalle: {destino}")


if __name__ == "__main__":
    main()
