"""Product.attributes corrupto (guardado como LISTA en vez de objeto, por el viejo bug de
doble serializacion de apply_preset_to_products) no debe tirar GET /products/{uuid} con 500
(Sentry FERRETERIA-CHARLY-5), y el siguiente guardado debe dejarlo como objeto."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import insert, select, text

from app.models.attribute import Attribute, AttributePreset, attribute_preset_items
from app.models.product import Product
from app.services import attribute_service
from tests.conftest import make_product

CORRUPT = ['{"color": "rojo"}', {}]


def test_stored_attributes_ignores_non_objects():
    assert attribute_service.stored_attributes(None) == {}
    assert attribute_service.stored_attributes(CORRUPT) == {}
    assert attribute_service.stored_attributes("texto") == {}
    assert attribute_service.stored_attributes({"color": "rojo"}) == {"color": "rojo"}


async def test_product_detail_with_corrupt_attributes_returns_200(db, api):
    product = make_product(details_updated_at=datetime.now(timezone.utc), attributes=CORRUPT)
    db.add(product)
    await db.commit()

    resp = await api.get(f"/v1/products/{product.sicar_uuid}")

    assert resp.status_code == 200
    assert resp.json()["attributes"] == []


async def test_apply_preset_repairs_corrupt_attributes(db):
    attr = Attribute(
        uuid=str(uuid.uuid4()), name="Color", slug=f"color-{uuid.uuid4().hex[:6]}",
        data_type="TEXT", updated_at=datetime.now(timezone.utc),
    )
    preset = AttributePreset(
        uuid=str(uuid.uuid4()), name="Pintura", slug=f"pintura-{uuid.uuid4().hex[:6]}",
        updated_at=datetime.now(timezone.utc),
    )
    product = make_product(attributes=CORRUPT)
    db.add_all([attr, preset, product])
    await db.flush()
    await db.execute(insert(attribute_preset_items).values(preset_uuid=preset.uuid, attribute_uuid=attr.uuid))
    await db.commit()

    _, scaffolded = await attribute_service.apply_preset_to_products(db, preset.uuid, [product.sicar_uuid])

    assert scaffolded == 1
    kind = await db.scalar(select(text("jsonb_typeof(attributes)")).select_from(Product).where(Product.id == product.id))
    assert kind == "object"
    stored = await db.scalar(select(Product.attributes).where(Product.id == product.id))
    assert stored == {attr.slug: None}
