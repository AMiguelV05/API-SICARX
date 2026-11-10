"""Resolucion de la direccion de entrega a domicilio (DELIVERYMAN), compartida por
POST /orders y POST /shipping/quotes para que ambos validen y normalicen igual."""
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import ClientAccount
from app.schemas.client import ClientAddressPublic
from app.schemas.orders import GuestDeliveryAddress
from app.services.address_service import get_owned_address

GUEST_ADDRESS_UUID_ERROR = "Un pedido sin cuenta no puede usar una dirección guardada; envía 'address' en su lugar."
ACCOUNT_INLINE_ADDRESS_ERROR = "Una cuenta autenticada debe usar 'addressUuid' de una dirección guardada, no una dirección inline."


def _sicar_address(street, ext_number, int_number, neighborhood, city, county, state, zip_code, references) -> dict:
    """Forma que se guarda en delivery_info.contactInfo.address (la que exigia Sicar X)."""
    return {
        "street": street,
        "extNumber": ext_number,
        "intNumber": int_number,
        "district": neighborhood,
        "city": city,
        "county": county,
        "state": state,
        "zipCode": zip_code,
        "country": "MEX",
        "reference": references,
    }


async def resolve_delivery_address(
    db: AsyncSession, client: ClientAccount | None, address_uuid: str | None, guest_address: GuestDeliveryAddress | None,
) -> tuple[dict, dict]:
    """Devuelve (direccion para delivery_info.contactInfo.address, snapshot ClientAddressPublic
    camelCase para Order.delivery_address_snapshot). Cuenta: `address_uuid` de su address book,
    con todos los campos de entrega llenos (400 nombrando los que faltan). Invitado: `guest_address`
    inline, ya validada completa por el schema."""
    if client is not None:
        if guest_address is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ACCOUNT_INLINE_ADDRESS_ERROR)
        address = await get_owned_address(db, client, address_uuid)
        missing = [field for field, value in [
            ("street", address.street),
            ("city", address.city),
            ("county", address.county),
            ("state", address.state),
            ("zipCode", address.zip_code),
            ("extNumber", address.ext_number),
            ("neighborhood", address.neighborhood),
        ] if not value]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"La dirección seleccionada está incompleta para entrega a domicilio (faltan: {', '.join(missing)}).",
            )
        contact_address = _sicar_address(
            address.street, address.ext_number, address.int_number, address.neighborhood, address.city,
            address.county, address.state, address.zip_code, address.references,
        )
        # Foto fija del address book en la forma ClientAddressPublic - ver Order.delivery_address_snapshot.
        snapshot = ClientAddressPublic.model_validate(address).model_dump(mode="json", by_alias=True)
        return contact_address, snapshot

    if address_uuid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GUEST_ADDRESS_UUID_ERROR)
    if guest_address is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta la dirección de entrega.")
    g = guest_address
    contact_address = _sicar_address(g.street, g.ext_number, g.int_number, g.neighborhood, g.city, g.county, g.state, g.zip_code, g.references)
    # Mismo shape que el camino de cuenta, pero sin fila real de address book (uuid None).
    snapshot = ClientAddressPublic(
        uuid=None, street=g.street, ext_number=g.ext_number, int_number=g.int_number, neighborhood=g.neighborhood,
        city=g.city, county=g.county, state=g.state, zip_code=g.zip_code, references=g.references,
        latitude=g.latitude, longitude=g.longitude,
    ).model_dump(mode="json", by_alias=True)
    return contact_address, snapshot
