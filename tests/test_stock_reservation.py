"""Cubre el race de Product.reserved/available_stock documentado en CLAUDE.md
("Reserva local de stock") - el bug real que este backend ya tuvo una vez: el sync
periodico de Sicar X sobreescribe `stock` pero nunca debe tocar `reserved`."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.models.product import Product
from app.services.product_stock_service import apply_reserved_deltas
from app.services.order_service import validate_cart_items
from tests.conftest import make_product


async def test_apply_reserved_deltas_increments_and_available_stock_drops(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("0"))
    db.add(product)
    await db.flush()

    await apply_reserved_deltas(db, [(product.sicar_uuid, Decimal("3"))])
    await db.flush()
    await db.refresh(product)

    assert product.reserved == Decimal("3")
    assert product.available_stock == Decimal("7")


async def test_sync_tick_does_not_clobber_reservation(db):
    """Repite el walkthrough de CLAUDE.md: stock=10,reserved=0 -> checkout qty 3 ->
    reserved=3 (available=7) -> un sync tick que solo escribe `stock` (whitelist real de
    sync_task.py) no debe tocar `reserved`."""
    product = make_product(stock=Decimal("10"), reserved=Decimal("0"))
    db.add(product)
    await db.flush()

    await apply_reserved_deltas(db, [(product.sicar_uuid, Decimal("3"))])
    await db.flush()

    # Simula el upsert de sync_task.py: solo escribe `stock` (Sicar sigue reportando 10),
    # nunca `reserved` - ver sync_task.py, product_values no incluye reserved.
    await db.execute(update(Product).where(Product.sicar_uuid == product.sicar_uuid).values(stock=Decimal("10")))
    await db.flush()
    await db.refresh(product)

    assert product.stock == Decimal("10")
    assert product.reserved == Decimal("3")
    assert product.available_stock == Decimal("7")


async def test_reserved_release_clamped_at_zero(db):
    """apply_reserved_deltas clampea con GREATEST(...,0) - una orden pre-existente a la
    reserva local que igual intenta liberar no debe dejar `reserved` negativo (violaria
    ck_products_reserved_non_negative)."""
    product = make_product(stock=Decimal("5"), reserved=Decimal("0"))
    db.add(product)
    await db.flush()

    await apply_reserved_deltas(db, [(product.sicar_uuid, Decimal("-2"))])
    await db.flush()
    await db.refresh(product)

    assert product.reserved == Decimal("0")


async def test_validate_cart_items_rejects_when_available_stock_insufficient(db):
    product = make_product(stock=Decimal("5"), reserved=Decimal("4"))  # available_stock = 1
    db.add(product)
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await validate_cart_items(db, [product.sicar_uuid], {product.sicar_uuid: 2})
    assert exc_info.value.status_code == 409


async def test_validate_cart_items_rejects_zero_price_product(db):
    """Guarda contra el edge case documentado en CLAUDE.md: un producto sincronizado sin
    entrada de precio para la lista actual queda price=0.00 y nunca debe ser comprable
    gratis, aunque tenga stock de sobra."""
    product = make_product(stock=Decimal("10"), reserved=Decimal("0"), price=Decimal("0.00"))
    db.add(product)
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await validate_cart_items(db, [product.sicar_uuid], {product.sicar_uuid: 1})
    assert exc_info.value.status_code == 409


async def test_validate_cart_items_accepts_when_stock_and_price_ok(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"), price=Decimal("50.00"))
    db.add(product)
    await db.flush()

    resolved = await validate_cart_items(db, [product.sicar_uuid], {product.sicar_uuid: 5})
    assert resolved[product.sicar_uuid].id == product.id
