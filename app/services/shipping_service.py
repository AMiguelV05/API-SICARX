import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.upstream_errors import raise_upstream_error
from app.models.order import Order

# API de envios de envia.com (necesita ENVIA_API_TOKEN) - distinta de su API de Geocodes, sin llave, que llama el frontend directo.
# Dominio depende de ENVIA_ENVIRONMENT: un token sandbox contra el dominio de produccion da 401 enganoso (parece invalido, es solo mismatch de ambiente).
ENVIA_BASE_URL = "https://api-test.envia.com" if settings.ENVIA_ENVIRONMENT == "sandbox" else "https://api.envia.com"
RATE_URL = f"{ENVIA_BASE_URL}/ship/rate/"
GENERATE_URL = f"{ENVIA_BASE_URL}/ship/generate/"
CANCEL_URL = f"{ENVIA_BASE_URL}/ship/cancel/"
# Queries API - catalogo de referencia de envia.com, dominio distinto al de envios, tambien separado por ambiente.
QUERIES_BASE_URL = "https://queries-test.envia.com" if settings.ENVIA_ENVIRONMENT == "sandbox" else "https://queries.envia.com"
SHIPPING_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
# ~24 carriers reales en el catalogo de Mexico - secuencial seria muy lento para una llamada sincrona del dashboard admin.
MAX_CONCURRENT_RATE_REQUESTS = 8
# El catalogo de carriers no cambia seguido - evita una llamada de red extra en cada cotizacion.
CARRIER_CATALOG_TTL = timedelta(hours=24)

logger = logging.getLogger(__name__)

_carrier_catalog_cache: dict[str, tuple[datetime, list[str]]] = {}
_carrier_catalog_lock = asyncio.Lock()

# envia.com: `state` debe medir <=3 caracteres (ISO 3166-2:MX), o rechaza la peticion para TODOS los carriers por igual (200 con meta:error, indistinguible de "sin cobertura").
# _normalize_mx_state convierte nombres completos/variantes de los 32 estados a ese codigo; un valor no reconocido >3 chars se trunca con WARNING en vez de fallar la peticion completa.
_MX_STATE_ALIASES = {
    "AGUASCALIENTES": "AGU",
    "BAJACALIFORNIA": "BCN",
    "BAJACALIFORNIANORTE": "BCN",
    "BAJACALIFORNIASUR": "BCS",
    "CAMPECHE": "CAM",
    "CHIAPAS": "CHP",
    "CHIHUAHUA": "CHH",
    "CIUDADDEMEXICO": "CMX",
    "CDMX": "CMX",
    "DISTRITOFEDERAL": "CMX",
    "MEXICOCITY": "CMX",
    "COAHUILA": "COA",
    "COAHUILADEZARAGOZA": "COA",
    "COLIMA": "COL",
    "DURANGO": "DUR",
    "GUANAJUATO": "GUA",
    "GUERRERO": "GRO",
    "HIDALGO": "HID",
    "JALISCO": "JAL",
    "MEXICO": "MEX",
    "ESTADODEMEXICO": "MEX",
    "EDOMEX": "MEX",
    "EDODEMEXICO": "MEX",
    "MICHOACAN": "MIC",
    "MICHOACANDEOCAMPO": "MIC",
    "MORELOS": "MOR",
    "NAYARIT": "NAY",
    "NUEVOLEON": "NLE",
    "OAXACA": "OAX",
    "PUEBLA": "PUE",
    "QUERETARO": "QUE",
    "QUINTANAROO": "ROO",
    "SANLUISPOTOSI": "SLP",
    "SINALOA": "SIN",
    "SONORA": "SON",
    "TABASCO": "TAB",
    "TAMAULIPAS": "TAM",
    "TLAXCALA": "TLA",
    "VERACRUZ": "VER",
    "VERACRUZDEIGNACIODELALLAVE": "VER",
    "YUCATAN": "YUC",
    "ZACATECAS": "ZAC",
}


def _normalize_mx_state(value: str | None) -> str | None:
    if not value:
        return value
    cleaned = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).strip().upper()
    if len(cleaned) <= 3:
        return cleaned
    alnum = re.sub(r"[^A-Z]", "", cleaned)
    code = _MX_STATE_ALIASES.get(alnum)
    if code:
        return code
    logger.warning(f"envia.com: no se reconoce el estado '{value}' - se trunca a 3 caracteres ('{cleaned[:3]}') en vez de fallar la cotizacion completa")
    return cleaned[:3]


def _envia_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.ENVIA_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _origin_address(overrides: dict | None = None) -> dict:
    """Direccion de la tienda (ENVIA_ORIGIN_* por defecto). `overrides` reemplaza campos
    individuales por pedido (nunca todo o nada); `country`/`phone_code` no son overrideables.
    `state` siempre pasa por `_normalize_mx_state`, venga de `overrides` o de `.env`."""
    overrides = overrides or {}

    def pick(key: str, default: str) -> str:
        value = overrides.get(key)
        return value if value else default

    return {
        "name": pick("name", settings.ENVIA_ORIGIN_NAME),
        "company": pick("company", settings.ENVIA_ORIGIN_COMPANY),
        "email": pick("email", settings.ENVIA_ORIGIN_EMAIL),
        "phone_code": settings.ENVIA_PHONE_CODE,
        "phone": pick("phone", settings.ENVIA_ORIGIN_PHONE),
        "street": pick("street", settings.ENVIA_ORIGIN_STREET),
        "number": pick("number", settings.ENVIA_ORIGIN_NUMBER),
        "district": pick("district", settings.ENVIA_ORIGIN_DISTRICT),
        "city": pick("city", settings.ENVIA_ORIGIN_CITY),
        "state": _normalize_mx_state(pick("state", settings.ENVIA_ORIGIN_STATE)),
        "country": settings.ENVIA_COUNTRY,
        "postalCode": pick("zip_code", settings.ENVIA_ORIGIN_ZIP_CODE),
        "reference": pick("reference", settings.ENVIA_ORIGIN_REFERENCE),
    }


def _destination_address(order: Order) -> dict:
    """Campos de direccion vienen de `order.delivery_address_snapshot`; `name`/`phone`/`email`
    de `order.delivery_info["contactInfo"]` (contacto real del checkout), no del address book.
    `company` siempre None (cliente final, a diferencia de _origin_address). `state` pasa por
    `_normalize_mx_state` antes de mandarse a envia.com."""
    snapshot = order.delivery_address_snapshot or {}
    contact_info = (order.delivery_info or {}).get("contactInfo") or {}
    return {
        "name": contact_info.get("name") or snapshot.get("label") or "Cliente",
        "company": None,
        "email": contact_info.get("email"),
        "phone_code": settings.ENVIA_PHONE_CODE,
        "phone": contact_info.get("phone"),
        "street": snapshot.get("street"),
        "number": snapshot.get("extNumber"),
        "district": snapshot.get("neighborhood"),
        "city": snapshot.get("city"),
        "state": _normalize_mx_state(snapshot.get("state")),
        "country": settings.ENVIA_COUNTRY,
        "postalCode": snapshot.get("zipCode"),
        "reference": snapshot.get("references"),
    }


def _build_packages(weight: float, length: float, width: float, height: float) -> list[dict]:
    """envia.com exige las medidas anidadas bajo un objeto `dimensions`, no como campos
    planos del paquete."""
    return [{
        "content": "Ferreteria",
        "amount": 1,
        "type": "box",
        "weight": weight,
        "weightUnit": "KG",
        "dimensions": {
            "length": length,
            "width": width,
            "height": height,
            "unit": "CM",
        },
    }]


def missing_address_fields(snapshot: dict | None) -> list[str]:
    if not snapshot:
        return ["deliveryAddress"]
    return [field for field in ("street", "city", "state", "zipCode") if not snapshot.get(field)]


async def _fetch_available_carriers() -> list[str]:
    """Catalogo real de carriers para MX vía la Queries API (`international=0`,
    `shipment_type_id=1`) - reemplaza una lista fija que podia quedar desactualizada.
    Cacheado en memoria por CARRIER_CATALOG_TTL."""
    cache_key = settings.ENVIA_COUNTRY
    async with _carrier_catalog_lock:
        cached = _carrier_catalog_cache.get(cache_key)
        if cached and datetime.now(timezone.utc) - cached[0] < CARRIER_CATALOG_TTL:
            return cached[1]

        url = f"{QUERIES_BASE_URL}/available-carrier/{settings.ENVIA_COUNTRY}/0/1"
        async with httpx.AsyncClient(timeout=SHIPPING_TIMEOUT) as client:
            response = await client.get(url, headers=_envia_headers())

        if response.status_code not in (200, 201):
            raise_upstream_error(
                response,
                "envia.com rechazo la consulta del catalogo de carriers disponibles",
                "No se pudo obtener el catálogo de paqueterías de envia.com.",
            )

        body = response.json()
        raw_carriers = body.get("data") if isinstance(body, dict) else body
        names = [c["name"] for c in raw_carriers if isinstance(c, dict) and c.get("name")] if isinstance(raw_carriers, list) else []

        _carrier_catalog_cache[cache_key] = (datetime.now(timezone.utc), names)
        return names


async def get_shipping_quote(order: Order, weight: float, length: float, width: float, height: float, origin_overrides: dict | None = None) -> list[dict]:
    """Cotiza opciones de envio con envia.com para `order`. No persiste nada, se puede
    llamar repetidamente mientras el admin ajusta peso/medidas. Solo devuelve opciones
    puerta a puerta (`dropOff: 0`) - ver el filtro en `_quote_one`.

    POST /ship/rate/ exige un `carrier` especifico (no hay comodin "all"/"*") - se llama
    una vez por carrier en paralelo (tope MAX_CONCURRENT_RATE_REQUESTS, ~24 carriers
    reales). Un carrier no disponible responde HTTP 200 con `meta:"error"` y se omite.

    Si ningun carrier produjo opcion Y todos los `meta:error` comparten el mismo mensaje,
    se asume un problema estructural de la peticion (no falta de cobertura) y se levanta
    un 502 en vez de `200 {"options": []}` silencioso."""
    origin = _origin_address(origin_overrides)
    destination = _destination_address(order)
    packages = _build_packages(weight, length, width, height)
    carriers = await _fetch_available_carriers()

    options: list[dict] = []
    last_error_response = None
    meta_error_messages: list[str] = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RATE_REQUESTS)

    async def _quote_one(client: httpx.AsyncClient, carrier: str):
        nonlocal last_error_response
        payload = {"origin": origin, "destination": destination, "packages": packages, "carrier": carrier}
        async with semaphore:
            response = await client.post(RATE_URL, json=payload, headers=_envia_headers())

        if response.status_code not in (200, 201):
            # Falla de transporte/auth (no meta:error) - se recuerda para el 502 de abajo.
            last_error_response = response
            return []

        body = response.json()
        if isinstance(body, dict) and body.get("meta") == "error":
            message = (body.get("error") or {}).get("message")
            logger.warning(
                f"envia.com: carrier '{carrier}' no disponible para la orden {order.uuid}: {message}"
            )
            if isinstance(message, str) and message:
                meta_error_messages.append(message)
            return []

        raw_options = body.get("data") if isinstance(body, dict) else body
        if isinstance(raw_options, dict):
            raw_options = raw_options.get("rates") or raw_options.get("options") or []
        if not isinstance(raw_options, list):
            raw_options = []

        parsed = []
        for raw in raw_options:
            if not isinstance(raw, dict):
                continue
            if raw.get("dropOff"):
                # dropOff!=0 exige un branch_code en /ship/generate/ que no recolectamos -
                # se descartan; solo se ofrecen opciones puerta a puerta (dropOff=0).
                continue
            parsed.append({
                "carrier": raw.get("carrier") or raw.get("carrier_name") or carrier,
                "service": str(raw.get("service") or raw.get("service_id") or raw.get("serviceId") or ""),
                "serviceDescription": raw.get("serviceDescription") or raw.get("service_description") or raw.get("service_name"),
                "deliveryEstimate": raw.get("deliveryEstimate") or raw.get("delivery_date") or raw.get("estimated_delivery"),
                "totalPrice": float(raw.get("totalPrice") or raw.get("total_price") or raw.get("total") or 0),
                "currency": raw.get("currency") or "MXN",
            })
        return parsed

    async with httpx.AsyncClient(timeout=SHIPPING_TIMEOUT) as client:
        results = await asyncio.gather(*(_quote_one(client, carrier) for carrier in carriers))
    for parsed in results:
        options.extend(parsed)

    logger.info(
        f"envia.com: cotizacion para la orden {order.uuid} - {len(carriers)} carriers "
        f"consultados, {len(options)} opciones obtenidas, {len(meta_error_messages)} "
        f"rechazados por carrier (meta:error), {'1 falla de transporte/auth' if last_error_response is not None else '0 fallas de transporte/auth'}"
    )

    if not options and last_error_response is not None:
        raise_upstream_error(
            last_error_response,
            f"envia.com rechazo la cotizacion de envio para la orden {order.uuid}",
            "No se pudo cotizar el envío con envia.com.",
        )

    if not options and len(meta_error_messages) >= 2 and len(set(meta_error_messages)) == 1:
        # Mismo mensaje en todos los carriers = problema estructural de la peticion, no falta de cobertura real.
        shared_message = meta_error_messages[0]
        logger.error(
            f"envia.com: TODOS los carriers ({len(meta_error_messages)}) rechazaron la "
            f"cotizacion de la orden {order.uuid} con el mismo mensaje - probable problema "
            f"estructural de la peticion, no de cobertura: {shared_message}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo cotizar el envío con envia.com (envia respondió: {shared_message[:300]}).",
        )

    options.sort(key=lambda opt: opt["totalPrice"])
    return options


async def generate_shipping_label(order: Order, weight: float, length: float, width: float, height: float, carrier: str, service: str, origin_overrides: dict | None = None) -> dict:
    """Genera una guia real (con costo) para `order` via envia.com; re-resuelve
    origin/destination igual que get_shipping_quote. El llamador (admin_service) persiste
    el resultado y avanza dispatch_status - esta funcion solo habla con envia.com.

    El payload de /ship/generate/ exige `carrier`/`service`/`type` anidados bajo
    `shipment`, mas un objeto `settings` (`printFormat: "PDF"`/`printSize: "STOCK_4X6"`,
    la combinacion default de esta cuenta) - sin ellos envia.com responde 200 con
    `meta:"error"` que aqui se detecta explicitamente y se levanta como 502 real (nunca se
    confunde con exito).

    Riesgo conocido, sin mitigar: esta llamada cobra una guia real del lado de envia.com -
    un timeout de este lado que compita con un exito del lado del servidor dejaria una
    guia generada (y cobrada) sin que este backend se entere; sin reintento automatico."""
    payload = {
        "origin": _origin_address(origin_overrides),
        "destination": _destination_address(order),
        "packages": _build_packages(weight, length, width, height),
        "shipment": {"carrier": carrier, "service": service, "type": 1},
        "settings": {"printFormat": "PDF", "printSize": "STOCK_4X6"},
    }

    async with httpx.AsyncClient(timeout=SHIPPING_TIMEOUT) as client:
        response = await client.post(GENERATE_URL, json=payload, headers=_envia_headers())

    if response.status_code not in (200, 201):
        raise_upstream_error(
            response,
            f"envia.com rechazo la generacion de guia para la orden {order.uuid}",
            "No se pudo generar la guía de envío con envia.com.",
        )

    body = response.json()
    if isinstance(body, dict) and body.get("meta") == "error":
        message = (body.get("error") or {}).get("message")
        logger.error(f"envia.com rechazo la generacion de guia para la orden {order.uuid}: {message}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo generar la guía de envío con envia.com (envia respondió: {(message or '')[:300]}).",
        )

    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}

    return {
        "carrier": carrier,
        "service": service,
        "shipmentId": data.get("shipmentId") or data.get("shipment_id"),
        "serviceDescription": data.get("serviceDescription") or data.get("service_description"),
        "trackingNumber": data.get("trackingNumber") or data.get("tracking_number"),
        "trackUrl": data.get("trackUrl") or data.get("tracking_url") or data.get("trackingUrl"),
        "labelUrl": data.get("label") or data.get("labelUrl") or data.get("label_url"),
        "totalPrice": float(data.get("totalPrice") or data.get("total_price") or data.get("total") or 0),
        "currency": data.get("currency") or "MXN",
        "weight": weight,
        "length": length,
        "width": width,
        "height": height,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


async def cancel_shipping_label(order: Order, carrier: str, tracking_number: str) -> dict:
    """Cancela ante envia.com una guia ya generada (`POST /ship/cancel/`), identificada solo
    por `carrier`/`trackingNumber` - los unicos campos que envia.com exige para esto, ambos ya
    persistidos en `Order.shipping_label` desde que se genero. El llamador (admin_service) es
    quien limpia `shipping_label`/`dispatch_status` en Postgres tras un exito aqui.

    Mismo tratamiento de `meta:"error"` que `generate_shipping_label` - envia.com no documenta
    un shape de error distinto para los casos de rechazo (guia ya recogida por el carrier,
    ventana de cancelacion vencida), asi que llegan como el mismo 200 con `meta:"error"` y se
    levantan igual como 502 real, nunca confundidos con exito."""
    payload = {"carrier": carrier, "trackingNumber": tracking_number}

    async with httpx.AsyncClient(timeout=SHIPPING_TIMEOUT) as client:
        response = await client.post(CANCEL_URL, json=payload, headers=_envia_headers())

    if response.status_code not in (200, 201):
        raise_upstream_error(
            response,
            f"envia.com rechazo la cancelacion de guia para la orden {order.uuid}",
            "No se pudo cancelar la guía de envío con envia.com.",
        )

    body = response.json()
    if isinstance(body, dict) and body.get("meta") == "error":
        message = (body.get("error") or {}).get("message")
        logger.error(f"envia.com rechazo la cancelacion de guia para la orden {order.uuid}: {message}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo cancelar la guía de envío con envia.com (envia respondió: {(message or '')[:300]}).",
        )

    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}

    return {
        "balanceReturned": bool(data.get("balanceReturned")),
        "balanceReturnDate": data.get("balanceReturnDate"),
    }
