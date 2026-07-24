import logging

import httpx

from app.core.config import settings
from app.models.order import Order

RESEND_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

async def send_order_confirmation_email(order: Order) -> None:
    """Envia el correo de confirmacion de pedido via Resend. No fatal si falla - ver
    create_preference en payment_service.py, mismo patron: un correo que no se manda no
    debe bloquear ni revertir el pago ya aplicado en finalize_order_payment (unico
    llamador de esta funcion)."""
    client_account = await order.awaitable_attrs.client_account
    if not client_account or not client_account.email:
        logger.warning(f"Orden {order.uuid}: no se pudo resolver el email del cliente, se omite el correo de confirmacion.")
        return

    payload = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [client_account.email],
        "subject": f"Confirmación de tu pedido #{order.serie_folio or order.uuid}",
        "html": _build_confirmation_html(order, client_account.name),
    }

    try:
        async with httpx.AsyncClient(timeout=RESEND_TIMEOUT) as client:
            response = await client.post(
                RESEND_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        if response.status_code not in (200, 201):
            logger.error(f"Resend rechazo el correo de confirmacion para la orden {order.uuid}: {response.status_code} - {response.text}")
            return
        logger.info(f"Correo de confirmacion enviado para la orden {order.uuid}.")
    except httpx.HTTPError as e:
        logger.error(f"Error de red enviando correo de confirmacion para la orden {order.uuid}: {e}")

def _build_confirmation_html(order: Order, client_name: str) -> str:
    items_rows = "".join(
        f"<tr>"
        f"<td style=\"padding:6px 8px;border-bottom:1px solid #eee;\">{item.get('description', '')}</td>"
        f"<td style=\"padding:6px 8px;border-bottom:1px solid #eee;text-align:center;\">{item.get('quantity', '')} {item.get('unit', '')}</td>"
        f"</tr>"
        for item in (order.items or [])
    )

    delivery_info = order.delivery_info or {}
    delivery_type = delivery_info.get("deliveryType")
    delivery_label = "Entrega a domicilio" if delivery_type == "DELIVERYMAN" else "Recoger en tienda"

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#222;">
        <h2>¡Gracias por tu compra, {client_name}!</h2>
        <p>Tu pedido <strong>#{order.serie_folio or order.uuid}</strong> fue confirmado.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <thead>
                <tr>
                    <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ccc;">Producto</th>
                    <th style="text-align:center;padding:6px 8px;border-bottom:2px solid #ccc;">Cantidad</th>
                </tr>
            </thead>
            <tbody>
                {items_rows}
            </tbody>
        </table>
        <p><strong>Total:</strong> ${order.total:.2f} MXN</p>
        <p><strong>Entrega:</strong> {delivery_label}</p>
    </div>
    """
