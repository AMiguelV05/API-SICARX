"""Cliente del webservice B2B de Grupo URREA (disponibilidad e informacion de producto).

Un solo endpoint (`POST settings.URREA_WS_URL`); `opcion` elige el servicio y las
credenciales viajan EN EL BODY JSON - por eso este modulo nunca loggea el body de una
peticion. Comportamiento real (probe de 2026-11-12), distinto del PDF de URREA en varios
puntos:

- Errores de autenticacion llegan con HTTP 200 y `{"status": "ERROR", "mensaje": ...}`.
- Opcion 2 (disponibilidad de un codigo para una cantidad): la fila trae `disponibilidad`
  = "Suficiente" | "Insuficiente" | "-" (codigo inexistente, con `descripcion` "Codigo no
  encontrado"). `fechaDisponibilidad` siempre es la fecha de consulta: no se usa.
- Opcion 4 (masiva): ~17k filas, ~22 MB, ~85 s. Se descarga a un archivo temporal y se lee
  en streaming con ijson para no cargar 100+ MB en el worker. `EstatusInventario` =
  "Disponible" | "Con reservas" | "No disponible", y es solo una pista: no siempre coincide
  con la opcion 2. `fotografia` es una LISTA de URLs; todo lo demas son strings, con "" o
  "0" como vacio.
- Opcion 1 (informacion por lista de codigos) devuelve lo mismo que la masiva (mismo precio
  incluido), por eso no se usa.

Cortacircuitos en proceso igual que typesense_client.py: tras un error de red, 5xx o error
de credenciales, URREA se da por caido BREAKER_SECONDS y las consultas fallan rapido.
"""
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

import httpx
import ijson

from app.core.config import settings

logger = logging.getLogger(__name__)

SUPPLIER = "URREA"
# Fila de supplier_sync_status del sync de la masiva.
BULK_JOB = "bulk_catalog"

# Consulta por cantidad (API, en linea): la opcion 2 tarda 1.3-2 s.
QUERY_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
# Masiva (worker): ~85 s observados.
BULK_TIMEOUT = httpx.Timeout(connect=10.0, read=240.0, write=10.0, pool=10.0)
BULK_ATTEMPTS = 2
BREAKER_SECONDS = 60

# Estatus normalizados de la masiva.
AVAILABLE = "AVAILABLE"
LIMITED = "LIMITED"
UNAVAILABLE = "UNAVAILABLE"
_BULK_STATUS = {
    "disponible": AVAILABLE,
    "con reservas": LIMITED,
    "con resevas": LIMITED,  # ortografia del PDF, por si acaso
    "no disponible": UNAVAILABLE,
}

# Solo embeds de YouTube con un id real (11 caracteres): la tienda los mete en un iframe. La
# masiva trae el placeholder ".../embed/0" en ~1,200 filas y basura como ".../embed/ 4000 PSI".
_YOUTUBE_EMBED = re.compile(r"^https://(www\.)?youtube(-nocookie)?\.com/embed/[A-Za-z0-9_-]{11}(\?[^\s]*)?$")

_down_until: float = 0.0
# Solo para pruebas: un httpx.MockTransport en lugar de la red real.
_transport: httpx.AsyncBaseTransport | None = None


class UrreaError(Exception):
    pass


class UrreaAuthError(UrreaError):
    """URREA rechazo usuario/contrasena (status ERROR)."""


def is_enabled() -> bool:
    return bool(settings.URREA_USER and settings.URREA_PASSWORD)


def is_available() -> bool:
    """Configurado y con el cortacircuitos cerrado."""
    return is_enabled() and time.monotonic() >= _down_until


def _trip(reason: str) -> None:
    global _down_until
    _down_until = time.monotonic() + BREAKER_SECONDS
    logger.warning(f"URREA marcado como caido por {BREAKER_SECONDS}s: {reason}")


def reset_breaker() -> None:
    global _down_until
    _down_until = 0.0


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


def _body(opcion: int, **extra: Any) -> dict:
    return {"opcion": opcion, "usuario": settings.URREA_USER, "password": settings.URREA_PASSWORD, **extra}


def _raise_for_status_field(data: Any, context: str) -> None:
    """URREA responde 200 aun con error: hay que revisar `status`."""
    if not isinstance(data, dict):
        raise UrreaError(f"{context}: respuesta inesperada de URREA (no es un objeto JSON).")
    status = str(data.get("status") or "").upper()
    if status == "OK":
        return
    message = str(data.get("mensaje") or data.get("message") or "")[:300]
    if "usuario" in message.lower() or "contrase" in message.lower():
        _trip("credenciales rechazadas")
        raise UrreaAuthError(f"{context}: URREA rechazo las credenciales: {message}")
    raise UrreaError(f"{context}: URREA respondio status={status or '?'}: {message}")


def _lower_keys(row: dict) -> dict:
    return {str(k).lower(): v for k, v in row.items()}


# --- Normalizacion de valores -----------------------------------------------------------

def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number_text(value: Any) -> str | None:
    """Numero como string; "" y "0" cuentan como vacio (asi los manda URREA)."""
    text = _text(value)
    if text is None:
        return None
    try:
        return text if Decimal(text.replace(",", ".")) != 0 else None
    except InvalidOperation:
        return text


def _decimal(value: Any) -> Decimal | None:
    text = _number_text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _int(value: Any) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _url(value: Any) -> str | None:
    text = _text(value)
    if not text or not text.lower().startswith(("http://", "https://")):
        return None
    if text.lower().startswith("http://"):
        # Los hosts de URREA sirven HTTPS; evita contenido mixto en la tienda.
        text = "https://" + text[len("http://"):]
    return text


def _urls(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for v in values:
        url = _url(v)
        if url and url not in out:
            out.append(url)
    return out


@dataclass(frozen=True)
class UrreaCatalogRow:
    """Una fila de la masiva (opcion 4), normalizada. Vacios -> None."""
    code: str
    status: str | None
    price: Decimal | None
    currency: str | None
    sale_multiple: int | None
    barcode: str | None
    long_name: str | None
    description: str | None
    brand: str | None
    sub_brand: str | None
    family: str | None
    product_class: str | None
    subclass: str | None
    bullets: str | None
    accessories: str | None
    warranty: str | None
    packaging: str | None
    keywords: str | None
    is_set: bool
    set_pieces: int | None
    set_contents_url: str | None
    sat_product_key: str | None
    sat_unit_key: str | None
    photo_urls: list[str] = field(default_factory=list)
    video_url: str | None = None
    datasheet_url: str | None = None
    manual_url: str | None = None
    # Medidas crudas del paquete (cm / kg); se validan al aplicarlas al PIM.
    height_cm: str | None = None
    depth_cm: str | None = None
    width_cm: str | None = None
    weight_kg: str | None = None

    def info(self) -> dict:
        """Lo que se guarda en supplier_products.info (sin precio ni estatus)."""
        return {
            "longName": self.long_name, "description": self.description, "brand": self.brand,
            "subBrand": self.sub_brand, "family": self.family, "class": self.product_class,
            "subclass": self.subclass, "bullets": self.bullets, "accessories": self.accessories,
            "warranty": self.warranty, "packaging": self.packaging, "keywords": self.keywords,
            "isSet": self.is_set, "setPieces": self.set_pieces,
            "satProductKey": self.sat_product_key, "satUnitKey": self.sat_unit_key,
            "package": {"heightCm": self.height_cm, "depthCm": self.depth_cm, "widthCm": self.width_cm, "weightKg": self.weight_kg},
        }


def parse_catalog_row(raw: dict) -> UrreaCatalogRow | None:
    r = _lower_keys(raw)
    code = _text(r.get("codigo"))
    if not code:
        return None
    video = _url(r.get("video"))
    if video and not _YOUTUBE_EMBED.match(video):
        video = None
    status_text = (_text(r.get("estatusinventario")) or "").lower()
    return UrreaCatalogRow(
        code=code,
        status=_BULK_STATUS.get(status_text),
        price=_decimal(r.get("precio")),
        currency=_text(r.get("moneda")),
        sale_multiple=_int(r.get("multiplo")),
        barcode=_number_text(r.get("codigobarras")),
        long_name=_text(r.get("nombrelargo")),
        description=_text(r.get("descripcionproducto")),
        brand=_text(r.get("marca")),
        sub_brand=_text(r.get("submarca")),
        family=_text(r.get("familia")),
        product_class=_text(r.get("clase")),
        subclass=_text(r.get("subclase")),
        bullets=_text(r.get("bullets")),
        accessories=_text(r.get("accesorios")),
        warranty=_text(r.get("garantia")),
        packaging=_text(r.get("empaque")),
        keywords=_text(r.get("keywords")),
        is_set=(_text(r.get("esjuego")) or "").lower() in ("si", "sí"),
        set_pieces=_int(r.get("piezasjuego")),
        set_contents_url=_url(r.get("contenidojuego")),
        sat_product_key=_number_text(r.get("anexo20sat")),
        sat_unit_key=_text(r.get("claveunidadsat")),
        photo_urls=_urls(r.get("fotografia") if r.get("fotografia") is not None else r.get("fotografiahd")),
        video_url=video,
        datasheet_url=_url(r.get("fichatecnica") if r.get("fichatecnica") is not None else r.get("ficharecnica")),
        manual_url=_url(r.get("manual")),
        height_cm=_number_text(r.get("alto")),
        depth_cm=_number_text(r.get("fondo")),
        width_cm=_number_text(r.get("ancho")),
        weight_kg=_number_text(r.get("peso")),
    )


# --- Opcion 2: disponibilidad por cantidad ----------------------------------------------

@dataclass(frozen=True)
class UrreaAvailability:
    code: str
    quantity: int
    found: bool  # False: URREA no conoce el codigo
    sufficient: bool  # URREA puede surtir `quantity` (alta probabilidad, no garantia)


async def check_availability(code: str, quantity: int) -> UrreaAvailability:
    """Opcion 2. Lanza UrreaError si URREA no esta configurado, esta caido (cortacircuitos) o
    responde con error; el llamador decide el fallback."""
    if not is_enabled():
        raise UrreaError("URREA no esta configurado (URREA_USER/URREA_PASSWORD).")
    if not is_available():
        raise UrreaError("URREA marcado como caido (cortacircuitos abierto).")
    context = f"Disponibilidad URREA de {code} x{quantity}"
    try:
        async with _client(QUERY_TIMEOUT) as client:
            response = await client.post(
                settings.URREA_WS_URL,
                json=_body(2, producto={"codigoProducto": code, "cantidad": str(quantity)}),
            )
    except httpx.HTTPError as e:
        _trip(f"{type(e).__name__} en opcion 2")
        raise UrreaError(f"{context}: error de red ({type(e).__name__}).") from e
    if response.status_code >= 500:
        _trip(f"HTTP {response.status_code} en opcion 2")
        raise UrreaError(f"{context}: URREA respondio {response.status_code}.")
    if response.status_code >= 400:
        raise UrreaError(f"{context}: URREA respondio {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except ValueError as e:
        raise UrreaError(f"{context}: respuesta no JSON: {response.text[:300]}") from e
    _raise_for_status_field(data, context)

    rows = next((v for k, v in data.items() if k.lower() == "disponibilidad" and isinstance(v, list)), None)
    if not rows or not isinstance(rows[0], dict):
        raise UrreaError(f"{context}: respuesta sin filas de disponibilidad.")
    row = _lower_keys(rows[0])
    verdict = (_text(row.get("disponibilidad")) or "").lower()
    if verdict == "suficiente":
        return UrreaAvailability(code, quantity, found=True, sufficient=True)
    if verdict == "insuficiente":
        return UrreaAvailability(code, quantity, found=True, sufficient=False)
    # "-" con descripcion "Codigo no encontrado"
    return UrreaAvailability(code, quantity, found=False, sufficient=False)


# --- Opcion 4: masiva -------------------------------------------------------------------

async def download_bulk_catalog() -> str:
    """Descarga la masiva a un archivo temporal y devuelve su ruta (el llamador lo borra).
    Valida `status` antes de devolver. Reintenta una vez ante error de red o 5xx."""
    if not is_enabled():
        raise UrreaError("URREA no esta configurado (URREA_USER/URREA_PASSWORD).")
    last_error: Exception | None = None
    for attempt in range(1, BULK_ATTEMPTS + 1):
        fd, path = tempfile.mkstemp(prefix="urrea_masiva_", suffix=".json")
        try:
            started = time.monotonic()
            with os.fdopen(fd, "wb") as fh:
                async with _client(BULK_TIMEOUT) as client:
                    async with client.stream("POST", settings.URREA_WS_URL, json=_body(4)) as response:
                        if response.status_code >= 400:
                            await response.aread()
                            raise UrreaError(f"Masiva URREA: HTTP {response.status_code}: {response.text[:300]}")
                        async for chunk in response.aiter_bytes():
                            fh.write(chunk)
            size = os.path.getsize(path)
            logger.info(f"Masiva URREA descargada: {size / 1_048_576:.1f} MiB en {time.monotonic() - started:.0f} s.")
            _check_bulk_status(path)
            return path
        except UrreaAuthError:
            os.unlink(path)
            raise
        except (httpx.HTTPError, UrreaError) as e:
            os.unlink(path)
            last_error = e
            if attempt < BULK_ATTEMPTS:
                logger.warning(f"Masiva URREA fallo en el intento {attempt}/{BULK_ATTEMPTS} ({type(e).__name__}: {e}); reintentando.")
        except BaseException:
            os.unlink(path)
            raise
    if isinstance(last_error, UrreaError):
        raise last_error
    raise UrreaError(f"Masiva URREA: error de red ({type(last_error).__name__}).") from last_error


def _check_bulk_status(path: str) -> None:
    """Lee solo el principio del archivo: `status` es la primera clave."""
    with open(path, "rb") as fh:
        try:
            for prefix, event, value in ijson.parse(fh):
                if prefix == "" and event == "map_key" and value.lower() not in ("status", "mensaje", "message"):
                    # Paso `status` (o no viene) y ya empezaron los datos.
                    return
                if prefix.lower() == "status" and event == "string":
                    if value.upper() == "OK":
                        return
                    break
        except ijson.JSONError as e:
            raise UrreaError(f"Masiva URREA: respuesta no es JSON valido ({e}).") from e
    with open(path, "rb") as fh:
        try:
            data = json.load(fh)
        except ValueError as e:
            raise UrreaError(f"Masiva URREA: respuesta no es JSON valido ({e}).") from e
    _raise_for_status_field(data, "Masiva URREA")


def iter_bulk_rows(path: str) -> Iterator[UrreaCatalogRow]:
    """Recorre la masiva en streaming (sin cargarla entera). La clave de la lista se busca sin
    distinguir mayusculas (`resultadoDispMasiva`)."""
    with open(path, "rb") as fh:
        list_key = None
        for prefix, event, value in ijson.parse(fh):
            if prefix == "" and event == "map_key" and value.lower() == "resultadodispmasiva":
                list_key = value
                break
    if list_key is None:
        raise UrreaError("Masiva URREA: no se encontro la lista resultadoDispMasiva.")
    with open(path, "rb") as fh:
        for raw in ijson.items(fh, f"{list_key}.item"):
            if isinstance(raw, dict):
                row = parse_catalog_row(raw)
                if row is not None:
                    yield row
