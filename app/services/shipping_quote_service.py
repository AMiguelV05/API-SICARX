"""Cotizacion y cobro de envio en el checkout.

Flujo: POST /shipping/quotes (create_quote) empaca el carrito con las medidas de cada producto,
consulta envia.com y guarda la cotizacion en shipping_quotes. POST /orders (resolve_for_order)
vuelve a validar esa cotizacion contra el carrito y la direccion reales y suma el precio de la
opcion elegida al total. El precio nunca viene del cliente.

Si algun producto no tiene peso/medidas, o envia.com no da tarifas, el envio no se cobra en
linea (como antes de esta funcionalidad): el pedido queda con shipping_quote.status = MANUAL y
el admin lo cotiza a mano. Nunca se bloquea una venta por envia.com.
"""
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.product import Product
from app.models.shipping_quote import ShippingQuote
from app.schemas.shipping import OrderShippingSelection
from app.services import shipping_service
from app.services.package_dimensions_service import PackageDimensions
from app.services.shipping_packing import build_packages

logger = logging.getLogger(__name__)

QUOTE_TTL = timedelta(minutes=30)
# Una cotizacion QUOTED mas nueva que esto, mismo carrito/destino/cliente, se reusa sin volver
# a llamar a envia.com (cada cotizacion son ~24 llamadas). Siempre le quedan >= 20 min de vida.
QUOTE_REUSE_WINDOW = timedelta(minutes=10)
QUOTE_RETENTION = timedelta(days=30)
MAX_OPTIONS = 5

QUOTED = "QUOTED"
NOT_QUOTABLE = "NOT_QUOTABLE"
MISSING_DIMENSIONS = "MISSING_DIMENSIONS"
NO_RATES = "NO_RATES"
UPSTREAM_ERROR = "UPSTREAM_ERROR"

_REQUOTE = "vuelve a cotizar el envío"


@dataclass(frozen=True)
class OrderShipping:
    """Resultado para POST /orders: `amount` se suma al total (None = sin costo); `snapshot`
    va a Order.shipping_quote (None = no aplica, p. ej. cobro de envio apagado)."""
    amount: Decimal | None
    snapshot: dict | None


def _quantity_key(quantity) -> str:
    return format(Decimal(str(quantity)).normalize(), "f")


def items_fingerprint(quantities: dict[str, float]) -> str:
    payload = "|".join(f"{uuid}:{_quantity_key(q)}" for uuid, q in sorted(quantities.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _norm(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def destination_key(snapshot: dict) -> str:
    return f"{_norm(snapshot.get('zipCode'))}|{_norm(snapshot.get('state'))}|{_norm(snapshot.get('city'))}"


def _quotable_dims(product: Product, quantity) -> PackageDimensions | None:
    """Medidas de una pieza si la linea se puede empacar: producto con las cuatro medidas,
    no a granel y cantidad entera (las medidas son de una pieza)."""
    if product.package_weight_kg is None or product.is_bulk:
        return None
    q = Decimal(str(quantity))
    if q != q.to_integral_value():
        return None
    return PackageDimensions(product.package_weight_kg, product.package_length_cm, product.package_width_cm, product.package_height_cm)


def missing_dimensions(products: dict[str, Product], quantities: dict[str, float]) -> list[str]:
    return sorted(uuid for uuid, q in quantities.items() if _quotable_dims(products[uuid], q) is None)


def apply_margin(carrier_price: Decimal) -> Decimal:
    price = carrier_price * (1 + settings.SHIPPING_MARGIN_PERCENT / 100) + settings.SHIPPING_MARGIN_FIXED_MXN
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _shortlist(rates: list[dict]) -> list[dict]:
    """La opcion mas barata de cada paqueteria, las MAX_OPTIONS mas baratas, con margen."""
    cheapest: dict[str, tuple[Decimal, dict]] = {}
    for rate in rates:
        if (rate.get("currency") or "MXN").upper() != "MXN":
            continue
        carrier_price = Decimal(str(rate.get("totalPrice") or 0))
        if carrier_price <= 0:
            continue
        carrier = (rate.get("carrier") or "").lower()
        if carrier not in cheapest or carrier_price < cheapest[carrier][0]:
            cheapest[carrier] = (carrier_price, rate)
    ranked = sorted(cheapest.values(), key=lambda pair: pair[0])[:MAX_OPTIONS]
    return [
        {
            "optionId": str(i),
            "carrier": rate.get("carrier"),
            "service": rate.get("service"),
            "serviceDescription": rate.get("serviceDescription"),
            "deliveryEstimate": rate.get("deliveryEstimate"),
            "carrierPrice": str(carrier_price),
            "price": str(apply_margin(carrier_price)),
            "currency": "MXN",
        }
        for i, (carrier_price, rate) in enumerate(ranked, start=1)
    ]


async def _load_products(db: AsyncSession, uuids: list[str]) -> dict[str, Product]:
    """Lectura SIN lock (a diferencia de order_service.validate_cart_items): la cotizacion
    llama a envia.com y nunca debe sostener locks de filas durante una llamada externa. El
    stock lo revalida POST /orders."""
    result = await db.execute(
        select(Product).where(Product.sicar_uuid.in_(uuids), Product.is_deleted == False, Product.is_active == True)
    )
    products = {p.sicar_uuid: p for p in result.scalars().all()}
    unavailable = [u for u in uuids if u not in products]
    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Algunos productos ya no están disponibles: {', '.join(unavailable)}.",
        )
    return products


async def create_quote(db: AsyncSession, client_account_id: int | None, quantities: dict[str, float], snapshot: dict) -> ShippingQuote:
    """Cotiza el envio de `quantities` a `snapshot` (ClientAddressPublic camelCase) y guarda la
    cotizacion. Hace commit."""
    products = await _load_products(db, sorted(quantities))
    fingerprint = items_fingerprint(quantities)
    dest_key = destination_key(snapshot)
    now = datetime.now(timezone.utc)

    def new_quote(status_: str, reason: str | None, **fields) -> ShippingQuote:
        return ShippingQuote(
            client_account_id=client_account_id, items_fingerprint=fingerprint, destination_key=dest_key,
            status=status_, reason=reason, created_at=now, expires_at=now + QUOTE_TTL,
            options=fields.pop("options", []), **fields,
        )

    missing = missing_dimensions(products, quantities)
    if missing:
        quote = new_quote(NOT_QUOTABLE, MISSING_DIMENSIONS, missing_dimensions=missing)
        return await _save(db, quote)

    cached = await db.scalar(
        select(ShippingQuote)
        .where(
            ShippingQuote.items_fingerprint == fingerprint,
            ShippingQuote.destination_key == dest_key,
            ShippingQuote.client_account_id.is_not_distinct_from(client_account_id),
            ShippingQuote.status == QUOTED,
            ShippingQuote.created_at > now - QUOTE_REUSE_WINDOW,
        )
        .order_by(ShippingQuote.created_at.desc())
        .limit(1)
    )
    if cached is not None:
        await db.commit()  # cierra la transaccion de lectura
        return cached

    boxes = build_packages([(_quotable_dims(products[u], q), int(q)) for u, q in quantities.items()])
    packages = [box.as_dict() for box in boxes]
    # Cierra la transaccion de lectura antes de la llamada externa (hasta ~20 s): no se
    # sostiene una conexion "idle in transaction" mientras se espera a envia.com.
    await db.commit()

    destination = shipping_service.destination_from(snapshot, None)
    payload = shipping_service.build_packages_payload(
        [{"weight": b.weight_kg, "length": b.length_cm, "width": b.width_cm, "height": b.height_cm} for b in boxes]
    )
    carriers = [c for c in settings.SHIPPING_QUOTE_CARRIERS.split(",") if c.strip()] or None
    try:
        rates = await shipping_service.fetch_rates(
            shipping_service._origin_address(), destination, payload,
            context=f"cotizacion de checkout (CP {snapshot.get('zipCode')})", carriers=carriers,
        )
    except (HTTPException, httpx.HTTPError) as exc:
        logger.warning(f"Cotizacion de checkout sin tarifas por error de envia.com (CP {snapshot.get('zipCode')}): {exc!r}")
        return await _save(db, new_quote(NOT_QUOTABLE, UPSTREAM_ERROR, packages=packages))

    options = _shortlist(rates)
    if not options:
        logger.info(f"Cotizacion de checkout sin tarifas (CP {snapshot.get('zipCode')}, {len(boxes)} cajas): envio sin costo, cotizacion manual.")
        return await _save(db, new_quote(NOT_QUOTABLE, NO_RATES, packages=packages))
    return await _save(db, new_quote(QUOTED, None, packages=packages, options=options))


async def _save(db: AsyncSession, quote: ShippingQuote) -> ShippingQuote:
    db.add(quote)
    await db.commit()
    await db.refresh(quote)
    return quote


def _manual(reason: str | None, quote_uuid: str | None) -> OrderShipping:
    return OrderShipping(amount=None, snapshot={"quoteUuid": quote_uuid, "status": "MANUAL", "reason": reason})


async def resolve_for_order(
    db: AsyncSession, *, client_account_id: int | None, products: dict[str, Product], quantities: dict[str, float],
    snapshot: dict, selection: OrderShippingSelection | None,
) -> OrderShipping:
    """Decide el costo de envio de un pedido DELIVERYMAN. Solo lee la BD (corre dentro de la
    transaccion de POST /orders, con los productos bloqueados). 409 si la cotizacion no
    corresponde a este pedido: el frontend debe volver a cotizar."""
    if not settings.SHIPPING_CHECKOUT_ENABLED:
        return OrderShipping(amount=None, snapshot=None)

    if missing_dimensions(products, quantities):
        return _manual(MISSING_DIMENSIONS, selection.quote_uuid if selection else None)

    if selection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Falta elegir el envío: cotízalo con POST /v1/shipping/quotes y manda shipping.quoteUuid/optionId.",
        )

    quote = await db.scalar(select(ShippingQuote).where(ShippingQuote.uuid == selection.quote_uuid))
    if quote is None or quote.client_account_id != client_account_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"La cotización de envío no es válida; {_REQUOTE}.")
    if quote.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"La cotización de envío expiró; {_REQUOTE}.")
    if quote.items_fingerprint != items_fingerprint(quantities):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"El carrito cambió desde la cotización; {_REQUOTE}.")
    if quote.destination_key != destination_key(snapshot):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"La dirección de entrega cambió desde la cotización; {_REQUOTE}.")

    if quote.status != QUOTED:
        return _manual(quote.reason, quote.uuid)

    option = next((o for o in quote.options or [] if o.get("optionId") == selection.option_id), None)
    if option is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"La opción de envío elegida no existe en la cotización; {_REQUOTE}.")

    amount = Decimal(option["price"])
    return OrderShipping(amount=amount, snapshot={
        "quoteUuid": quote.uuid,
        "status": "QUOTED",
        "reason": None,
        "optionId": option["optionId"],
        "carrier": option.get("carrier"),
        "service": option.get("service"),
        "serviceDescription": option.get("serviceDescription"),
        "deliveryEstimate": option.get("deliveryEstimate"),
        "carrierPrice": option.get("carrierPrice"),
        "price": option["price"],
        "currency": option.get("currency") or "MXN",
        "packages": quote.packages,
    })


async def prune_old_quotes(db: AsyncSession) -> int:
    """Borra cotizaciones con mas de QUOTE_RETENTION (los pedidos guardan su propia copia en
    Order.shipping_quote). Lo corre el worker una vez al dia."""
    result = await db.execute(delete(ShippingQuote).where(ShippingQuote.created_at < datetime.now(timezone.utc) - QUOTE_RETENTION))
    await db.commit()
    return result.rowcount or 0
