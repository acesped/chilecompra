
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import zlib
import math
import time
import random
import hashlib
import argparse
import statistics
import unicodedata

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont


# ==========================================
# CONFIGURACIÓN
# ==========================================

TZ = ZoneInfo("America/Santiago")

ANIO = int(os.getenv("ANIO", "2026"))
MAX_MB = int(os.getenv("MAX_MB_DESCARGA", "45"))
MAX_REGISTROS = int(os.getenv("MAX_REGISTROS", "15000"))
CASOS = int(os.getenv("CASOS_POR_EJECUCION", "1"))

IG_ID = os.getenv("IG_USER_ID", "")
IG_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")
GRAPH = f"https://graph.instagram.com/{GRAPH_VERSION}"

BASE = Path(
    os.getenv("RUNNER_TEMP", "/tmp")
) / "cuanto_paga_chile"

SITE = BASE / "site"
MEDIA = SITE / "media"

MEDIA.mkdir(parents=True, exist_ok=True)
(SITE / "index.html").write_text(
    "<!doctype html><html lang='es'>"
    "<meta charset='utf-8'>"
    "<title>Cuánto Paga Chile</title></html>",
    encoding="utf-8"
)

URLS = [
    f"https://data.open-contracting.org/es/publication/144/download?name={ANIO}.jsonl.gz",
    f"https://fastly.data.open-contracting.org/downloads/chile_compra_api_releases/3730/{ANIO}.jsonl.gz",
]

FUENTE_PUBLICA = "https://data.open-contracting.org/es/publication/144"

ISSUE_TITLE = "[BOT] Estado de publicaciones Cuánto Paga Chile"


# ==========================================
# UTILIDADES
# ==========================================

def normalizar(texto):
    texto = unicodedata.normalize(
        "NFKD", str(texto).lower()
    )
    texto = "".join(
        c for c in texto
        if not unicodedata.combining(c)
    )
    texto = re.sub(r"[^a-z0-9 ]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def numero(valor):
    if isinstance(valor, (int, float)):
        return float(valor) if math.isfinite(valor) else None

    if isinstance(valor, str):
        try:
            texto = valor.strip().replace(" ", "")
            if "," in texto and "." in texto:
                texto = texto.replace(".", "").replace(",", ".")
            elif "," in texto:
                texto = texto.replace(",", ".")
            resultado = float(texto)
            return resultado if math.isfinite(resultado) else None
        except ValueError:
            pass

    return None


def dinero(valor):
    return "$" + format(round(valor), ",").replace(",", ".")


def fecha_legible(valor):
    if not valor:
        return None

    try:
        fecha = datetime.fromisoformat(
            str(valor).replace("Z", "+00:00")
        )
        return fecha.strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return None


def fecha_ordenable(valor):
    if not valor:
        return None
    try:
        return str(valor)[:10]
    except (ValueError, TypeError):
        return None


def identificador(caso):
    cadena = (
        f"{caso['ocid']}::"
        f"{caso['award_id']}::"
        f"{caso['clave']}"
    )
    return hashlib.sha256(
        cadena.encode("utf-8")
    ).hexdigest()


def url_imagen(nombre):
    base = os.getenv(
        "GH_PAGES_BASE_URL", ""
    ).rstrip("/")

    if not base:
        repo = os.getenv("GITHUB_REPOSITORY", "")

        if "/" not in repo:
            raise RuntimeError(
                "No se pudo determinar la URL de GitHub Pages"
            )

        owner, repo_name = repo.split("/", 1)

        if repo_name.lower() == f"{owner.lower()}.github.io":
            base = f"https://{owner}.github.io"
        else:
            base = f"https://{owner}.github.io/{repo_name}"

    return f"{base}/media/{quote(nombre)}"


# ==========================================
# HISTORIAL PERSISTENTE EN GITHUB ISSUES
# ==========================================

ISSUE_NUMBER = None


def github_request(metodo, ruta, datos=None):
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")

    if not token or not repo:
        raise RuntimeError(
            "Faltan GITHUB_TOKEN o GITHUB_REPOSITORY"
        )

    respuesta = requests.request(
        metodo,
        f"https://api.github.com/repos/{repo}{ruta}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=datos,
        timeout=40,
    )

    respuesta.raise_for_status()

    if respuesta.content:
        return respuesta.json()

    return None


def cargar_estado():
    global ISSUE_NUMBER

    issues = github_request(
        "GET", "/issues?state=all&per_page=100"
    )

    for issue in issues:
        if (
            issue.get("title") == ISSUE_TITLE
            and "pull_request" not in issue
        ):
            ISSUE_NUMBER = issue["number"]
            try:
                estado = json.loads(
                    issue.get("body") or "{}"
                )
            except ValueError:
                raise RuntimeError(
                    "El historial de GitHub contiene JSON inválido"
                )

            return {
                "history": estado.get("history", []),
                "pending": estado.get("pending", []),
            }

    nuevo = github_request(
        "POST",
        "/issues",
        {
            "title": ISSUE_TITLE,
            "body": json.dumps({
                "history": [],
                "pending": [],
            }, ensure_ascii=False, indent=2),
        },
    )

    ISSUE_NUMBER = nuevo["number"]

    return {"history": [], "pending": []}


def guardar_estado(estado):
    github_request(
        "PATCH",
        f"/issues/{ISSUE_NUMBER}",
        {
            "body": json.dumps(
                estado,
                ensure_ascii=False,
                indent=2,
            )
        },
    )


# ==========================================
# DESCARGA DE DATOS REALES
# ==========================================

def descargar_datos():
    errores = []

    for url in URLS:
        registros = []
        buffer = b""
        recibidos = 0

        try:
            print("Consultando:", url)

            with requests.get(
                url,
                stream=True,
                timeout=(20, 90),
                headers={
                    "User-Agent": "CuantoPagaChile/1.0"
                },
            ) as respuesta:

                respuesta.raise_for_status()

                descompresor = zlib.decompressobj(
                    16 + zlib.MAX_WBITS
                )

                for chunk in respuesta.iter_content(
                    chunk_size=256 * 1024
                ):
                    if not chunk:
                        continue

                    recibidos += len(chunk)
                    buffer += descompresor.decompress(chunk)

                    lineas = buffer.split(b"\n")
                    buffer = lineas.pop()

                    for linea in lineas:
                        if not linea.strip():
                            continue

                        try:
                            registros.append(
                                json.loads(linea)
                            )
                        except (ValueError, UnicodeDecodeError):
                            continue

                        if len(registros) >= MAX_REGISTROS:
                            break

                    if (
                        recibidos >= MAX_MB * 1024 * 1024
                        or len(registros) >= MAX_REGISTROS
                    ):
                        break

            if registros:
                print(
                    "Registros descargados:",
                    len(registros)
                )
                return registros, url

        except Exception as exc:
            errores.append(str(exc))
            print("Error de fuente:", exc)

    raise RuntimeError(
        "No se pudieron obtener datos reales. "
        + " | ".join(errores)
    )


def obtener_releases(registro):
    if not isinstance(registro, dict):
        return []

    compilado = registro.get("compiledRelease")

    if isinstance(compilado, dict):
        return [compilado]

    releases = registro.get("releases")

    if isinstance(releases, list):
        return [
            x for x in releases
            if isinstance(x, dict)
        ]

    if "awards" in registro:
        return [registro]

    return []


def extraer_items(registros, origen):
    items = []

    for registro in registros:
        for release in obtener_releases(registro):

            buyer = release.get("buyer") or {}
            comprador = buyer.get("name", "No indicado")

            ocid = (
                release.get("ocid")
                or registro.get("ocid")
                or ""
            )

            fecha_registro = release.get("date", "")

            for award in release.get("awards") or []:
                if not isinstance(award, dict):
                    continue

                if str(
                    award.get("status", "")
                ).lower() in (
                    "cancelled",
                    "canceled",
                    "unsuccessful",
                ):
                    continue

                proveedores = award.get("suppliers") or []

                proveedor = "No indicado"

                if (
                    proveedores
                    and isinstance(proveedores[0], dict)
                ):
                    proveedor = proveedores[0].get(
                        "name", "No indicado"
                    )

                for item in award.get("items") or []:
                    if not isinstance(item, dict):
                        continue

                    descripcion = (
                        item.get("description")
                        or (
                            item.get("classification") or {}
                        ).get("description")
                        or ""
                    )

                    unidad = item.get("unit") or {}
                    if not isinstance(unidad, dict):
                        unidad = {}

                    valor = unidad.get("value") or {}
                    if not isinstance(valor, dict):
                        valor = {}

                    precio = numero(valor.get("amount"))

                    moneda = (
                        valor.get("currency")
                        or (
                            award.get("value") or {}
                        ).get("currency")
                    )

                    cantidad = numero(item.get("quantity"))

                    nombre_unidad = normalizar(
                        unidad.get("name")
                        or unidad.get("id")
                        or ""
                    )

                    clave = normalizar(descripcion)

                    if not (
                        ocid
                        and precio is not None
                        and precio > 0
                        and moneda == "CLP"
                        and len(clave) >= 12
                        and len(clave.split()) >= 2
                    ):
                        continue

                    if cantidad is not None and cantidad <= 0:
                        continue

                    items.append({
                        "ocid": ocid,
                        "award_id": award.get("id", ""),
                        "comprador": comprador,
                        "proveedor": proveedor,
                        "descripcion": descripcion,
                        "clave": clave,
                        "unidad": nombre_unidad,
                        "cantidad": cantidad,
                        "precio_unitario": precio,
                        "fecha_adjudicacion": award.get("date"),
                        "fecha_registro": fecha_registro,
                        "fuente": origen,
                    })

    return items


# ==========================================
# SELECCIÓN DE CASOS COMPARABLES
# ==========================================

def seleccionar_casos(items, estado):
    grupos = defaultdict(list)

    for item in items:
        grupos[
            (item["clave"], item["unidad"])
        ].append(item)

    usados = set(estado["history"])

    usados.update(
        p["unique_id"]
        for p in estado["pending"]
    )

    candidatos = []
    vistos = set()

    for grupo in grupos.values():

        # Una adjudicación por proceso para evitar
        # contar varias veces el mismo expediente.
        por_proceso = {}

        for item in grupo:
            por_proceso.setdefault(
                item["ocid"], item
            )

        valores = list(por_proceso.values())

        if len(valores) < 3:
            continue

        for caso in valores:

            if not caso.get("fecha_adjudicacion"):
                continue

            referencias = [
                r["precio_unitario"]
                for r in valores
                if r["ocid"] != caso["ocid"]
            ]

            if len(referencias) < 2:
                continue

            if (
                min(referencias) <= 0
                or max(referencias) / min(referencias) > 4
            ):
                continue

            uid = identificador(caso)

            if uid in usados or uid in vistos:
                continue

            vistos.add(uid)

            mediana = statistics.median(referencias)

            diferencia = (
                caso["precio_unitario"] / mediana - 1
            ) * 100

            candidatos.append({
                "unique_id": uid,
                "caso": caso,
                "referencia": mediana,
                "diferencia": diferencia,
                "n_referencias": len(referencias),
            })

    random.SystemRandom().shuffle(candidatos)

    seleccion = []
    procesos = set()

    for candidato in candidatos:
        ocid = candidato["caso"]["ocid"]

        if ocid in procesos:
            continue

        procesos.add(ocid)
        seleccion.append(candidato)

        if len(seleccion) >= CASOS:
            break

    return seleccion


# ==========================================
# TIPOGRAFÍAS Y DISEÑO
# ==========================================

def fuente(tamano, negrita=False):
    archivo = (
        "DejaVuSans-Bold.ttf"
        if negrita else
        "DejaVuSans.ttf"
    )

    rutas = [
        f"/usr/share/fonts/truetype/dejavu/{archivo}",
        f"/usr/share/fonts/truetype/liberation2/{archivo}",
    ]

    for ruta in rutas:
        try:
            return ImageFont.truetype(ruta, tamano)
        except OSError:
            pass

    try:
        return ImageFont.load_default(size=tamano)
    except TypeError:
        return ImageFont.load_default()


def crear_imagen(entry, destino):
    caso = entry["caso"]
    precio = dinero(caso["precio_unitario"])
    diferencia = entry["diferencia"]
    referencia = entry["referencia"]

    W, H = 1080, 1350

    imagen = Image.new("RGB", (W, H), "#0a1223")
    d = ImageDraw.Draw(imagen)

    def ancho(texto, f):
        caja = d.textbbox((0, 0), texto, font=f)
        return caja[2] - caja[0]

    def centrado(texto, y, tamano, color):
        while tamano > 18:
            f = fuente(tamano, True)
            if ancho(texto, f) <= 950:
                break
            tamano -= 2

        x = (W - ancho(texto, f)) // 2
        d.text((x, y), texto, font=f, fill=color)

    def lineas(texto, max_width, f):
        resultado = []
        actual = ""

        for palabra in str(texto).split():
            prueba = (actual + " " + palabra).strip()

            if ancho(prueba, f) <= max_width:
                actual = prueba
            else:
                if actual:
                    resultado.append(actual)
                actual = palabra

        if actual:
            resultado.append(actual)

        return resultado

    def bloque(texto, x, y, tamano, color, max_lineas=2):
        f = fuente(tamano)

        partes = lineas(texto, 940, f)

        for linea in partes[:max_lineas]:
            d.text(
                (x, y),
                linea,
                font=f,
                fill=color
            )
            y += tamano + 9

        return y

    # Panel principal
    d.rounded_rectangle(
        (35, 35, 1045, 670),
        radius=38,
        fill="#15243f"
    )

    centrado(
        "PRECIO UNITARIO ADJUDICADO",
        88, 28, "#c8d7f0"
    )

    centrado(precio, 155, 110, "#ffdf62")

    centrado(
        "DIFERENCIA VS OTRAS ADJUDICACIONES",
        320, 26, "#c8d7f0"
    )

    if diferencia > 0:
        color = "#ff5c5c"
    elif diferencia < 0:
        color = "#30d07d"
    else:
        color = "#ffd84d"

    porcentaje = (
        f"{diferencia:+.1f}%"
        if diferencia != 0 else "0.0%"
    )

    centrado(porcentaje, 385, 130, color)

    centrado(
        f"Mediana de referencia: {dinero(referencia)}",
        565, 33, "#f4f7fc"
    )

    # Información adicional
    d.text(
        (68, 714),
        "DETALLES DE LA ADJUDICACIÓN",
        font=fuente(30, True),
        fill="#f1f5fb"
    )

    d.line(
        (68, 760, 1012, 760),
        fill="#364760",
        width=2
    )

    fecha = fecha_legible(
        caso["fecha_adjudicacion"]
    ) or "No consta"

    campos = [
        ("ARTÍCULO", caso["descripcion"], 3),
        ("ORGANISMO", caso["comprador"], 2),
        ("PROVEEDOR ADJUDICADO", caso["proveedor"], 2),
        ("FECHA DE ADJUDICACIÓN", fecha, 1),
        ("IDENTIFICADOR", caso["ocid"], 2),
    ]

    y = 785

    for titulo, valor, limite in campos:
        if y >= 1190:
            break

        d.text(
            (70, y),
            titulo,
            font=fuente(21, True),
            fill="#75baff"
        )

        y += 30
        y = bloque(
            valor, 70, y, 26,
            "#f1f5fb", limite
        )
        y += 15

    # Fuente al pie: sin texto de consulta
    d.rectangle(
        (0, 1260, 1080, 1350),
        fill="#07101e"
    )

    d.text(
        (65, 1296),
        "Fuente: ChileCompra vía OCP",
        font=fuente(21, True),
        fill="#ffd28a"
    )

    imagen.save(destino, "JPEG", quality=92)


# ==========================================
# TEXTO PERIODÍSTICO
# ==========================================

def crear_caption(entry, todos):
    caso = entry["caso"]

    precio = dinero(caso["precio_unitario"])
    mediana = dinero(entry["referencia"])
    diferencia = entry["diferencia"]

    fecha = fecha_legible(
        caso["fecha_adjudicacion"]
    )

    if diferencia > 0:
        comparacion = f"{abs(diferencia):.1f}% superior"
    elif diferencia < 0:
        comparacion = f"{abs(diferencia):.1f}% inferior"
    else:
        comparacion = "igual"

    # Proveedores anteriores observados
    anteriores = set()

    for otro in todos:
        if (
            otro["ocid"] != caso["ocid"]
            and otro["clave"] == caso["clave"]
            and otro["unidad"] == caso["unidad"]
            and otro.get("fecha_adjudicacion")
            and fecha_ordenable(
                otro["fecha_adjudicacion"]
            ) < fecha_ordenable(
                caso["fecha_adjudicacion"]
            )
        ):
            anteriores.add(otro["proveedor"])

    texto = (
        "=== CUANTO PAGA CHILE? === 🔎 "
        "¿Qué muestran los registros de esta adjudicación pública? "
        "En los datos abiertos de compras públicas de Chile, "
        "publicados por Open Contracting Partnership a partir "
        "de información de ChileCompra, encontramos el proceso "
        f"{caso['ocid']}, en el que figura "
        f"{caso['comprador']} como organismo comprador y "
        f"{caso['proveedor']} como proveedor adjudicado. "
        f"El artículo descrito como «{caso['descripcion']}» "
        f"registra un precio unitario de {precio} pesos chilenos. "
    )

    if fecha:
        texto += (
            f"La adjudicación está fechada el {fecha}. "
        )

    texto += (
        "Para poner la cifra en contexto, contrastamos "
        f"el precio unitario de {precio} con otras "
        f"{entry['n_referencias']} adjudicaciones del archivo "
        "descargado que presentan una descripción normalizada "
        "y unidad de medida idénticas. "
        f"La mediana de esas referencias fue {mediana}, "
        f"por lo que el precio reportado resulta "
        f"{comparacion} a ese parámetro. "
    )

    if anteriores:
        nombres = ", ".join(sorted(anteriores)[:2])
        texto += (
            "Entre los antecedentes previos observados "
            "para el mismo artículo aparecen proveedores "
            f"como {nombres}. "
        )

    texto += (
        "NOTA: Las cifras corresponden a adjudicaciones, "
        "no necesariamente a pagos realizados. "
        f"Fuente: ChileCompra vía OCP ({FUENTE_PUBLICA}). "
        "#ComprasPúblicas #ChileCompra #DatosAbiertos "
        "#Transparencia #Chile"
    )

    if len(texto) > 2200:
        raise ValueError(
            "El pie supera el límite de caracteres "
            "de Instagram. Se omitirá este caso."
        )

    return texto


# ==========================================
# PREPARAR PUBLICACIONES
# ==========================================

def recuperar_pendientes(estado):
    for pendiente in estado["pending"]:
        nombre = pendiente["media_name"]
        ruta = MEDIA / nombre

        if ruta.exists():
            continue

        respuesta = requests.get(
            url_imagen(nombre),
            timeout=40
        )
        respuesta.raise_for_status()

        if "image/" not in respuesta.headers.get(
            "Content-Type", ""
        ):
            raise RuntimeError(
                "La imagen pendiente no está disponible"
            )

        ruta.write_bytes(respuesta.content)

    return estado["pending"]


def generar():
    estado = cargar_estado()

    if estado["pending"]:
        print("Existen publicaciones pendientes.")
        recuperar_pendientes(estado)
        return

    registros, origen = descargar_datos()
    items = extraer_items(registros, origen)

    if not items:
        raise RuntimeError(
            "No se encontraron adjudicaciones válidas."
        )

    seleccionados = seleccionar_casos(items, estado)

    if not seleccionados:
        raise RuntimeError(
            "No existen casos nuevos con suficientes "
            "referencias comparables en la muestra descargada."
        )

    pendientes = []

    for entry in seleccionados:
        uid = entry["unique_id"]
        nombre = uid[:24] + ".jpg"

        ruta = MEDIA / nombre

        try:
            caption = crear_caption(entry, items)
            crear_imagen(entry, ruta)
        except Exception as exc:
            print("Caso descartado:", exc)
            continue

        pendientes.append({
            "unique_id": uid,
            "ocid": entry["caso"]["ocid"],
            "media_name": nombre,
            "caption": caption,
            "published": {
                "instagram": None
            }
        })

        print(
            "Publicación preparada:",
            entry["caso"]["ocid"]
        )

    if not pendientes:
        raise RuntimeError(
            "No se consiguió preparar ninguna publicación."
        )

    estado["pending"] = pendientes
    guardar_estado(estado)


# ==========================================
# INSTAGRAM API
# ==========================================

def meta_request(metodo, ruta, datos=None):
    if not IG_TOKEN:
        raise RuntimeError(
            "Falta INSTAGRAM_ACCESS_TOKEN"
        )

    respuesta = requests.request(
        metodo,
        GRAPH + ruta,
        headers={
            "Authorization": f"Bearer {IG_TOKEN}"
        },
        params=datos if metodo == "GET" else None,
        data=datos if metodo != "GET" else None,
        timeout=45
    )

    try:
        cuerpo = respuesta.json()
    except ValueError:
        raise RuntimeError(
            f"Respuesta inválida de Instagram: "
            f"HTTP {respuesta.status_code}"
        )

    if respuesta.status_code >= 400 or "error" in cuerpo:
        raise RuntimeError(
            f"Instagram API: {cuerpo.get('error')}"
        )

    return cuerpo


def verificar_imagen_publica(url):
    for intento in range(24):
        try:
            respuesta = requests.get(
                url, timeout=25
            )

            if (
                respuesta.status_code == 200
                and "image/jpeg" in respuesta.headers.get(
                    "Content-Type", ""
                ).lower()
            ):
                return

        except requests.RequestException:
            pass

        time.sleep(10)

    raise RuntimeError(
        "GitHub Pages no está sirviendo la imagen JPG."
    )


def publicar_instagram(url, caption):
    if not IG_ID or not IG_TOKEN:
        raise RuntimeError(
            "Faltan IG_USER_ID o INSTAGRAM_ACCESS_TOKEN"
        )

    verificar_imagen_publica(url)

    # Crear contenedor de imagen
    contenedor = meta_request(
        "POST",
        f"/{IG_ID}/media",
        {
            "image_url": url,
            "caption": caption,
        }
    )["id"]

    # Esperar procesamiento
    for intento in range(18):
        estado = meta_request(
            "GET",
            f"/{contenedor}",
            {"fields": "status_code"}
        )

        status = estado.get("status_code")

        if status == "FINISHED":
            break

        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(
                f"Instagram rechazó el contenedor: {status}"
            )

        time.sleep(5)

    else:
        raise RuntimeError(
            "El contenedor no terminó de procesarse."
        )

    resultado = meta_request(
        "POST",
        f"/{IG_ID}/media_publish",
        {"creation_id": contenedor}
    )

    return resultado["id"]


# ==========================================
# PUBLICACIÓN E HISTORIAL
# ==========================================

def publicar():
    estado = cargar_estado()

    pendientes = estado["pending"]

    if not pendientes:
        print("No hay publicaciones pendientes.")
        return

    if os.getenv(
        "PUBLISH_ENABLED", "false"
    ).lower() != "true":
        print(
            "Modo de prueba: publicación desactivada."
        )
        for pendiente in pendientes:
            print(
                pendiente["ocid"],
                url_imagen(pendiente["media_name"])
            )
        return

    if not IG_ID or not IG_TOKEN:
        raise RuntimeError(
            "Configura los secretos de Instagram."
        )

    errores = []

    for pendiente in list(pendientes):
        uid = pendiente["unique_id"]

        # Caso previamente confirmado
        if pendiente.get("published", {}).get("instagram"):
            if uid not in estado["history"]:
                estado["history"].append(uid)

            pendientes.remove(pendiente)
            guardar_estado(estado)
            continue

        try:
            url = url_imagen(pendiente["media_name"])

            post_id = publicar_instagram(
                url, pendiente["caption"]
            )

            # Registrar inmediatamente el éxito
            pendiente.setdefault(
                "published", {}
            )["instagram"] = post_id

            guardar_estado(estado)

            if uid not in estado["history"]:
                estado["history"].append(uid)

            pendientes.remove(pendiente)
            guardar_estado(estado)

            print(
                f"Publicado en Instagram: {post_id}"
            )

        except Exception as exc:
            print(
                "Error publicando",
                pendiente["ocid"],
                str(exc)
            )
            errores.append(str(exc))

    if errores:
        raise RuntimeError(
            f"{len(errores)} publicaciones quedaron pendientes."
        )


# ==========================================
# EJECUCIÓN
# ==========================================

def main():
    parser = argparse.ArgumentParser(
        description="Cuánto Paga Chile - Instagram"
    )

    parser.add_argument(
        "command",
        choices=["generate", "publish"]
    )

    args = parser.parse_args()

    if args.command == "generate":
        generar()
    else:
        publicar()


if __name__ == "__main__":
    main()
