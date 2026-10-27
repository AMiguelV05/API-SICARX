"""Integracion con Mercado Pago, solo por la Orders API (`/v1/orders`, ids `ORD...`).

Las credenciales de las apps creadas en el panel nuevo de MP no aceptan la Payments API
(`/v1/payments` responde 401 "Unauthorized use of live credentials", confirmado 2026-10-25),
asi que todo cobro, consulta, reembolso y cancelacion va por `/v1/orders`. Los pedidos de
prueba cobrados con la app anterior (ids numericos) ya no se pueden gestionar: las funciones
de abajo responden 409 para ellos (`_require_mp_order_id`).

Para no duplicar la logica de estados, cada order se traduce con `_order_as_payment` al
formato de pago que entienden `finalize_order_payment`, las rutas de cancelacion y el admin
(`status` en el vocabulario de la Payments API: approved/pending/rejected/...).
"""
import asyncio
import hashlib
import json
import logging
import time
import uuid
from decimal import Decimal

import httpx
from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.retry import request_with_backoff
from app.models.order import Order
from app.core.upstream_errors import raise_upstream_error
from app.core.webhook_signing import verify_hmac_sha256

ORDERS_URL = "https://api.mercadopago.com/v1/orders"
PAYMENT_METHODS_URL = "https://api.mercadopago.com/v1/payment_methods"
CHARGEBACKS_URL = "https://api.mercadopago.com/v1/chargebacks"
MP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

# Estado de la Orders API -> estado de la Payments API (guia oficial "Como migrar de la
# Payments API a la Orders API", paso 5). Ojo: Orders escribe `canceled` con una sola L.
_ORDER_STATUS_AS_PAYMENT = {
    "created": "pending",
    "action_required": "pending",
    "processing": "in_process",
    "processed": "approved",
    "failed": "rejected",
    "canceled": "cancelled",
    "expired": "cancelled",
    "refunded": "refunded",
}

# Tipos que la Orders API acepta en payment_method.type para el checkout.
_PAYMENT_TYPES = {"credit_card", "debit_card", "prepaid_card", "ticket", "atm", "bank_transfer"}
_PAYMENT_METHODS_TTL = 3600
_payment_methods_cache: dict = {"fetched_at": 0.0, "types": {}}


def is_mp_order_id(mp_id: str | None) -> bool:
    """True si el id es de la Orders API (`ORD...`, `ORDTST...` en pruebas)."""
    return bool(mp_id) and str(mp_id).upper().startswith("ORD")


def _require_mp_order_id(mp_id: str | None) -> None:
    if not is_mp_order_id(mp_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este pedido se cobró con la integración anterior de Mercado Pago y ya no se puede gestionar desde aquí.",
        )


def _order_as_payment(mp_order: dict) -> dict:
    """Traduce una order de la Orders API al formato de pago de la Payments API.

    `status` sale del nivel de la order (vision consolidada); `status_detail`, de la
    transaccion cuando existe, porque ahi viene el motivo concreto de un rechazo
    (`rejected_by_issuer`) mientras la order solo dice `failed`. Un contracargo en curso
    (`charged_back` + `in_process`) equivale al `in_mediation` de la Payments API; resuelto
    (`settled`/`reimbursed`), a `charged_back`. `chargeback_case_ids` lista los casos de
    `transactions.chargebacks[]`, para consultar su detalle en `/v1/chargebacks/{case_id}`."""
    transactions = mp_order.get("transactions") or {}
    payment = (transactions.get("payments") or [{}])[0]
    method = payment.get("payment_method") or {}
    status_ = mp_order.get("status")
    order_detail = mp_order.get("status_detail")

    if status_ == "charged_back":
        mapped = "in_mediation" if order_detail == "in_process" else "charged_back"
    elif status_ == "action_required" and order_detail == "waiting_capture":
        mapped = "authorized"
    else:
        mapped = _ORDER_STATUS_AS_PAYMENT.get(status_, status_)

    # `refunds[]` tambien guarda los intentos fallidos (`status: failed`), asi que no se suma
    # tal cual: se usa `refunded_amount` de la transaccion y, si falta, solo los procesados.
    if payment.get("refunded_amount") is not None:
        refunded = Decimal(str(payment["refunded_amount"]))
    else:
        refunds = [r for r in transactions.get("refunds") or [] if r.get("status") == "processed"]
        refunded = sum((Decimal(str(r.get("amount") or 0)) for r in refunds), Decimal("0"))
    if not refunded and status_ == "refunded":
        refunded = Decimal(str(mp_order.get("total_paid_amount") or mp_order.get("total_amount") or 0))

    return {
        "id": mp_order.get("id"),
        "status": mapped,
        "status_detail": payment.get("status_detail") or order_detail,
        "payment_method_id": method.get("id"),
        "external_reference": mp_order.get("external_reference"),
        "transaction_details": {"external_resource_url": method.get("ticket_url")},
        "transaction_amount_refunded": str(refunded),
        "chargeback_case_ids": [str(c["case_id"]) for c in transactions.get("chargebacks") or [] if c.get("case_id")],
    }


async def _payment_type_for(payment_method_id: str, has_token: bool) -> str:
    """`payment_method.type` que exige la Orders API, a partir del catalogo de MP
    (`/v1/payment_methods`, en cache una hora). `visa`/`master` aparecen como credito y como
    prepago: con token se prefiere debito si el id es de debito y si no credito, que es lo que
    el Payment Brick ofrece como "Tarjeta de credito". Si el catalogo no responde, se deduce
    del prefijo del id."""
    now = time.monotonic()
    if now - _payment_methods_cache["fetched_at"] > _PAYMENT_METHODS_TTL:
        try:
            async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
                response = await client.get(PAYMENT_METHODS_URL, headers=_mp_headers())
            if response.status_code == 200:
                types: dict[str, set[str]] = {}
                for method in response.json():
                    types.setdefault(method.get("id"), set()).add(method.get("payment_type_id"))
                _payment_methods_cache.update(fetched_at=now, types=types)
        except httpx.HTTPError as e:
            logger.warning(f"No se pudo consultar el catalogo de medios de pago de Mercado Pago: {e}")

    known = _payment_methods_cache["types"].get(payment_method_id) or set()
    if has_token:
        for preferred in ("debit_card", "credit_card", "prepaid_card"):
            if preferred in known:
                return preferred
        return "debit_card" if payment_method_id.startswith("deb") else "credit_card"
    for candidate in ("ticket", "atm", "bank_transfer"):
        if candidate in known:
            return candidate
    return "ticket"

def _mp_headers(idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    return headers

async def create_payment(order: Order, submit_data: dict) -> dict:
    """Cobra via `POST /v1/orders` y devuelve el resultado en formato de pago
    (`_order_as_payment`). El monto siempre viene de `order.total` en Postgres, nunca de
    submit_data.

    Un rechazo de la tarjeta NO es un error HTTP de este lado: la Orders API responde `402`
    con la order completa (`status: failed`) en `data`, y se devuelve igual que un exito para
    que `finalize_order_payment` la cancele y libere la reserva.

    X-Idempotency-Key es un hash determinista de `order.uuid` + el payload final, NO un uuid4
    fresco ni un order.uuid fijo puro. Un order.uuid fijo dejaba cualquier orden que fallara
    una vez atascada repitiendo ese mismo error para siempre (confirmado en vivo el
    2026-08-12). Un uuid4 fresco en cada llamada abria el problema opuesto: un timeout de este
    lado *despues* de que MP ya proceso el cobro, seguido de un reintento, generaba una key
    nueva que MP no podia reconocer como duplicado - riesgo real de cobro doble. Con el hash,
    un reintento del mismo envio es seguro y un envio distinto (token nuevo, correo corregido)
    genera una key nueva. La Orders API responde 409 a una key repetida con otro body, cosa
    que este hash nunca produce porque el body forma parte de la key.

    `payer.email` es obligatorio: si el Brick no lo trae en `formData`, se cae al correo de
    contacto del checkout / de la cuenta (mismo fallback que
    `order_notification_service.notify_order_confirmed`). Para OXXO la guia de migracion
    marca nombre, apellido e identificacion como obligatorios; en pruebas basta el correo
    (verificado 2026-10-25), pero se mandan nombre y apellido del checkout cuando existen.
    """
    submit_payer = submit_data.get("payer") or {}
    contact = (order.delivery_info or {}).get("contactInfo") or {}
    payer_email = submit_payer.get("email")
    if not payer_email:
        client_account = await order.awaitable_attrs.client_account
        payer_email = contact.get("email") or (client_account.email if client_account else None)

    payer: dict = {"email": payer_email}
    first_name, _, last_name = (contact.get("name") or "").strip().partition(" ")
    if first_name:
        payer["first_name"] = first_name
    if last_name.strip():
        payer["last_name"] = last_name.strip()
    identification = submit_payer.get("identification") or {}
    if identification.get("number"):
        payer["identification"] = {"type": identification.get("type"), "number": identification.get("number")}

    payment_method_id = submit_data.get("paymentMethodId")
    token = submit_data.get("token")
    payment_type = submit_data.get("paymentTypeId")
    if payment_type not in _PAYMENT_TYPES:
        payment_type = await _payment_type_for(payment_method_id, has_token=bool(token))
    method: dict = {"id": payment_method_id, "type": payment_type}
    if token:
        method["token"] = token
        method["installments"] = submit_data.get("installments") or 1

    amount = f"{Decimal(str(order.total)):.2f}"
    payload = {
        "type": "online",
        "processing_mode": "automatic",
        "total_amount": amount,
        "external_reference": order.uuid,
        "description": f"Pedido Ferretería Charly #{order.uuid}",
        "payer": payer,
        "transactions": {"payments": [{"amount": amount, "payment_method": method}]},
    }

    idempotency_source = json.dumps(payload, sort_keys=True, default=str)
    idempotency_key = hashlib.sha256(f"{order.uuid}:{idempotency_source}".encode()).hexdigest()

    async def attempt():
        async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
            return await client.post(ORDERS_URL, json=payload, headers=_mp_headers(idempotency_key=idempotency_key))

    response = await request_with_backoff(attempt, context=f"MP create_order orden {order.uuid}")

    if response.status_code == 402:
        body = response.json()
        logger.info(f"Mercado Pago rechazo el cobro de la orden {order.uuid}: {body.get('errors')}")
        return _order_as_payment(body.get("data") or {})
    if response.status_code not in (200, 201):
        raise_upstream_error(response, f"Mercado Pago rechazo el cobro de la orden {order.uuid}", "No se pudo procesar el pago con Mercado Pago. Intenta nuevamente.")

    return _order_as_payment(response.json())

async def _get_mp_order_raw(mp_order_id: str) -> dict:
    async def attempt():
        async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
            return await client.get(f"{ORDERS_URL}/{mp_order_id}", headers=_mp_headers())

    response = await request_with_backoff(attempt, context=f"MP get_order {mp_order_id}")
    if response.status_code != 200:
        raise_upstream_error(response, f"Error al consultar la order {mp_order_id} en Mercado Pago", "No se pudo consultar el estado del pago en Mercado Pago.")
    return response.json()

async def get_payment(mp_order_id: str) -> dict:
    """Consulta el estado autoritativo de una order; los webhooks solo avisan que algo
    cambio, nunca se confia en su cuerpo."""
    _require_mp_order_id(mp_order_id)
    return _order_as_payment(await _get_mp_order_raw(mp_order_id))

async def get_chargeback(case_id: str) -> dict:
    """Detalle de un contracargo ("Compra no reconocida") por su `case_id` (el que viene en
    `transactions.chargebacks[]` de la order): coverage_elegible/documentation_required/
    date_documentation_deadline y coverage_applied (None mientras esta en proceso, true/false
    al resolverse). Ver chargeback_service.py."""
    async def attempt():
        async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
            return await client.get(f"{CHARGEBACKS_URL}/{case_id}", headers=_mp_headers())

    response = await request_with_backoff(attempt, context=f"MP get_chargeback {case_id}")

    if response.status_code != 200:
        raise_upstream_error(response, f"Error al consultar el contracargo {case_id} en Mercado Pago", "No se pudo consultar el contracargo en Mercado Pago.")

    return response.json()

async def refund_payment(mp_order_id: str, amount: Decimal | None = None) -> dict:
    """Reembolsa una order cobrada via `POST /v1/orders/{id}/refund`: total si `amount` es
    None (body sin `transactions`), parcial si se da (con el id de la transaccion `PAY...`,
    que se lee de la order). Devuelve `{id, amount}` del ultimo reembolso no fallido, la forma
    que leen los llamadores. La key de idempotencia es un uuid4 por llamada: los reintentos
    dentro de la misma llamada la reusan y dos reembolsos parciales distintos no chocan."""
    _require_mp_order_id(mp_order_id)
    body: dict = {}
    if amount is not None:
        mp_order = await _get_mp_order_raw(mp_order_id)
        transaction_id = ((mp_order.get("transactions") or {}).get("payments") or [{}])[0].get("id")
        body = {"transactions": [{"id": transaction_id, "amount": f"{Decimal(str(amount)):.2f}"}]}
    idempotency_key = uuid.uuid4().hex

    async def attempt():
        async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
            return await client.post(f"{ORDERS_URL}/{mp_order_id}/refund", json=body, headers=_mp_headers(idempotency_key=idempotency_key))

    # Justo despues de otro reembolso, MP responde 409 `post_processing_operation_pending`
    # ("still being processed, try again shortly") durante unos segundos (visto en pruebas
    # con 3 s de separacion). Se reintenta con la misma key en vez de devolver un 502.
    for wait_seconds in (3, 6, 0):
        response = await request_with_backoff(attempt, context=f"MP refund_order {mp_order_id}")
        if not (response.status_code == 409 and "post_processing_operation_pending" in response.text) or not wait_seconds:
            break
        await asyncio.sleep(wait_seconds)

    if response.status_code not in (200, 201):
        raise_upstream_error(response, f"Error al reembolsar la order {mp_order_id} en Mercado Pago", "No se pudo reembolsar el pago en Mercado Pago.")

    refunds = [r for r in (response.json().get("transactions") or {}).get("refunds") or [] if r.get("status") != "failed"]
    last = refunds[-1] if refunds else {}
    result: dict = {"id": last.get("id")}
    # Sin "amount" los llamadores caen a order.total (reembolso total); nunca mandar None.
    refunded_amount = last.get("amount") or (str(amount) if amount is not None else None)
    if refunded_amount is not None:
        result["amount"] = refunded_amount
    return result

async def cancel_payment(mp_order_id: str) -> dict:
    """Cancela una order sin cobrar via `POST /v1/orders/{id}/cancel` - mas barato que un
    reembolso. Solo acepta orders en `created`/`action_required` (p. ej. una ficha OXXO sin
    pagar); una order `processing` no se puede cancelar y MP responde error."""
    _require_mp_order_id(mp_order_id)
    idempotency_key = uuid.uuid4().hex

    async def attempt():
        async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
            return await client.post(f"{ORDERS_URL}/{mp_order_id}/cancel", headers=_mp_headers(idempotency_key=idempotency_key))

    response = await request_with_backoff(attempt, context=f"MP cancel_order {mp_order_id}")
    if response.status_code not in (200, 201):
        raise_upstream_error(response, f"Error al cancelar la order {mp_order_id} en Mercado Pago", "No se pudo cancelar el pago pendiente en Mercado Pago.")
    return _order_as_payment(response.json())

async def verify_mercadopago_webhook_signature(request: Request) -> bool:
    """Valida x-signature/x-request-id contra MP_WEBHOOK_SECRET (esquema distinto al webhook saliente propio de este backend, ver notify_order_confirmed). Manifest `id:{data.id};request-id:{x-request-id};ts:{ts};`. La guia oficial de Webhooks ("Validar origen de una notificacion", seccion Sin SDKs) pide data.id en minusculas, pero el simulador del panel firma con data.id tal cual llega (verificado 2026-10-27 con un log de diagnostico): se aceptan ambas formas."""
    x_signature = request.headers.get("x-signature")
    x_request_id = request.headers.get("x-request-id")
    if not x_signature or not x_request_id:
        return False

    parts = dict(p.strip().split("=", 1) for p in x_signature.split(",") if "=" in p)
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False

    data_id = request.query_params.get("data.id") or request.query_params.get("id")
    if not data_id:
        return False

    # Primero data.id tal cual llega (lo que firma MP de verdad, medido 2026-10-27); en
    # minusculas como respaldo, que es lo que pide la guia.
    return any(
        verify_hmac_sha256(settings.MP_WEBHOOK_SECRET, f"id:{candidate};request-id:{x_request_id};ts:{ts};".encode(), v1)
        for candidate in dict.fromkeys((data_id, data_id.lower()))
    )
