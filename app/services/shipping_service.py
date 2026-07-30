import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.core.upstream_errors import raise_upstream_error
from app.models.order import Order

# API de envios de envia.com - distinta de su API de Geocodes (que el frontend llama
# directo, sin llave, para autocompletar direcciones; ver CLAUDE.md). Esta si necesita
# un token (ENVIA_API_TOKEN, ver app/core/config.py).
ENVIA_BASE_URL = "https://api.envia.com"
RATE_URL = f"{ENVIA_BASE_URL}/ship/rate/"
GENERATE_URL = f"{ENVIA_BASE_URL}/ship/generate/"
SHIPPING_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)


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
    return [{
        "content": "Ferreteria",
        "amount": 1,
        "type": "box",
        "weight": weight,
        "weightUnit": "KG",
        "length": length,
        "width": width,
        "height": height,
        "lengthUnit": "CM",
    }]


def missing_address_fields(snapshot: dict | None) -> list[str]:
    if not snapshot:
        return ["deliveryAddress"]
    return [field for field in ("street", "city", "state", "zipCode") if not snapshot.get(field)]


async def get_shipping_quote(order: Order, weight: float, length: float, width: float, height: float) -> list[dict]:
    """Cotiza opciones de envio con envia.com para `order` (DELIVERYMAN, dispatch_status
    COMPLETE - validado por el llamador, admin_service.quote_shipping). No persiste nada,
    se puede llamar repetidamente mientras el admin ajusta peso/medidas.

    NOTA: el shape exacto de la respuesta de POST /ship/rate/ de envia.com no esta
    verificado contra una llamada real (su documentacion publica actual no lo publica en
    detalle - empuja hacia su SDK oficial, que no se usa aqui por la misma razon que el
    resto de integraciones externas de este backend usan httpx crudo, ver CLAUDE.md).
    El parseo de abajo es best-effort/defensivo (multiples nombres de campo posibles) -
    verificar con el simulador/sandbox de envia.com antes de confiar en produccion, mismo
    espiritu que la advertencia ya existente sobre el formato del webhook de Mercado Pago."""
    payload = {
        "origin": _origin_address(),
        "destination": _destination_address(order),
        "packages": _build_packages(weight, length, width, height),
    }

    async with httpx.AsyncClient(timeout=SHIPPING_TIMEOUT) as client:
        response = await client.post(RATE_URL, json=payload, headers=_envia_headers())

    if response.status_code not in (200, 201):
        raise_upstream_error(
            response,
            f"envia.com rechazo la cotizacion de envio para la orden {order.uuid}",
            "No se pudo cotizar el envío con envia.com.",
        )

    body = response.json()
    raw_options = body.get("data") if isinstance(body, dict) else body
    if isinstance(raw_options, dict):
        raw_options = raw_options.get("rates") or raw_options.get("options") or []
    if not isinstance(raw_options, list):
        raw_options = []

    options = []
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        options.append({
            "carrier": raw.get("carrier") or raw.get("carrier_name"),
            "service": str(raw.get("service") or raw.get("service_id") or raw.get("serviceId") or ""),
            "serviceDescription": raw.get("serviceDescription") or raw.get("service_description") or raw.get("service_name"),
            "deliveryEstimate": raw.get("deliveryEstimate") or raw.get("delivery_date") or raw.get("estimated_delivery"),
            "totalPrice": float(raw.get("totalPrice") or raw.get("total_price") or raw.get("total") or 0),
            "currency": raw.get("currency") or "MXN",
        })
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
