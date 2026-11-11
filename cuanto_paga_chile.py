
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CUANTO PAGA CHILE?
Datos abiertos OCDS de ChileCompra vía Open Contracting Partnership.

Publicación exclusivamente en Instagram mediante Instagram Login.
Imágenes alojadas en una rama pública independiente de GitHub.
Historial persistente en GitHub Issues.

Dependencias:
    pip install requests Pillow

Comandos:
    python cuanto_paga_chile.py check-instagram
    python cuanto_paga_chile.py generate
    python cuanto_paga_chile.py publish
"""

import os
import re
import json
import zlib
import math
import time
import base64
import random
import hashlib
import argparse
import statistics
import unicodedata

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont


# ==========================================
# CONFIGURACION
# ==========================================

TZ = ZoneInfo("America/Santiago")

ANIO = int(
    os.getenv(
        "ANIO",
        str(datetime.now(TZ).year)
    )
)

MAX_MB_DESCARGA = int(
    os.getenv("MAX_MB_DESCARGA", "45")
)

MAX_REGISTROS = int(
    os.getenv("MAX_REGISTROS", "15000")
)

CASOS_POR_EJECUCION = int(
    os.getenv("CASOS_POR_EJECUCION", "1")
)

PUBLISH_ENABLED = (
    os.getenv("PUBLISH_ENABLED", "false").lower()
    == "true"
)

GRAPH_VERSION = os.getenv(
    "META_GRAPH_VERSION",
    "v25.0"
)

IG_GRAPH_BASE = os.getenv(
    "IG_GRAPH_BASE",
    "https://graph.instagram.com"
).rstrip("/")

GRAPH = f"{IG_GRAPH_BASE}/{GRAPH_VERSION}"

IG_EXPECTED_USERNAME = os.getenv(
    "IG_EXPECTED_USERNAME",
    "cuantopagachile"
).strip().lower().lstrip("@")

MEDIA_BRANCH = os.getenv(
    "IG_MEDIA_BRANCH",
    "instagram-media"
)

MEDIA_PATH = "cuanto_paga_chile/imagenes"

ISSUE_TITLE = (
    "[BOT] Estado de publicaciones Cuánto Paga Chile"
)

OCP_URL = (
    "https://data.open-contracting.org/"
    "es/publication/144"
)

URLS = [
    (
        f"https://data.open-contracting.org/"
        f"es/publication/144/download?"
        f"name={ANIO}.jsonl.gz"
    ),
    (
        "https://fastly.data.open-contracting.org/"
        "downloads/chile_compra_api_releases/"
        f"3730/{ANIO}.jsonl.gz"
    ),
]

CARPETA = (
    Path(os.getenv("RUNNER_TEMP", "/tmp"))
    / "cuanto_paga_chile"
)

MEDIA = CARPETA / "site" / "media"

MEDIA.mkdir(parents=True, exist_ok=True)

(CARPETA / "site" / "index.html").write_text(
    "<!doctype html><html lang='es'>"
    "<meta charset='utf-8'>"
    "<title>Cuánto Paga Chile</title>",
    encoding="utf-8"
)

ISSUE_NUMBER = None
STATE = None


# ==========================================
# UTILIDADES
# ==========================================

def log(*partes):
    print(*partes, flush=True)


def normalizar(texto):
    texto = unicodedata.normalize(
        "NFKD",
        str(texto).lower()
    )

    texto = "".join(
        c for c in texto
        if not unicodedata.combining(c)
    )

    texto = re.sub(
        r"[^a-z0-9 ]",
        " ",
        texto
    )

    return re.sub(
        r"\s+",
        " ",
        texto
    ).strip()


def numero(valor):
    if isinstance(valor, (int, float)):
        resultado = float(valor)
        return (
            resultado
            if math.isfinite(resultado)
            else None
        )

    if isinstance(valor, str):
        try:
            texto = valor.strip().replace(" ", "")

            if "," in texto and "." in texto:
                texto = texto.replace(
                    ".", ""
                ).replace(",", ".")
            elif "," in texto:
                texto = texto.replace(",", ".")

            resultado = float(texto)

            return (
                resultado
                if math.isfinite(resultado)
                else None
            )

        except ValueError:
            pass

    return None


def dinero(valor):
    return "$" + format(
        round(valor),
        ","
    ).replace(",", ".")


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

    return str(valor)[:10]


def fecha_consulta():
    return datetime.now(TZ).strftime("%d-%m-%Y")


def safe_error(exc):
    """
    Nunca mostrar secretos en los registros.
    """

    mensaje = str(exc)

    for nombre in (
        "GITHUB_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "INSTAGRAM_TOKEN",
    ):
        secreto = os.getenv(nombre, "")

        if secreto:
            mensaje = mensaje.replace(
                secreto,
                "[REDACTADO]"
            )

    return mensaje[:350]


def identificador(caso):
    """
    Compatible con los identificadores
    utilizados por la versión anterior.
    """

    return (
        f"{caso['ocid']}::"
        f"{caso.get('award_id', '')}::"
        f"{caso['clave']}"
    )


# ==========================================
# GITHUB API
# ==========================================

def github_request(
    metodo,
    ruta,
    datos=None
):
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")

    if not token or not repo:
        raise RuntimeError(
            "Faltan GITHUB_TOKEN "
            "o GITHUB_REPOSITORY"
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
        timeout=60,
    )

    if not respuesta.ok:
        raise RuntimeError(
            f"GitHub API HTTP "
            f"{respuesta.status_code}: "
            f"{respuesta.text[:250]}"
        )

    return (
        respuesta.json()
        if respuesta.content
        else None
    )


# ==========================================
# GITHUB ISSUES: HISTORIAL
# ==========================================

def state_load():
    global ISSUE_NUMBER

    issues = github_request(
        "GET",
        "/issues?state=all&per_page=100"
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
                    "El historial contiene JSON inválido"
                )

            return {
                "history": estado.get(
                    "history", []
                ),
                "pending": estado.get(
                    "pending", []
                ),
                "abandoned": estado.get(
                    "abandoned", []
                ),
                "uncertain": estado.get(
                    "uncertain", []
                ),
            }

    nuevo = github_request(
        "POST",
        "/issues",
        {
            "title": ISSUE_TITLE,
            "body": json.dumps(
                {
                    "history": [],
                    "pending": [],
                    "abandoned": [],
                    "uncertain": [],
                },
                ensure_ascii=False,
                indent=2
            ),
        }
    )

    ISSUE_NUMBER = nuevo["number"]

    return {
        "history": [],
        "pending": [],
        "abandoned": [],
        "uncertain": [],
    }


def state_save(estado):
    if not ISSUE_NUMBER:
        raise RuntimeError(
            "El historial no está inicializado"
        )

    github_request(
        "PATCH",
        f"/issues/{ISSUE_NUMBER}",
        {
            "body": json.dumps(
                estado,
                ensure_ascii=False,
                indent=2
            )
        }
    )


def init_state():
    global STATE

    if STATE is None:
        STATE = state_load()

    return STATE


# ==========================================
# CREDENCIALES INSTAGRAM
# ==========================================

def instagram_credentials():
    """
    Obtiene el token de Instagram Login.

    Comprueba formato, pero nunca revela
    ni imprime el token.
    """

    raw = os.getenv(
        "INSTAGRAM_ACCESS_TOKEN"
    )

    if not raw:
        raw = os.getenv(
            "INSTAGRAM_TOKEN",
            ""
        )

    user = (
        os.getenv("IG_USER_ID")
        or os.getenv("INSTAGRAM_USER_ID")
        or ""
    ).strip()

    if not raw:
        raise RuntimeError(
            "INSTAGRAM_ACCESS_TOKEN está vacío "
            "o no fue entregado por GitHub Actions"
        )

    if raw != raw.strip():
        raise RuntimeError(
            "INSTAGRAM_ACCESS_TOKEN contiene "
            "espacios o saltos de línea "
            "al inicio o al final"
        )

    token = raw.strip()

    if token.lower().startswith("bearer "):
        raise RuntimeError(
            "El secreto no debe incluir "
            "el prefijo Bearer"
        )

    if (
        token.startswith(("'", '"'))
        or token.endswith(("'", '"'))
    ):
        raise RuntimeError(
            "El secreto contiene comillas. "
            "Guarda únicamente el token."
        )

    if any(
        caracter.isspace()
        for caracter in token
    ):
        raise RuntimeError(
            "El secreto contiene espacios "
            "o saltos de línea internos"
        )

    if not user or not user.isdigit():
        raise RuntimeError(
            "IG_USER_ID debe contener "
            "el ID numérico profesional "
            "de Instagram"
        )

    log(
        "INSTAGRAM:",
        "secreto presente;",
        "formato básico correcto;",
        "ID numérico configurado."
    )

    return user, token


# ==========================================
# META GRAPH API
# ==========================================

def meta_request(
    metodo,
    ruta,
    token,
    datos=None,
    timeout=75
):
    """
    Método utilizado por SISMONAUTA:
    el token se envía como access_token.

    GET  -> params
    POST -> form data
    """

    campos = {
        **(datos or {}),
        "access_token": token,
    }

    url = GRAPH + "/" + ruta.lstrip("/")

    try:
        if metodo == "GET":
            respuesta = requests.get(
                url,
                params=campos,
                timeout=timeout,
            )
        else:
            respuesta = requests.post(
                url,
                data=campos,
                timeout=timeout,
            )

    except requests.RequestException as exc:
        raise RuntimeError(
            "No se pudo conectar con Instagram: "
            + type(exc).__name__
        ) from exc

    try:
        resultado = respuesta.json()

    except ValueError:
        raise RuntimeError(
            "Instagram no devolvió una "
            "respuesta JSON válida. "
            f"HTTP {respuesta.status_code}"
        )

    if not respuesta.ok or resultado.get("error"):
        error = resultado.get("error") or {}

        codigo = error.get("code", "?")

        mensaje = str(
            error.get(
                "message",
                "Error no especificado"
            )
        )

        mensaje = mensaje.replace(
            token,
            "[REDACTADO]"
        )[:240]

        if str(codigo) == "190":
            raise RuntimeError(
                "Instagram OAuth 190: "
                + mensaje
                + ". Meta rechazó el token. "
                "Genera un token de Instagram Login "
                "para @cuantopagachile, actualiza "
                "INSTAGRAM_ACCESS_TOKEN en GitHub "
                "Secrets y revisa si existe otro "
                "secreto del mismo nombre en "
                "GitHub Environments."
            )

        raise RuntimeError(
            f"Instagram API HTTP "
            f"{respuesta.status_code}, "
            f"código {codigo}: {mensaje}"
        )

    return resultado


# ==========================================
# VERIFICAR INSTAGRAM
# ==========================================

def instagram_check():
    """
    Verifica autenticación e identidad
    sin intentar publicar.

    Compatible con los campos id/user_id
    de Instagram Login.
    """

    user, token = instagram_credentials()

    try:
        info = meta_request(
            "GET",
            "me",
            token,
            {
                "fields": "id,user_id,username"
            }
        )

    except RuntimeError as exc:
        mensaje = str(exc).lower()

        if (
            "user_id" not in mensaje
            or "190" in mensaje
        ):
            raise

        info = meta_request(
            "GET",
            "me",
            token,
            {
                "fields": "id,username"
            }
        )

    username = str(
        info.get("username") or ""
    ).strip().lower().lstrip("@")

    if not username:
        raise RuntimeError(
            "Instagram respondió sin username. "
            "Comprueba el permiso "
            "instagram_business_basic."
        )

    if (
        IG_EXPECTED_USERNAME
        and username != IG_EXPECTED_USERNAME
    ):
        raise RuntimeError(
            "El token corresponde a "
            f"@{username}, no a "
            f"@{IG_EXPECTED_USERNAME}"
        )

    ids_permitidos = {
        str(info.get("id") or ""),
        str(info.get("user_id") or ""),
    }

    if user not in ids_permitidos:
        perfil = meta_request(
            "GET",
            user,
            token,
            {
                "fields": "id,username"
            }
        )

        perfil_id = str(
            perfil.get("id") or ""
        )

        perfil_usuario = str(
            perfil.get("username") or ""
        ).strip().lower().lstrip("@")

        if (
            perfil_id != user
            or perfil_usuario != username
        ):
            raise RuntimeError(
                "IG_USER_ID no corresponde "
                "a la cuenta autorizada "
                "por este token"
            )

    log(
        "INSTAGRAM CHECK OK:",
        f"@{username}",
        "| ID de publicación confirmado."
    )

    return user, token


# ==========================================
# ALOJAMIENTO DE IMAGENES EN GITHUB
# ==========================================

def media_url(nombre):
    repo = os.getenv(
        "GITHUB_REPOSITORY",
        ""
    )

    if "/" not in repo:
        raise RuntimeError(
            "GITHUB_REPOSITORY no configurado"
        )

    return (
        f"https://raw.githubusercontent.com/"
        f"{repo}/"
        f"{quote(MEDIA_BRANCH, safe='')}/"
        f"{MEDIA_PATH}/"
        f"{quote(nombre)}"
    )


def legacy_pages_url(nombre):
    base = os.getenv(
        "GH_PAGES_BASE_URL",
        ""
    ).rstrip("/")

    if not base:
        repo = os.getenv(
            "GITHUB_REPOSITORY",
            ""
        )

        owner, repository = repo.split(
            "/",
            1
        )

        if repository.lower() == (
            owner.lower() + ".github.io"
        ):
            base = (
                f"https://{owner}.github.io"
            )
        else:
            base = (
                f"https://{owner}.github.io/"
                f"{repository}"
            )

    return (
        f"{base}/media/{quote(nombre)}"
    )


def ensure_media_branch():
    """
    Crea la rama de imágenes si no existe.

    No modifica la rama principal.
    """

    repo = os.getenv(
        "GITHUB_REPOSITORY",
        ""
    )

    token = os.getenv(
        "GITHUB_TOKEN",
        ""
    )

    detalles = github_request(
        "GET",
        ""
    )

    if detalles.get("private"):
        raise RuntimeError(
            "El repositorio debe ser público "
            "para utilizar imágenes raw públicas."
        )

    encabezados = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    referencia = quote(
        MEDIA_BRANCH,
        safe=""
    )

    url = (
        f"https://api.github.com/repos/"
        f"{repo}/git/ref/heads/{referencia}"
    )

    respuesta = requests.get(
        url,
        headers=encabezados,
        timeout=30,
    )

    if respuesta.status_code == 200:
        return

    if respuesta.status_code != 404:
        raise RuntimeError(
            "No se pudo consultar la rama "
            f"de imágenes: HTTP "
            f"{respuesta.status_code}"
        )

    principal = detalles["default_branch"]

    referencia_principal = github_request(
        "GET",
        "/git/ref/heads/"
        + quote(principal, safe="")
    )

    sha = referencia_principal[
        "object"
    ]["sha"]

    try:
        github_request(
            "POST",
            "/git/refs",
            {
                "ref": (
                    f"refs/heads/{MEDIA_BRANCH}"
                ),
                "sha": sha,
            }
        )

        log(
            "Rama de imágenes creada:",
            MEDIA_BRANCH
        )

    except RuntimeError:
        comprobacion = requests.get(
            url,
            headers=encabezados,
            timeout=30,
        )

        if comprobacion.status_code != 200:
            raise


def put_media(nombre, ruta):
    """
    Guarda un JPG de manera permanente
    en la rama instagram-media.

    Si el archivo ya existe con contenido
    diferente, no lo sobrescribe.
    """

    ensure_media_branch()

    repo = os.getenv(
        "GITHUB_REPOSITORY",
        ""
    )

    token = os.getenv(
        "GITHUB_TOKEN",
        ""
    )

    archivo = f"{MEDIA_PATH}/{nombre}"

    endpoint = (
        "/contents/"
        + quote(archivo, safe="/")
    )

    url = (
        f"https://api.github.com/repos/"
        f"{repo}{endpoint}"
    )

    encabezados = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    contenido = Path(ruta).read_bytes()

    blob = (
        f"blob {len(contenido)}\0"
    ).encode("utf-8")

    git_sha = hashlib.sha1(
        blob + contenido
    ).hexdigest()

    respuesta = requests.get(
        url,
        headers=encabezados,
        params={"ref": MEDIA_BRANCH},
        timeout=30,
    )

    if respuesta.status_code == 200:
        remoto = respuesta.json()

        if remoto.get("sha") != git_sha:
            raise RuntimeError(
                "La imagen remota ya existe "
                "con contenido distinto. "
                "No se sobrescribirá."
            )

        return media_url(nombre)

    if respuesta.status_code != 404:
        raise RuntimeError(
            "Error consultando imagen remota: "
            f"HTTP {respuesta.status_code}"
        )

    datos = {
        "message": (
            f"Cuánto Paga Chile: {nombre}"
        ),
        "branch": MEDIA_BRANCH,
        "content": base64.b64encode(
            contenido
        ).decode("ascii"),
    }

    try:
        github_request(
            "PUT",
            endpoint,
            datos
        )

    except Exception:
        comprobacion = requests.get(
            url,
            headers=encabezados,
            params={"ref": MEDIA_BRANCH},
            timeout=30,
        )

        if comprobacion.status_code == 200:
            remoto = comprobacion.json()

            if remoto.get("sha") == git_sha:
                return media_url(nombre)

        raise

    return media_url(nombre)


def verificar_imagen_publica(url):
    ultimo_error = "Sin respuesta"

    for intento in range(15):
        try:
            respuesta = requests.get(
                url,
                stream=True,
                timeout=25,
            )

            try:
                tipo = respuesta.headers.get(
                    "Content-Type",
                    ""
                ).lower()

                if (
                    respuesta.ok
                    and tipo.startswith("image/")
                ):
                    return True

                ultimo_error = (
                    f"HTTP {respuesta.status_code}"
                )

            finally:
                respuesta.close()

        except requests.RequestException as exc:
            ultimo_error = type(exc).__name__

        if intento < 14:
            time.sleep(4)

    raise RuntimeError(
        "La imagen pública no está "
        "disponible para Instagram: "
        + ultimo_error
    )


# ==========================================
# RECUPERAR PENDIENTES
# ==========================================

def restore_pending_media():
    """
    Recupera imágenes desde la rama
    permanente o desde GitHub Pages antiguo.

    Si no existe una imagen y nunca fue
    enviada, archiva el caso sin marcarlo
    como publicado.
    """

    estado = init_state()

    pendientes = estado.get(
        "pending",
        []
    )

    if not pendientes:
        return []

    recuperados = []
    modificacion = False

    abandonados = estado.setdefault(
        "abandoned",
        []
    )

    for pendiente in pendientes:
        status = pendiente.get(
            "instagram_status",
            "pending"
        )

        if status in (
            "uncertain",
            "sending",
        ):
            log(
                "Envío incierto:",
                pendiente.get("ocid"),
                "No reintentar automáticamente."
            )

            recuperados.append(pendiente)
            continue

        nombre = pendiente["media_name"]

        ruta = MEDIA / nombre

        problema = None

        if not ruta.is_file():
            for url in (
                media_url(nombre),
                legacy_pages_url(nombre),
            ):
                try:
                    respuesta = requests.get(
                        url,
                        timeout=35,
                    )

                    respuesta.raise_for_status()

                    tipo = respuesta.headers.get(
                        "Content-Type",
                        ""
                    ).lower()

                    if not tipo.startswith("image/"):
                        raise ValueError(
                            "El recurso no es una imagen"
                        )

                    ruta.write_bytes(
                        respuesta.content
                    )

                    with Image.open(ruta) as img:
                        img.verify()

                    problema = None

                    log(
                        "Imagen pendiente recuperada:",
                        nombre
                    )

                    break

                except (
                    requests.RequestException,
                    OSError,
                    ValueError,
                ) as exc:
                    problema = (
                        type(exc).__name__
                        + ": "
                        + str(exc)[:150]
                    )

                    ruta.unlink(
                        missing_ok=True
                    )

        if problema:
            modificacion = True

            uid = pendiente["unique_id"]

            if pendiente.get(
                "published",
                {}
            ).get("instagram"):
                if uid not in estado["history"]:
                    estado["history"].append(uid)

            elif not any(
                p.get("unique_id") == uid
                for p in abandonados
            ):
                abandonados.append({
                    "unique_id": uid,
                    "ocid": pendiente.get(
                        "ocid", ""
                    ),
                    "status": "NO_PUBLICADO",
                    "reason": problema,
                })

            log(
                "Imagen antigua no recuperable:",
                pendiente.get("ocid")
            )

            continue

        recuperados.append(pendiente)

    if modificacion:
        estado["pending"] = recuperados
        state_save(estado)

    return recuperados


# ==========================================
# DESCARGA DE DATOS OCDS
# ==========================================

def descargar_registros(
    url,
    max_mb=MAX_MB_DESCARGA,
    max_records=MAX_REGISTROS
):
    log(
        "Consultando datos abiertos OCDS:",
        url
    )

    registros = []
    buffer = b""
    recibidos = 0

    with requests.get(
        url,
        stream=True,
        timeout=(20, 90),
        headers={
            "User-Agent": (
                "CuantoPagaChile/2.0 "
                "(open contracting data research)"
            )
        },
    ) as respuesta:

        respuesta.raise_for_status()

        descompresor = zlib.decompressobj(
            16 + zlib.MAX_WBITS
        )

        for bloque in respuesta.iter_content(
            chunk_size=256 * 1024
        ):
            if not bloque:
                continue

            recibidos += len(bloque)

            buffer += descompresor.decompress(
                bloque
            )

            lineas = buffer.split(b"\n")
            buffer = lineas.pop()

            for linea in lineas:
                if not linea.strip():
                    continue

                try:
                    registros.append(
                        json.loads(linea)
                    )

                except (
                    ValueError,
                    UnicodeDecodeError,
                ):
                    continue

                if len(registros) >= max_records:
                    break

            if (
                recibidos >= max_mb * 1024 * 1024
                or len(registros) >= max_records
            ):
                break

    log(
        "Registros descargados:",
        len(registros),
        "| MB:",
        round(recibidos / 1048576, 2)
    )

    if not registros:
        raise RuntimeError(
            "La fuente no devolvió registros "
            "OCDS válidos."
        )

    return registros


def obtener_datos():
    errores = []

    for url in URLS:
        try:
            registros = descargar_registros(
                url
            )

            if registros:
                return registros, url

        except Exception as exc:
            errores.append(
                safe_error(exc)
            )

            log(
                "Fuente no disponible:",
                safe_error(exc)
            )

    raise RuntimeError(
        "No fue posible consultar datos "
        "abiertos reales. "
        + " | ".join(errores)
    )


def iterar_releases(registro):
    if not isinstance(
        registro,
        dict
    ):
        return

    compilado = registro.get(
        "compiledRelease"
    )

    if isinstance(compilado, dict):
        yield compilado
        return

    releases = registro.get(
        "releases"
    )

    if isinstance(releases, list):
        for release in releases:
            if isinstance(release, dict):
                yield release
        return

    if (
        "awards" in registro
        or "tender" in registro
    ):
        yield registro


def extraer_items(
    registros,
    fuente
):
    items = []

    for registro in registros:
        for release in iterar_releases(
            registro
        ):
            comprador = (
                (release.get("buyer") or {})
                .get("name")
                or "Organismo no indicado"
            )

            ocid = (
                release.get("ocid")
                or registro.get("ocid")
                or ""
            )

            fecha_registro = (
                release.get("date") or ""
            )

            for award in release.get(
                "awards", []
            ) or []:
                if not isinstance(award, dict):
                    continue

                status = str(
                    award.get("status", "")
                ).lower()

                if status in (
                    "cancelled",
                    "canceled",
                    "unsuccessful",
                ):
                    continue

                proveedores = award.get(
                    "suppliers"
                ) or []

                proveedor = "No indicado"

                if (
                    proveedores
                    and isinstance(
                        proveedores[0],
                        dict
                    )
                ):
                    proveedor = (
                        proveedores[0].get("name")
                        or "No indicado"
                    )

                for item in award.get(
                    "items", []
                ) or []:
                    if not isinstance(item, dict):
                        continue

                    descripcion = (
                        item.get("description")
                        or (
                            item.get(
                                "classification"
                            ) or {}
                        ).get("description")
                        or ""
                    )

                    unidad = (
                        item.get("unit") or {}
                    )

                    if not isinstance(
                        unidad,
                        dict
                    ):
                        unidad = {}

                    valor = (
                        unidad.get("value") or {}
                    )

                    if not isinstance(
                        valor,
                        dict
                    ):
                        valor = {}

                    precio = numero(
                        valor.get("amount")
                    )

                    moneda = (
                        valor.get("currency")
                        or (
                            award.get("value") or {}
                        ).get("currency")
                    )

                    cantidad = numero(
                        item.get("quantity")
                    )

                    nombre_unidad = (
                        unidad.get("name")
                        or unidad.get("id")
                        or ""
                    )

                    clave = normalizar(
                        descripcion
                    )

                    if not (
                        ocid
                        and precio is not None
                        and precio > 0
                        and moneda == "CLP"
                        and len(clave) >= 12
                        and len(clave.split()) >= 2
                    ):
                        continue

                    if (
                        cantidad is not None
                        and cantidad <= 0
                    ):
                        continue

                    items.append({
                        "ocid": ocid,
                        "award_id": award.get(
                            "id", ""
                        ),
                        "fecha_adjudicacion": (
                            award.get("date") or ""
                        ),
                        "fecha_registro": (
                            fecha_registro
                        ),
                        "comprador": comprador,
                        "proveedor": proveedor,
                        "descripcion": descripcion,
                        "clave": clave,
                        "precio_unitario": precio,
                        "cantidad": cantidad,
                        "unidad": normalizar(
                            nombre_unidad
                        ),
                        "moneda": "CLP",
                        "fuente": fuente,
                    })

    return items


# ==========================================
# COMPARACION DE ADJUDICACIONES
# ==========================================

def seleccionar_casos(
    items,
    estado
):
    grupos = defaultdict(list)

    for item in items:
        grupos[
            (
                item["clave"],
                item["unidad"]
            )
        ].append(item)

    comparables = []

    for grupo in grupos.values():
        por_proceso = {}

        for item in grupo:
            if item["ocid"]:
                por_proceso.setdefault(
                    item["ocid"],
                    item
                )

        valores = list(
            por_proceso.values()
        )

        if len(valores) < 3:
            continue

        for caso in valores:
            if not caso.get(
                "fecha_adjudicacion"
            ):
                continue

            referencias = [
                r["precio_unitario"]
                for r in valores
                if r["ocid"] != caso["ocid"]
            ]

            if len(referencias) < 2:
                continue

            if min(referencias) <= 0:
                continue

            if (
                max(referencias)
                / min(referencias)
                > 4
            ):
                continue

            mediana = statistics.median(
                referencias
            )

            diferencia = (
                caso["precio_unitario"]
                / mediana
                - 1
            ) * 100

            comparables.append({
                "caso": caso,
                "ref": mediana,
                "dif": diferencia,
                "nref": len(referencias),
            })

    historial = set(
        estado.get("history", [])
    )

    for categoria in (
        "pending",
        "abandoned",
        "uncertain",
    ):
        historial.update(
            item["unique_id"]
            for item in estado.get(
                categoria, []
            )
            if "unique_id" in item
        )

    random.SystemRandom().shuffle(
        comparables
    )

    seleccionados = []
    procesos = set()
    vistos = set()

    for entry in comparables:
        caso = entry["caso"]

        uid = identificador(
            caso
        )

        if (
            uid in historial
            or uid in vistos
            or caso["ocid"] in procesos
        ):
            continue

        vistos.add(uid)
        procesos.add(caso["ocid"])

        entry["unique_id"] = uid

        seleccionados.append(entry)

        if (
            len(seleccionados)
            >= CASOS_POR_EJECUCION
        ):
            break

    return seleccionados


# ==========================================
# TIPOGRAFIA Y DISEÑO
# ==========================================

def fuente(
    tamano,
    negrita=False
):
    nombre = (
        "DejaVuSans-Bold.ttf"
        if negrita
        else "DejaVuSans.ttf"
    )

    rutas = [
        (
            "/usr/share/fonts/"
            "truetype/dejavu/"
            + nombre
        ),
        (
            "/usr/share/fonts/"
            "truetype/liberation2/"
            + nombre
        ),
    ]

    for ruta in rutas:
        try:
            return ImageFont.truetype(
                ruta,
                tamano
            )

        except OSError:
            pass

    try:
        return ImageFont.load_default(
            size=tamano
        )

    except TypeError:
        return ImageFont.load_default()


def crear_imagen(
    entry,
    destino
):
    caso = entry["caso"]

    precio = dinero(
        caso["precio_unitario"]
    )

    diferencia = entry["dif"]

    referencia = entry["ref"]

    W = 1080
    H = 1350

    imagen = Image.new(
        "RGB",
        (W, H),
        "#0a1223"
    )

    d = ImageDraw.Draw(
        imagen
    )

    def ancho(texto, f):
        caja = d.textbbox(
            (0, 0),
            str(texto),
            font=f
        )

        return caja[2] - caja[0]

    def centrado(
        texto,
        y,
        tamano,
        color
    ):
        f = fuente(
            tamano,
            True
        )

        while (
            tamano > 18
            and ancho(texto, f) > 950
        ):
            tamano -= 2

            f = fuente(
                tamano,
                True
            )

        x = (
            W - ancho(texto, f)
        ) // 2

        d.text(
            (x, y),
            texto,
            font=f,
            fill=color
        )

    def lineas(
        texto,
        f,
        max_width=940
    ):
        resultado = []
        actual = ""

        for palabra in str(
            texto
        ).split():
            prueba = (
                actual + " " + palabra
            ).strip()

            if (
                ancho(prueba, f)
                <= max_width
            ):
                actual = prueba
            else:
                if actual:
                    resultado.append(
                        actual
                    )

                actual = palabra

        if actual:
            resultado.append(
                actual
            )

        return resultado

    def bloque(
        texto,
        x,
        y,
        tamano,
        color,
        max_lineas=2
    ):
        f = fuente(
            tamano
        )

        partes = lineas(
            texto,
            f
        )

        for linea in partes[
            :max_lineas
        ]:
            d.text(
                (x, y),
                linea,
                font=f,
                fill=color
            )

            y += tamano + 9

        return y

    # Panel superior
    d.rounded_rectangle(
        (35, 35, 1045, 670),
        radius=38,
        fill="#15243f"
    )

    centrado(
        "PRECIO UNITARIO ADJUDICADO",
        88,
        28,
        "#c8d7f0"
    )

    centrado(
        precio,
        155,
        110,
        "#ffdf62"
    )

    centrado(
        "DIFERENCIA VS OTRAS ADJUDICACIONES",
        320,
        26,
        "#c8d7f0"
    )

    if diferencia > 0:
        color = "#ff5c5c"
    elif diferencia < 0:
        color = "#30d07d"
    else:
        color = "#ffd84d"

    porcentaje = (
        f"{diferencia:+.1f}%"
        if diferencia != 0
        else "0.0%"
    )

    centrado(
        porcentaje,
        385,
        130,
        color
    )

    centrado(
        "Mediana de referencia: "
        + dinero(referencia),
        565,
        33,
        "#f4f7fc"
    )

    # Información inferior
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

    fecha = (
        fecha_legible(
            caso["fecha_adjudicacion"]
        )
        or "No consta"
    )

    campos = [
        (
            "ARTÍCULO",
            caso["descripcion"],
            3,
        ),
        (
            "ORGANISMO",
            caso["comprador"],
            2,
        ),
        (
            "PROVEEDOR ADJUDICADO",
            caso["proveedor"],
            2,
        ),
        (
            "FECHA DE ADJUDICACIÓN",
            fecha,
            1,
        ),
        (
            "IDENTIFICADOR",
            caso["ocid"],
            2,
        ),
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
            valor,
            70,
            y,
            26,
            "#f1f5fb",
            limite
        )

        y += 15

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

    imagen.save(
        destino,
        "JPEG",
        quality=92,
        optimize=True
    )


# ==========================================
# PIE PERIODISTICO
# ==========================================

def crear_caption(
    entry,
    todos
):
    caso = entry["caso"]

    precio = dinero(
        caso["precio_unitario"]
    )

    mediana = dinero(
        entry["ref"]
    )

    diferencia = entry["dif"]

    fecha = fecha_legible(
        caso["fecha_adjudicacion"]
    )

    if diferencia > 0:
        comparacion = (
            f"{abs(diferencia):.1f}% superior"
        )
    elif diferencia < 0:
        comparacion = (
            f"{abs(diferencia):.1f}% inferior"
        )
    else:
        comparacion = "igual"

    anteriores = set()

    fecha_caso = fecha_ordenable(
        caso["fecha_adjudicacion"]
    )

    if fecha_caso:
        for otro in todos:
            fecha_otro = fecha_ordenable(
                otro.get("fecha_adjudicacion")
            )

            if (
                otro["ocid"] != caso["ocid"]
                and otro["clave"] == caso["clave"]
                and otro["unidad"] == caso["unidad"]
                and fecha_otro
                and fecha_otro < fecha_caso
            ):
                anteriores.add(
                    otro["proveedor"]
                )

    texto = (
        "=== CUANTO PAGA CHILE? === 🔎 "
        "¿Qué muestran los registros de esta "
        "adjudicación pública?/n "

        "En los datos abiertos de compras "
        "públicas de Chile a partir "
        "de información de ChileCompra, "
        f"encontramos el proceso {caso['ocid']}, "
        f"en el que figura {caso['comprador']} "
        "como organismo comprador y "
        f"{caso['proveedor']} como proveedor "
        "adjudicado./n "

        f"El artículo descrito como "
        f"«{caso['descripcion']}» registra "
        f"un precio unitario de {precio} "
        "pesos chilenos. "
    )

    if fecha:
        texto += (
            f"La adjudicación está fechada "
            f"el {fecha}./n "
        )

    texto += (
        "Para poner la cifra en contexto, "
        f"contrastamos el precio unitario "
        f"de {precio} con otras "
        f"{entry['nref']} adjudicaciones "
        "del archivo descargado que presentan "
        "una descripción normalizada y unidad "
        "de medida idénticas. "

        f"La mediana de esas referencias fue "
        f"{mediana}, por lo que el precio "
        f"reportado resulta {comparacion} "
        "a ese parámetro. "
    )

    if anteriores:
        nombres = ", ".join(
            sorted(anteriores)[:2]
        )

        texto += (
            "Entre los antecedentes previos "
            "observados para el mismo artículo "
            "aparecen proveedores como "
            f"{nombres}./n "
        )

    texto += (
        "NOTA: Las cifras corresponden a "
        "adjudicaciones, no necesariamente "
        "a pagos realizados. "

        f"Fuente: ChileCompra vía OCP "
        f"({OCP_URL}). "

        f"Consulta: {fecha_consulta()}. "

        "#ComprasPúblicas #ChileCompra "
        "#DatosAbiertos #Transparencia #Chile"
    )

    if len(texto) > 2200:
        raise ValueError(
            "El pie supera los "
            "2200 caracteres de Instagram"
        )

    return texto


# ==========================================
# GENERAR PUBLICACIONES
# ==========================================

def generar():
    """
    Primero valida Instagram cuando
    la publicación real está activada.

    Así no se crean nuevos casos pendientes
    si el token de Meta es inválido.
    """

    if PUBLISH_ENABLED:
        instagram_check()

    estado = init_state()

    # Recuperar publicaciones anteriores.
    if estado["pending"]:
        if PUBLISH_ENABLED:
            recuperados = restore_pending_media()

            seguros = [
                p for p in recuperados
                if p.get(
                    "instagram_status",
                    "pending"
                ) == "pending"
            ]

            if seguros:
                log(
                    "Casos pendientes recuperados:",
                    len(seguros)
                )
                return

            inciertos = [
                p for p in estado["pending"]
                if p.get("instagram_status")
                in ("uncertain", "sending")
            ]

            for pendiente in inciertos:
                if not any(
                    x.get("unique_id")
                    == pendiente["unique_id"]
                    for x in estado["uncertain"]
                ):
                    estado["uncertain"].append(
                        pendiente
                    )

            estado["pending"] = []

            state_save(estado)

        else:
            log(
                "Modo prueba: sin publicación real."
            )

            inciertos = [
                p for p in estado["pending"]
                if p.get("instagram_status")
                in ("uncertain", "sending")
            ]

            for pendiente in inciertos:
                if not any(
                    x.get("unique_id")
                    == pendiente["unique_id"]
                    for x in estado["uncertain"]
                ):
                    estado["uncertain"].append(
                        pendiente
                    )

            estado["pending"] = []

            state_save(estado)

    registros, fuente = obtener_datos()

    items = extraer_items(
        registros,
        fuente
    )

    log(
        "Ítems válidos encontrados:",
        len(items)
    )

    if not items:
        raise RuntimeError(
            "No se encontraron adjudicaciones "
            "con precio unitario CLP verificable."
        )

    seleccionados = seleccionar_casos(
        items,
        estado
    )

    if not seleccionados:
        raise RuntimeError(
            "No se encontraron casos nuevos "
            "con referencias comparables. "
            "Amplía MAX_REGISTROS o utiliza "
            "otro ANIO. No se repetirá "
            "un caso publicado."
        )

    nuevos = []

    for entry in seleccionados:
        uid = entry["unique_id"]

        nombre = (
            hashlib.sha256(
                uid.encode("utf-8")
            ).hexdigest()[:24]
            + ".jpg"
        )

        destino = MEDIA / nombre

        try:
            caption = crear_caption(
                entry,
                items
            )

            crear_imagen(
                entry,
                destino
            )

        except Exception as exc:
            log(
                "Caso descartado:",
                safe_error(exc)
            )

            continue

        respaldo = {
            "unique_id": uid,
            "ocid": entry["caso"]["ocid"],
            "media_name": nombre,
            "caption": caption,
            "instagram_status": "pending",
            "published": {
                "instagram": None
            },
        }

        nuevos.append(
            respaldo
        )

        # Copia local del análisis.
        carpeta_caso = (
            CARPETA
            / hashlib.sha256(
                uid.encode("utf-8")
            ).hexdigest()[:12]
        )

        carpeta_caso.mkdir(
            parents=True,
            exist_ok=True
        )

        (carpeta_caso / "caption.txt").write_text(
            caption,
            encoding="utf-8"
        )

        (carpeta_caso / "respaldo.json").write_text(
            json.dumps(
                {
                    "fuente": fuente,
                    "fecha_consulta": fecha_consulta(),
                    "caso": entry["caso"],
                    "mediana": entry["ref"],
                    "diferencia_pct": entry["dif"],
                    "n_referencias": entry["nref"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8"
        )

        log(
            "Publicación preparada:",
            entry["caso"]["ocid"]
        )

    if not nuevos:
        raise RuntimeError(
            "No se consiguió generar "
            "ninguna publicación válida."
        )

    estado["pending"] = nuevos

    state_save(estado)

    log(
        "Publicaciones preparadas:",
        len(nuevos)
    )


# ==========================================
# PUBLICACION EN INSTAGRAM
# ==========================================

def instagram_publish(
    image_url,
    caption,
    user,
    token
):
    """
    1. Crear contenedor.
    2. Esperar procesamiento.
    3. Publicar el contenedor.

    Solo se invoca después de guardar
    un estado de envío incierto.
    """

    respuesta = meta_request(
        "POST",
        f"{user}/media",
        token,
        {
            "image_url": image_url,
            "caption": caption,
        }
    )

    contenedor = respuesta.get(
        "id"
    )

    if not contenedor:
        raise RuntimeError(
            "Instagram no devolvió "
            "ID del contenedor"
        )

    log(
        "Contenedor de Instagram creado:",
        contenedor
    )

    for intento in range(18):
        time.sleep(8)

        estado = meta_request(
            "GET",
            str(contenedor),
            token,
            {
                "fields": "status_code"
            }
        )

        status = estado.get(
            "status_code"
        )

        if status == "FINISHED":
            break

        if status in (
            "ERROR",
            "EXPIRED",
        ):
            raise RuntimeError(
                "Instagram rechazó el "
                "contenedor: "
                + str(status)
            )

    else:
        raise RuntimeError(
            "El contenedor no terminó "
            "de procesarse."
        )

    resultado = meta_request(
        "POST",
        f"{user}/media_publish",
        token,
        {
            "creation_id": contenedor
        }
    )

    post_id = resultado.get(
        "id"
    )

    if not post_id:
        raise RuntimeError(
            "Instagram no devolvió ID "
            "de publicación. "
            "Revisar manualmente."
        )

    return str(post_id)


# ==========================================
# PUBLICAR Y MANTENER HISTORIAL
# ==========================================

def publicar():
    """
    Publica solamente en Instagram.

    Evita repetir automáticamente
    operaciones de resultado incierto.
    """

    estado = init_state()

    pendientes = estado[
        "pending"
    ]

    if not pendientes:
        log(
            "No hay publicaciones pendientes."
        )
        return

    if not PUBLISH_ENABLED:
        log(
            "Publicación desactivada. "
            "Solo vista previa."
        )
        return

    # Autenticación antes del primer POST.
    user, token = instagram_check()

    errores = []

    for pendiente in list(
        pendientes
    ):
        uid = pendiente["unique_id"]

        publicado = pendiente.get(
            "published",
            {}
        ).get("instagram")

        if publicado:
            if uid not in estado["history"]:
                estado["history"].append(
                    uid
                )

            pendientes.remove(
                pendiente
            )

            state_save(estado)

            continue

        status = pendiente.get(
            "instagram_status",
            "pending"
        )

        if status in (
            "uncertain",
            "sending",
        ):
            log(
                "SIN REINTENTO:",
                pendiente.get("ocid"),
                "envío de resultado incierto."
            )
            continue

        if status != "pending":
            raise RuntimeError(
                "Estado de Instagram "
                "desconocido: "
                + str(status)
            )

        # Preparación de imagen ANTES del envío.
        try:
            nombre = pendiente[
                "media_name"
            ]

            archivo = MEDIA / nombre

            if not archivo.is_file():
                restore_pending_media()

            if not archivo.is_file():
                raise RuntimeError(
                    "Imagen pendiente no disponible"
                )

            if pendiente not in estado["pending"]:
                raise RuntimeError(
                    "El caso fue archivado "
                    "durante la recuperación."
                )

            url = put_media(
                nombre,
                archivo
            )

            verificar_imagen_publica(
                url
            )

            pendiente[
                "image_url"
            ] = url

            state_save(
                estado
            )

        except Exception as exc:
            error = (
                pendiente["ocid"]
                + ": "
                + safe_error(exc)
            )

            log(
                "ERROR ANTES DEL ENVÍO:",
                error
            )

            errores.append(
                error
            )

            continue

        # Guardar el riesgo ANTES del POST.
        # Si GitHub Actions se interrumpe,
        # no volver a publicar a ciegas.
        pendiente[
            "instagram_status"
        ] = "uncertain"

        pendiente[
            "started_at"
        ] = datetime.now(
            TZ
        ).isoformat()

        state_save(
            estado
        )

        try:
            post_id = instagram_publish(
                url,
                pendiente["caption"],
                user,
                token
            )

            pendiente.setdefault(
                "published",
                {}
            )["instagram"] = post_id

            pendiente[
                "instagram_status"
            ] = "published"

            state_save(
                estado
            )

            if uid not in estado["history"]:
                estado["history"].append(
                    uid
                )

            pendientes.remove(
                pendiente
            )

            state_save(
                estado
            )

            log(
                "INSTAGRAM PUBLICADO:",
                post_id,
                "| Caso:",
                pendiente["ocid"]
            )

        except Exception as exc:
            mensaje = safe_error(
                exc
            )

            pendiente[
                "instagram_error"
            ] = mensaje

            try:
                state_save(
                    estado
                )

            except Exception as state_exc:
                log(
                    "ERROR GUARDANDO HISTORIAL:",
                    safe_error(state_exc)
                )

            errores.append(
                pendiente["ocid"]
                + ": "
                + mensaje
            )

            log(
                "ENVÍO INCIERTO:",
                pendiente["ocid"],
                "No se reintentará automáticamente."
            )

    if errores:
        raise RuntimeError(
            f"{len(errores)} publicación(es) "
            "fallaron o requieren revisión "
            "manual en Instagram y GitHub Issues."
        )


# ==========================================
# EJECUCION
# ==========================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Cuánto Paga Chile - "
            "Datos abiertos e Instagram"
        )
    )

    parser.add_argument(
        "command",
        choices=[
            "check-instagram",
            "generate",
            "publish",
        ],
    )

    args = parser.parse_args()

    if args.command == "check-instagram":
        instagram_check()

    elif args.command == "generate":
        generar()

    elif args.command == "publish":
        publicar()


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        log(
            "ERROR:",
            safe_error(exc)
        )

        raise SystemExit(1)
