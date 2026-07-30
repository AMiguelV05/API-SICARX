import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.core.upstream_errors import raise_upstream_error
from app.models.order import Order

# API de envios de envia.com - distinta de su API de Geocodes (que el frontend llama
# directo, sin llave, para autocompletar direcciones; ver CLAUDE.md). Esta si necesita
# un token (ENVIA_API_TOKEN, ver app/core/config.py).
#
# INCIDENTE (2026-07-30): esto estaba hardcodeado a api.envia.com (produccion) y
# ENVIA_API_TOKEN es en realidad un token de sandbox - envia.com da de alta cuentas
# nuevas en sandbox por defecto, produccion es un alta aparte con su propio token. Un
# token de sandbox contra el dominio de produccion siempre responde 401 "Authentication
# error" (sin mencionar el ambiente en el mensaje, lo que lo hacia parecer un token
# invalido/revocado en vez de un simple mismatch de dominio) - confirmado en vivo
# probando el mismo token contra ambos dominios. Ver settings.ENVIA_ENVIRONMENT.
ENVIA_BASE_URL = "https://api-test.envia.com" if settings.ENVIA_ENVIRONMENT == "sandbox" else "https://api.envia.com"
RATE_URL = f"{ENVIA_BASE_URL}/ship/rate/"
GENERATE_URL = f"{ENVIA_BASE_URL}/ship/generate/"
# Queries API - servicio de referencia/catalogo aparte del de envios (distinto dominio),
# tambien separado por ambiente sandbox/produccion. Ver _fetch_available_carriers.
QUERIES_BASE_URL = "https://queries-test.envia.com" if settings.ENVIA_ENVIRONMENT == "sandbox" else "https://queries.envia.com"
SHIPPING_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
# Cuantas cotizaciones a envia.com se disparan en paralelo por llamada a get_shipping_quote -
# con ~24 carriers reales en el catalogo de Mexico, hacerlo secuencial seria demasiado lento
# para una llamada sincrona desde el dashboard admin.
MAX_CONCURRENT_RATE_REQUESTS = 8
# TTL del cache en memoria de _fetch_available_carriers - mismo espiritu que el refresco
# perezoso de 24h ya usado en otros catalogos de este backend (ver taxonomy_service.py,
# GET /products/{uuid}): el catalogo de carriers de envia.com no cambia con frecuencia,
# no vale la pena una llamada de red extra en cada cotizacion.
CARRIER_CATALOG_TTL = timedelta(hours=24)

logger = logging.getLogger(__name__)

_carrier_catalog_cache: dict[str, tuple[datetime, list[str]]] = {}
_carrier_catalog_lock = asyncio.Lock()


def _envia_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.ENVIA_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _origin_address() -> dict:
    """Direccion de la tienda/almacen - constante propia de este servicio (variables
    ENVIA_ORIGIN_*, ver app/core/config.py), nunca enviada por el frontend.

    ADVERTENCIA conocida: envia.com exige `state` como un codigo corto (2-3 caracteres,
    p. ej. "NL" para Nuevo Leon), no el nombre completo del estado - ENVIA_ORIGIN_STATE
    debe cargarse con ese codigo, no con el nombre. Este backend no tiene (ni tenia antes
    de esto, ver CLAUDE.md - "no hay catalogo de estados/municipios") una tabla de
    conversion nombre->codigo, asi que no se normaliza aqui; si se carga mal, envia.com
    respondera 502 via raise_upstream_error, no un error silencioso."""
    return {
        "name": settings.ENVIA_ORIGIN_NAME,
        "company": settings.ENVIA_ORIGIN_COMPANY,
        "email": settings.ENVIA_ORIGIN_EMAIL,
        "phone_code": settings.ENVIA_PHONE_CODE,
        "phone": settings.ENVIA_ORIGIN_PHONE,
        "street": settings.ENVIA_ORIGIN_STREET,
        "number": settings.ENVIA_ORIGIN_NUMBER,
        "district": settings.ENVIA_ORIGIN_DISTRICT,
        "city": settings.ENVIA_ORIGIN_CITY,
        "state": settings.ENVIA_ORIGIN_STATE,
        "country": settings.ENVIA_COUNTRY,
        "postalCode": settings.ENVIA_ORIGIN_ZIP_CODE,
        "reference": settings.ENVIA_ORIGIN_REFERENCE,
    }


def _destination_address(order: Order) -> dict:
    """`order.delivery_address_snapshot` es la foto fija ClientAddressPublic (camelCase)
    tomada al crear la orden (ver routes/orders.py, ADMIN_INTEGRATION.md) - de ahi salen
    los campos de direccion. `name`/`phone`/`email` en cambio vienen de
    `order.delivery_info["contactInfo"]` (los datos de contacto reales capturados en el
    checkout), no del address book - mismo campo que ya usa
    cancel_service.advance_dispatch_status para lo mismo. `company` va siempre None: es un
    envio a un cliente final (ClientAddressPublic no tiene razon social), a diferencia de
    _origin_address, donde si aplica (la tienda). Misma advertencia sobre `state` que
    _origin_address arriba - aqui aplica a ClientAddress.state, que tampoco se guarda como
    codigo hoy."""
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
        "state": snapshot.get("state"),
        "country": settings.ENVIA_COUNTRY,
        "postalCode": snapshot.get("zipCode"),
        "reference": snapshot.get("references"),
    }


def _build_packages(weight: float, length: float, width: float, height: float) -> list[dict]:
    """INCIDENTE (2026-07-30): `length`/`width`/`height`/`lengthUnit` planos en el objeto
    del paquete (como estaba antes) son rechazados por el sandbox real de envia.com con
    "Required property missing: dimensions" - las medidas van anidadas bajo un objeto
    `dimensions` propio, confirmado en vivo contra POST /ship/rate/."""
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
    """Catalogo real de envia.com (Queries API, dominio aparte de Ship) para "que carriers
    existen para este pais/tipo de envio" - GET /available-carrier/{country}/{international}/
    {shipment_type_id}. `international=0` (envios nacionales - ver ENVIA_COUNTRY/CLAUDE.md,
    este negocio solo envia dentro de Mexico) y `shipment_type_id=1` ("Package", confirmado
    en vivo: devuelve exactamente los carriers que luego aceptan una cotizacion real via
    POST /ship/rate/, y correctamente NO incluye "estafeta" - que en la practica responde
    "Service provided not available" para esta cuenta/tipo de envio).

    Reemplaza la lista fija ENVIA_CARRIERS que existia antes: en vez de mantener a mano
    cuales carriers probar (y quedar desactualizada si envia.com agrega/quita alguno), se
    consulta su propio catalogo - ningun carrier queda fuera. Cacheado en memoria por
    CARRIER_CATALOG_TTL: es catalogo de referencia, no cambia por pedido."""
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


async def get_shipping_quote(order: Order, weight: float, length: float, width: float, height: float) -> list[dict]:
    """Cotiza opciones de envio con envia.com para `order` (DELIVERYMAN, dispatch_status
    COMPLETE - validado por el llamador, admin_service.quote_shipping). No persiste nada,
    se puede llamar repetidamente mientras el admin ajusta peso/medidas.

    INCIDENTE (2026-07-30): confirmado en vivo contra el sandbox real que POST /ship/rate/
    exige un `carrier` especifico en el body - no existe un valor comodin ("all",
    "multi_carrier", "*" fueron probados y todos responden "Carrier provided is not
    supported or incorrect"). Por eso esta funcion llama una vez por cada carrier de
    _fetch_available_carriers() y agrega los resultados, en vez de una sola llamada "multi
    carrier" como asumia el codigo anterior (que ademas nunca mandaba `carrier`, lo que de
    hecho fallaba mas temprano con un 500 "Undefined property: stdClass::$carrier" del lado
    de envia.com). Un carrier no habilitado para esta cuenta/ruta responde HTTP 200 con un
    sobre `{"meta": "error", ...}` (distinto de una falla real de transporte/autenticacion,
    que llega como status HTTP != 200/201) - se omite silenciosamente y se sigue con el
    resto; solo se levanta 502 si NINGUN carrier produjo una opcion valida. Las llamadas se
    disparan en paralelo (tope MAX_CONCURRENT_RATE_REQUESTS) porque el catalogo real tiene
    ~24 carriers - secuencial habria sido demasiado lento para una llamada sincrona desde
    el dashboard admin."""
    origin = _origin_address()
    destination = _destination_address(order)
    packages = _build_packages(weight, length, width, height)
    carriers = await _fetch_available_carriers()

    options: list[dict] = []
    last_error_response = None
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RATE_REQUESTS)

    async def _quote_one(client: httpx.AsyncClient, carrier: str):
        nonlocal last_error_response
        payload = {"origin": origin, "destination": destination, "packages": packages, "carrier": carrier}
        async with semaphore:
            response = await client.post(RATE_URL, json=payload, headers=_envia_headers())

        if response.status_code not in (200, 201):
            # Falla real (auth, red, etc.) - afecta a todos los carriers por igual, no tiene
            # caso tratarla distinto por carrier; se recuerda para el 502 de abajo.
            last_error_response = response
            return []

        body = response.json()
        if isinstance(body, dict) and body.get("meta") == "error":
            logger.info(
                f"envia.com: carrier '{carrier}' no disponible para la orden {order.uuid}: "
                f"{(body.get('error') or {}).get('message')}"
            )
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

    if not options and last_error_response is not None:
        raise_upstream_error(
            last_error_response,
            f"envia.com rechazo la cotizacion de envio para la orden {order.uuid}",
            "No se pudo cotizar el envío con envia.com.",
        )
    return options


async def generate_shipping_label(order: Order, weight: float, length: float, width: float, height: float, carrier: str, service: str) -> dict:
    """Genera una guia real (con costo) para `order` via envia.com. Re-resuelve
    origin/destination igual que get_shipping_quote. El llamador (admin_service) es
    responsable de persistir el resultado y avanzar dispatch_status - esta funcion solo
    habla con envia.com.

    Misma advertencia de shape-no-verificado que get_shipping_quote arriba, mas una
    especifica de confiabilidad: esta llamada tiene un efecto real con costo (genera y
    cobra una guia del lado de envia.com) - un timeout de este lado que compita con un
    exito del lado del servidor de envia.com dejaria una guia generada (y cobrada) sin
    que este backend se entere. Documentado como riesgo conocido en ADMIN_INTEGRATION.md,
    deliberadamente sin reintento automático sobre timeout."""
    payload = {
        "origin": _origin_address(),
        "destination": _destination_address(order),
        "packages": _build_packages(weight, length, width, height),
        "carrier": carrier,
        "service": service,
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
    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}

    return {
        "carrier": carrier,
        "service": service,
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
