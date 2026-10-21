# Automatización de revisión de CFDI

El proyecto tiene dos partes:

| | Qué hace | Cómo se corre |
|---|---|---|
| **El reto** | Lee la carpeta `cfdis/`, extrae los datos de cada factura, detecta duplicados y archivos dañados sin detenerse, y genera el Excel y el log de incidencias. | `python reto.py` |
| **El extra (bot)** | Un chat en el navegador que hace lo mismo que el reto con botones (sin IA) y, además, responde preguntas en lenguaje natural convirtiéndolas a SQL y genera el PDF de cualquier factura. | `python bot.py` |

## Instalación

Requiere Python 3.10 o más reciente.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (en Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

Para las preguntas libres del bot: copia `.env.example` como `.env` y pon tu `OPENAI_API_KEY`.
El reto y los botones del bot funcionan sin llave ni internet.

## El reto

```bash
python reto.py                 # lee cfdis/
python reto.py OTRA_CARPETA
```

Deja en `salida/`:

- `reporte_cfdi.xlsx`: hoja **Detalle** (una fila por factura: UUID, RFC y nombre del emisor, fecha, subtotal,
  IVA trasladado y total) y hoja **Resumen por proveedor** (total facturado e IVA por RFC emisor, de mayor a menor).
- `incidencias.log`: archivos con error y avisos.
- `facturas.db`: la base SQLite con las facturas; es la que usa el bot.

### Resultado con los archivos de la prueba

El paquete trae **18 XML** (no 20: faltan `factura_08` y `factura_09`). Se cargaron **14 facturas** de
6 proveedores, por **$320,421.31** con **$33,288.64** de IVA. Incidencias:

| Archivo | Nivel | Incidencia |
|---|---|---|
| factura_04.xml | ERROR | Duplicada: mismo UUID que factura_03 |
| factura_11.xml | ERROR | Duplicada: mismo UUID que factura_10 |
| factura_19.xml | ERROR | XML dañado (archivo truncado) |
| factura_20.xml | ERROR | No es CFDI (es un `<Pedido>`) |
| factura_12.xml, factura_13.xml | AVISO | IVA en cero: tasa 0% (alimentos), se cargan pero se señalan |

## El extra: el bot

```bash
python bot.py        # carga cfdis/ y abre http://localhost:5000
```

- **Lo del reto, con botones y sin IA:** Cargar XML, Detalle, Resumen por proveedor, Incidencias y Excel.
  Las respuestas salen directo de la base; funcionan aunque no haya llave de OpenAI.
- **Preguntas libres con IA:** "¿qué proveedor me cobró más IVA?", "¿qué le compré a la constructora?".
  El modelo escribe el SQL y cada respuesta lo muestra junto con sus filas. Recuerda la conversación
  ("¿y su PDF?").
- **PDF de las facturas:** "dame el PDF de la factura más alta" lo adjunta para verlo o descargarlo.

Solo escucha en `127.0.0.1`, es decir, solo es accesible desde la misma computadora.

## Estructura

```
Automatizacion_facturas/
├── reto.py                  EL RETO: genera el Excel y el log
├── bot.py                   EL EXTRA: abre el chat
├── revision/                código del reto
│   ├── config.py            rutas (cfdis/, salida/, la base)
│   ├── lectura.py           lectura y validación de un XML de CFDI
│   ├── base.py              base SQLite, carga de la carpeta y log de incidencias
│   └── reportes.py          detalle, resumen por proveedor, incidencias y el Excel
├── asistente/               código del bot (usa revision/)
│   ├── web.py               servidor del chat (Flask) y los botones del reto
│   ├── preguntas.py         lenguaje natural → SQL, y las herramientas que puede usar el modelo
│   ├── pdf.py               representación impresa del CFDI (con QR de verificación del SAT)
│   └── plantillas/chat.html página del chat
├── cfdis/                   XML de entrada
├── salida/                  reporte_cfdi.xlsx, incidencias.log, facturas.db y pdf/
├── tests/                   test_reto.py y test_bot.py, con los mismos XML de cfdis/
└── docs/                    instrucciones de la prueba (no se suben al repositorio)
```

Pruebas: `python -m pytest`. Las del bot imitan a OpenAI, así que no gastan llamadas ni necesitan llave.

## Decisiones

**Validación de cada archivo.** Cada XML se procesa dentro de su propio `try`: un archivo malo genera una
incidencia y el lote continúa. Se distinguen dos niveles:

- **ERROR** (no se carga): XML mal formado, raíz distinta de `cfdi:Comprobante` 3.3/4.0, sin
  `TimbreFiscalDigital`/UUID, sin Fecha/SubTotal/Total/RFC emisor, o UUID duplicado.
- **AVISO** (se carga, pero conviene revisarlo): RFC con formato inválido, totales que no cuadran
  (`SubTotal − Descuento + Traslados − Retenciones ≠ Total`, tolerancia $0.01), IVA en cero, moneda
  distinta de MXN, o comprobante que no es de ingreso.

**Duplicados.** Se conserva la primera copia (por orden de nombre de archivo) y las demás se reportan; así
el duplicado no infla el total del proveedor. "Detalle" tiene una fila por factura (UUID único).

**IVA trasladado.** Se suman solo los traslados con `Impuesto="002"` del nodo de impuestos del comprobante,
no `TotalImpuestosTrasladados`, que también incluye IEPS.

**Importes.** Las validaciones se hacen con `Decimal` para no arrastrar errores de redondeo de `float`;
el resumen redondea a 2 decimales.

**Resumen por proveedor.** Agrupa por RFC emisor (no por nombre, que puede venir escrito distinto), solo
facturas de ingreso (`tipo='I'`), ordenado por total facturado de mayor a menor, con fila de totales.

**Base de datos (SQLite).** No requiere servidor y permite consultar el histórico. La carga se puede repetir:
cada archivo reemplaza lo que se haya cargado antes desde él, así que correrla dos veces da el mismo
resultado y se pueden ir agregando carpetas de otros meses.

**Reto y bot separados.** `reto.py` hace solo lo que pide la prueba y no depende de la IA, de Flask ni de
reportlab. El bot reutiliza el mismo código (`revision/`) para los botones del reto, así que el Excel y las
cifras son idénticos por cualquiera de los dos caminos.

**IA (preguntas libres).** La IA no extrae los datos: leer un XML es exacto con código y no necesita un
modelo. Se usa donde aporta, para consultas libres. El modelo tiene tres herramientas: `consultar` (SQL),
`enviar_pdf` y `enviar_excel`; las dos últimas solo adjuntan archivos que genera el programa a partir de
la base. Para `consultar`, el modelo recibe el esquema y escribe SQL; el programa:

1. acepta solo una sentencia `SELECT`/`WITH`,
2. la ejecuta en una conexión de **solo lectura** (`mode=ro`), con límite de 10 s y de 50 filas,
3. si la consulta falla, le regresa el error al modelo para que la corrija,
4. muestra siempre el SQL ejecutado junto con el resultado, para poder auditarlo.

Al modelo solo se le envían el esquema y los resultados de las consultas, no los XML completos.

**PDF.** Es una representación impresa generada desde el XML; no sustituye al XML timbrado.

## Posibles mejoras

- Validar el XML contra el XSD del SAT y verificar la vigencia de cada UUID en el servicio del SAT.
- Clasificar el gasto por categoría con IA a partir de la descripción de los conceptos.
- Subir XML desde el chat, y llevar el bot a WhatsApp o Telegram.
