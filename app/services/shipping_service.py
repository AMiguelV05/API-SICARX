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

# INCIDENTE (2026-07-30): confirmado en vivo contra el sandbox real que el campo `state`
# del objeto address (origin/destination) de envia.com tiene `minLength: 2, maxLength: 3`
# (confirmado tanto por la doc oficial - docs.envia.com/reference/shipping-rates - como en
# vivo: "Ciudad de México"/"CDMX" (4 chars) responden 200 con
# meta:"error"/"String is too long ...properties:state", mientras que "CMX"/"DF"/"NL" (2-3
# chars) responden 200 meta:"rate" normalmente). Antes de esto, `_destination_address`
# mandaba `ClientAddress.state` tal cual estaba guardado (potencialmente el nombre
# completo, ya que este backend no tiene ni tenia un catalogo de conversion nombre->codigo -
# ver CLAUDE.md "Address book"), lo que producia el MISMO error de validacion para TODOS
# los carriers por igual (el request nunca llega a evaluarse por carrier, envia lo rechaza
# antes) - y `get_shipping_quote` lo trataba como "cada carrier individualmente no
# disponible", produciendo un `200 {"options": []}` indistinguible de una falta de
# cobertura real. Esta era la causa real detras del reporte "los carriers dicen que no hay
# disponibilidad aunque si la hay". `_normalize_mx_state` convierte los nombres completos
# (y variantes comunes, con o sin acentos/puntuacion) de los 32 estados a su codigo
# ISO 3166-2:MX de 3 letras; un valor que ya mide <=3 caracteres se deja intacto (ya
# demostrado en vivo que envia no valida que sea un codigo "real", solo la longitud), y un
# valor >3 caracteres no reconocido se trunca a los primeros 3 con un WARNING en vez de
# dejar que la peticion falle por completo otra vez.
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
    """Direccion de la tienda/almacen - por defecto las constantes propias de este servicio
    (variables ENVIA_ORIGIN_*, ver app/core/config.py). `overrides` (opcional, ver
    ShippingOriginOverride en app/schemas/admin.py) permite que el admin reemplace
    cualquier subconjunto de campos por pedido via /shipping/quote|generate - cualquier
    campo ausente o vacio en `overrides` cae de vuelta al valor de `.env`, campo por campo
    (nunca "todo o nada"). `country`/`phone_code` deliberadamente no son parte de
    `overrides` - se quedan fijos.

    `state` (sea de `overrides` o de `.env`) pasa por `_normalize_mx_state` - ver el
    comentario junto a esa funcion mas arriba (INCIDENTE 2026-07-30) para el porque: un
    admin que escriba el nombre completo del estado en el override necesita la misma
    normalizacion que ya se le aplica al valor de `.env`, o vuelve a producir el mismo
    "String is too long" que afecto a todos los carriers por igual antes de ese fix."""
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
    """`order.delivery_address_snapshot` es la foto fija ClientAddressPublic (camelCase)
    tomada al crear la orden (ver routes/orders.py, ADMIN_INTEGRATION.md) - de ahi salen
    los campos de direccion. `name`/`phone`/`email` en cambio vienen de
    `order.delivery_info["contactInfo"]` (los datos de contacto reales capturados en el
    checkout), no del address book. `company` va siempre None: es un
    envio a un cliente final (ClientAddressPublic no tiene razon social), a diferencia de
    _origin_address, donde si aplica (la tienda). `state` viene de `ClientAddress.state`,
    que se guarda tal cual lo resuelve el frontend (no necesariamente un codigo corto) -
    pasa por `_normalize_mx_state` antes de mandarse a envia.com. Ver el comentario junto a
    esa funcion mas arriba (INCIDENTE 2026-07-30) - un `state` sin normalizar (nombre
    completo, o cualquier valor de mas de 3 caracteres) hacia que envia.com rechazara la
    peticion con el MISMO error de validacion para todos los carriers por igual, lo que
    `get_shipping_quote` interpretaba (antes de este fix) como "ningun carrier disponible"."""
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


async def get_shipping_quote(order: Order, weight: float, length: float, width: float, height: float, origin_overrides: dict | None = None) -> list[dict]:
    """Cotiza opciones de envio con envia.com para `order` (DELIVERYMAN, dispatch_status
    COMPLETE - validado por el llamador, admin_service.quote_shipping). No persiste nada,
    se puede llamar repetidamente mientras el admin ajusta peso/medidas. `origin_overrides`
    (opcional) se pasa tal cual a `_origin_address` - ver esa funcion para el merge
    campo-por-campo contra `.env`. Solo devuelve opciones puerta a puerta (`dropOff: 0`) -
    ver el comentario junto al filtro en el loop de `_quote_one` mas abajo (INCIDENTE
    2026-07-30) para el porque de las opciones basadas en sucursal.

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
    resto. Las llamadas se disparan en paralelo (tope MAX_CONCURRENT_RATE_REQUESTS) porque
    el catalogo real tiene ~24 carriers - secuencial habria sido demasiado lento para una
    llamada sincrona desde el dashboard admin.

    INCIDENTE (2026-07-30): un `meta:"error"` por carrier puede significar dos cosas muy
    distintas - "este carrier en particular no cubre esta ruta/paquete" (disponibilidad
    real, variable carrier a carrier) o "la peticion esta mal formada de una forma que
    afecta a TODOS los carriers por igual" (p. ej. el bug de `state` resuelto en
    _normalize_mx_state arriba - un `state` sin normalizar hacia que los ~15-20 carriers
    reales de la cuenta respondieran, todos, el mismo error de validacion literal, y esta
    funcion lo devolvia como `options: []` - indistinguible de "no hay cobertura real" para
    quien llama). Para no repetir esa investigacion la proxima vez que algo similar pase:
    si ningun carrier produjo una opcion Y los `meta:error` recolectados comparten
    exactamente el mismo mensaje, se asume que es un problema estructural de la peticion (no
    de cobertura) y se levanta un 502 con ese mensaje real de envia.com en vez de un
    `200 {"options": []}` silencioso - ver el bloque despues del `gather` mas abajo."""
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
            # Falla real (auth, red, etc.) - afecta a todos los carriers por igual, no tiene
            # caso tratarla distinto por carrier; se recuerda para el 502 de abajo.
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
                # INCIDENTE (2026-07-30): `dropOff != 0` significa que el origen o el
                # destino (segun el valor - "Puerta a sucursal"/"Sucursal a puerta") deben
                # ser una sucursal fisica del carrier, no la puerta - envia.com exige un
                # `branch_code` especifico en /ship/generate/ para estas ("Origin/
                # Destination branch code is required..."), que este backend no recolecta
                # ni manda hoy - confirmado en vivo que generate falla siempre para estas
                # opciones. Se descartan aqui (en vez de ofrecerlas y fallar despues en
                # /shipping/generate) hasta que exista soporte real de seleccion de
                # sucursal (deliberadamente no implementado - requeriria una nueva UI en el
                # dashboard admin para elegir la sucursal). Solo se ofrecen opciones
                # `dropOff: 0` ("Puerta a puerta"), que ya generan guias correctamente.
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
        # Todos los carriers consultados fallaron con el MISMO mensaje - casi seguro un
        # problema estructural de la peticion (direccion/paquete mal formado), no una
        # falta de cobertura real. Ver el INCIDENTE (2026-07-30) en el docstring de esta
        # funcion.
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
    """Genera una guia real (con costo) para `order` via envia.com. Re-resuelve
    origin/destination igual que get_shipping_quote (incluyendo `origin_overrides` - ver
    esa funcion y `_origin_address`). El llamador (admin_service) es responsable de
    persistir el resultado y avanzar dispatch_status - esta funcion solo habla con
    envia.com.

    INCIDENTE (2026-07-30): confirmado en vivo que esta funcion NUNCA genero una guia real
    - envia.com rechazaba la peticion (HTTP 200, `meta:"error"`, "Required property
    missing: settings") y esta funcion, al no revisar `meta`, lo trataba como exito: devolvia
    un `dict` con `trackingNumber`/`labelUrl` en `None` y `totalPrice: 0`, que admin_service
    igual persistia como `Order.shipping_label`, avanzaba `dispatch_status` a `DISPATCHED` y
    disparaba la notificacion "pedido enviado" al cliente - sin que ningun envio existiera
    realmente en envia.com (de ahi que no apareciera nada en su dashboard de pruebas). Dos
    problemas, arreglados juntos:
    1. El payload de `/ship/generate/` (a diferencia de `/ship/rate/`) exige `carrier`/
       `service`/`type` anidados bajo un objeto `shipment`, y un objeto `settings`
       (`printFormat`/`printSize`) - ninguno de los dos se mandaba. Confirmado contra
       docs.envia.com/reference/create-shipping-label y en vivo: con el payload corregido
       envia.com devuelve `meta:"generate"` y un `shipmentId` real (visible en su
       dashboard). `printFormat: "PDF"`/`printSize: "STOCK_4X6"` son la combinacion
       "default" documentada para el catalogo de carriers de esta cuenta (ver
       docs.envia.com/reference/carrier-print-options) - no expuesto como configuracion,
       no hay necesidad de variarlo hoy.
    2. Un `meta:"error"` en HTTP 200 (mismo patron "falla suave" que ya maneja
       `get_shipping_quote`/`_quote_one` mas arriba) ahora se detecta explicitamente y se
       levanta como un 502 real - nunca mas se puede confundir con un exito.

    Misma advertencia de shape-no-verificado que get_shipping_quote arriba, mas una
    especifica de confiabilidad: esta llamada tiene un efecto real con costo (genera y
    cobra una guia del lado de envia.com) - un timeout de este lado que compita con un
    exito del lado del servidor de envia.com dejaria una guia generada (y cobrada) sin
    que este backend se entere. Documentado como riesgo conocido en ADMIN_INTEGRATION.md,
    deliberadamente sin reintento automático sobre timeout."""
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
