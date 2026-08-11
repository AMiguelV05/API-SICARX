from app.models.order import Order


def resolve_client_name(order: Order, client_account) -> str | None:
    """Nombre a mostrar para notificaciones/admin - una orden de invitado no tiene
    `client_account` (None), asi que cae al `contactInfo.name` capturado en el checkout
    (mismo campo que `contactInfo.email` ya usan estas mismas llamadas para `clientEmail`).

    Vive en su propio modulo (no en order_history_service.py) para que
    order_notification_service.py/order_cancellation_notification_service.py puedan
    importarlo sin ciclo - order_history_service.py ya importa esos dos modulos."""
    if client_account is not None:
        return client_account.name
    return ((order.delivery_info or {}).get("contactInfo") or {}).get("name")
